"""
Unified System Prompt Builder — 统一 prompt 组装器

Phase 6: 14 层 → 8 层。expression_guide 从 layer 12 提前到 layer 2。
角色人格紧跟身份定义——LLM 先知道"怎么说"再学"怎么查数据"。

==== 组装顺序（8 层）====

::

    1. character.identity + motivation                ← 角色是第一层
    2. character.expression_guide + output_format     ← 怎么说（提前！）
    3. agent_profile.capabilities                     ← 有什么能力
    4. tool_behavior + tool_strategy + tool_constraint ← 怎么用工具（合并层）
    5. _TOOL_CALLING_RULES + _CONTINUITY_RULES         ← 关键规则（合并层）
    6. data_guide（deep）+ intent 策略                  ← 当前场景
    7. memory_context + critic_feedback                ← 上下文
    8. character.guardrails                            ← 硬约束

==== 使用方式 ====

::

    from agent.persona.profiles import get_character, get_agent_profile
    from agent.orchestrate.prompt_builder import build_system_prompt

    character = get_character("bangumi")
    agent = get_agent_profile("companion")
    prompt = build_system_prompt(agent_profile=agent, character=character, depth="auto")
"""

from __future__ import annotations

from agent.persona.profiles import AgentProfile, CharacterProfile

# ═══════════════════════════════════════════════════════════════════════════
# 常用对话连续性规则
# ═══════════════════════════════════════════════════════════════════════════

_CONTINUITY_RULES = """
## 对话连续性规则

如果本轮对话历史中包含你之前的回复，先判断用户当前问题与历史的关系。

### 话题绑定检测

**✅ 明确指代 → 使用对话历史**

- 代词回指："这部"、"那个"、"它"、"这些"
- 省略主语："评分怎么样？"、"评论呢？"、"还有吗？"
- 集合操作："评分最高的"、"8分以上的"、"里面哪个"
- 显式引用："你刚提到的"、"第一个"

从你上一轮回复中提取对应实体继续。

**❌ 全新话题 → 忽略对话历史**

新作品名、新类型、新人物 → 独立处理，**严禁**将旧话题混入新回答。

**⚠️ 模糊边界 → 保守处理**

无法确定时默认当作全新问题，宁可追问确认也不错误关联。

**原则：宁可少用历史（让用户补一句），不要错误关联（污染无关回答）。**
"""

# ═══════════════════════════════════════════════════════════════════════════
# 工具调用 + 诚实兜底规则
# ═══════════════════════════════════════════════════════════════════════════

_TOOL_CALLING_RULES = """
## ⚠️ 关键规则：工具调用后必须生成文字回复

- 收到工具返回的数据后，**必须**基于数据生成文字回复
- **严禁**连续调用多个工具而不生成任何文字输出
- 数据够了就直接回，不要无意义地继续调工具

## 当数据不足时

如果你搜索后没有获取到有效数据（搜索返回空、详情不存在等）：
- **不要编造**具体数字（评分、排名、集数、收藏数）
- 如实告诉用户你没找到，但用你的语气让它听起来不像是系统错误
- 主动建议替代方案：换关键词试试、换话题聊聊、换种问法

"没找到"不是你的失败——诚实比瞎编更让用户信任你。
"""

# ═══════════════════════════════════════════════════════════════════════════
# 数据解读指南（仅 depth=="deep" 时注入）
# ═══════════════════════════════════════════════════════════════════════════

_DATA_INTERPRETATION = """
## 数据解读指南

工具返回的是结构化数据（dict），不是自然语言。

**⚠️ 最重要的一条：以下分析框架是你的内部工具，不是输出模板。**
你算出完成率、判断口碑两极化——这些都是**思考过程**，不要逐条报告给用户。
用户不需要听"1-2 分 XX 人，9-10 分 YY 人，两端偏高"——他们只需要听"这部的口碑比较两极分化"。

### 什么是好的呈现

❌ 罗列数据：
"EVA 评分 9.1，排名 #1，评分总人数 9438 人。rating_count 显示 1-2 分 89 人，9-10 分 4567 人……"

✅ 融入叙述：
"EVA 稳坐 Bangumi 头把交椅，9.1 分。有意思的是打 1 分和打 10 分的人都特别多——爱的爱死恨的恨死，三十年了还没吵完。"

### 评分
- `score`: 0-10 分制。Bangumi 社区评分偏高——6.0 算合格，7.5+ 优秀，8.5+ 现象级，9.0+ 神作
- `rank`: 全站排名。结合 score 看——同分作品的 rank 差异反映评分人数和社区认可度
- `rating_total`: >10000 大众热门，1000-10000 圈内知名，<1000 冷门/新作
- `rating_count`: 长度为 10 的列表，索引 0 = 1 分人数，索引 9 = 10 分人数
  - 两端同时偏高 → 口碑两极化；集中在 7-9 → 口碑稳定

### 收藏
- `collection`: {"想看": N, "看过": N, "在看": N, "搁置": N, "抛弃": N}
- 看过/(看过+搁置+抛弃) ≈ 完成率，>70% 多数人能看完，<40% 弃坑率高

### 标签
- `tags`: [{"name": "机战", "count": 2345}, ...]
- 高 count = 社区共识的题材定位，**用于推荐**，不用于罗列

### 呈现原则
- 用户问口碑 → 说"两极分化"还是"一致好评"，不要列评分分布表
- 用户问热度 → 说"大众热门"还是"冷门佳作"，不要列收藏数据
- 用户问推荐 → 说"和它同标签的高分作品有..."，不要先贴 30 个标签再找
- 大胆融入你的角色风格——数据是你表演的道具，不是剧本
- **结论先行，数据是佐证不是主体**
"""


# ═══════════════════════════════════════════════════════════════════════════
# Builder（8 层）
# ═══════════════════════════════════════════════════════════════════════════


def build_system_prompt(
    agent_profile: AgentProfile,
    character: CharacterProfile,
    *,
    depth: str = "auto",
    intent: str | None = None,
    intent_strategies: dict[str, str] | None = None,
    tool_constraint: str = "",
    memory_context: str = "",
    critic_feedback: str = "",
) -> str:
    """组装完整的 System Prompt（8 层）。

    角色优先——角色身份在最前面，表达风格紧跟其后。
    depth 参数控制工具策略分支。

    Phase 6.5: 移除 ``data_guide`` 参数。数据呈现规则移到 render 层，
    主 prompt 专注工具策略和对话规则。

    Args:
        agent_profile: Agent 配置。
        character: 当前使用的角色人格。
        depth: 深度模式（"auto" | "quick" | "deep"）。
        intent: 查询意图。
        intent_strategies: 意图策略变体 dict。
        tool_constraint: 工具依赖约束（仅 deep 模式传入）。
        memory_context: L2 记忆召回 + tone 提示的格式化文本。
        critic_feedback: Critic 的定向反馈（仅 deep 模式传入）。

    Returns:
        完整的 System Prompt 字符串。
    """
    parts: list[str] = []

    # ── Layer 1: 角色是第一层 ──────────────────────────────
    parts.append(f"# {character.identity}\n\n{character.motivation}")

    # ── Layer 2: 怎么说（expression_guide 提前！） ────────
    expression_section = f"## 表达风格\n{character.expression_guide}"
    if agent_profile.output_format_guide:
        expression_section += f"\n\n{agent_profile.output_format_guide}"
    parts.append(expression_section)

    # ── Layer 3: 有什么能力 ─────────────────────────────────
    parts.append(agent_profile.capabilities)

    # ── Layer 4: 怎么用工具（合并层） ─────────────────────
    tool_section_parts: list[str] = []
    if character.tool_behavior:
        tool_section_parts.append(f"## 你对工具的态度\n{character.tool_behavior}")
    tool_section_parts.append(agent_profile.tool_strategy)
    if tool_constraint:
        tool_section_parts.append(tool_constraint)
    parts.append("\n\n".join(tool_section_parts))

    # ── Layer 5: 关键规则（合并层） ─────────────────────────
    parts.append(_TOOL_CALLING_RULES + "\n" + _CONTINUITY_RULES)

    # ── Layer 6: 当前场景（intent 策略） ─────
    if intent and intent_strategies:
        strategy = intent_strategies.get(intent, intent_strategies.get("unknown", ""))
        if strategy:
            parts.append(strategy)

    # ── Layer 7: 上下文（memory + critic_feedback） ─────────
    if memory_context:
        parts.append(memory_context)
    if critic_feedback:
        safe_feedback = critic_feedback
        if "|" not in critic_feedback and len(critic_feedback) > 200:
            safe_feedback = critic_feedback[:200] + "\n…[反馈过长已截断]"
        parts.append(
            f"\n## ⚠️ 上一轮回复需要改进\n{safe_feedback}\n请针对以上问题修正你的回复。"
        )

    # ── Layer 8: 硬约束 ─────────────────────────────────────
    parts.append(character.guardrails)

    return "\n\n".join(parts)
