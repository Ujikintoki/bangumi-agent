"""
Unified System Prompt Builder — 统一 prompt 组装器

两个 Agent（Dialogue / Research）共用同一个 builder。
组装顺序体现"角色优先"原则——角色身份在最前面，能力是角色的附属。

==== 组装顺序 ====

::

    1. character.identity + motivation      ← 角色是第一层
    2. agent_profile.capabilities           ← 能力是角色的附属
    3. character.tool_behavior              ← 角色如何使用工具
    4. agent_profile.tool_strategy          ← 具体策略
    5. tool_constraint（如有）               ← 工具依赖规则
    6. intent 策略变体（如有）                ← 当前场景
    7. memory_context（如有）                ← 用户历史 + tone 提示
    8. critic_feedback（如有）               ← 上一轮缺陷
    9. character.expression_guide          ← 怎么说
    10. agent_profile.output_format_guide  ← 格式指引
    11. character.guardrails                ← 硬约束
    12. last_chance_instruction（如有）     ← 熔断指令

==== 使用方式 ====

::

    from agent.profiles import get_character, get_agent_profile
    from agent.prompt_builder import build_system_prompt

    character = get_character("bangumi")
    agent = get_agent_profile("dialogue")
    prompt = build_system_prompt(agent_profile=agent, character=character)
"""

from __future__ import annotations

from agent.profiles import AgentProfile, CharacterProfile

# ═══════════════════════════════════════════════════════════════════════════
# 常用对话连续性规则（两个 Agent 共用）
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
# 数据模型约束 + 关键规则（Research 用）
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
# Builder
# ═══════════════════════════════════════════════════════════════════════════


def build_system_prompt(
    agent_profile: AgentProfile,
    character: CharacterProfile,
    *,
    intent: str | None = None,
    intent_strategies: dict[str, str] | None = None,
    tool_constraint: str = "",
    memory_context: str = "",
    critic_feedback: str = "",
    last_chance_instruction: str = "",
) -> str:
    """组装完整的 System Prompt。

    角色优先——角色身份在最前面，能力是角色的附属。
    两个 Agent（Dialogue / Research）共用同一个 builder，
    通过 agent_profile 和 character 参数区分行为。

    Args:
        agent_profile: Agent 配置（dialogue 或 research）。
        character: 当前使用的角色人格。
        intent: 查询意图（Research 用；Dialogue 传 None）。
        intent_strategies: 意图策略变体 dict（INTENT_PROMPTS）。
        tool_constraint: 工具依赖约束（TOOL_DEPENDENCY_CONSTRAINT）。
        memory_context: L2 记忆召回 + tone 提示的格式化文本。
        critic_feedback: Critic 的定向反馈。
        last_chance_instruction: Dialogue 熔断指令。

    Returns:
        完整的 System Prompt 字符串。
    """
    parts: list[str] = []

    # ── Layer 1: 角色是第一层 ──────────────────────────────
    parts.append(f"# {character.identity}\n\n{character.motivation}")

    # ── Layer 2: 能力是角色的附属 ─────────────────────────
    parts.append(agent_profile.capabilities)

    # ── Layer 3: 角色如何使用工具 ─────────────────────────
    if character.tool_behavior:
        parts.append(f"## 你对工具的态度\n{character.tool_behavior}")

    # ── Layer 4: 具体工具策略 ─────────────────────────────
    parts.append(agent_profile.tool_strategy)

    # ── Layer 5: 工具依赖规则（如有） ─────────────────────
    if tool_constraint:
        parts.append(tool_constraint)

    # ── Layer 6: 关键规则 ──────────────────────────────
    parts.append(_TOOL_CALLING_RULES)

    # ── Layer 7: 对话连续性 ──────────────────────────────
    parts.append(_CONTINUITY_RULES)

    # ── Layer 8: 意图策略变体（如有） ─────────────────────
    if intent and intent_strategies:
        strategy = intent_strategies.get(intent, intent_strategies.get("unknown", ""))
        if strategy:
            parts.append(strategy)

    # ── Layer 9: 用户历史 + tone 提示（如有） ─────────────
    if memory_context:
        parts.append(memory_context)

    # ── Layer 10: Critic 反馈（如有） ──────────────────────
    if critic_feedback:
        # 期望格式："<缺陷> | <建议> | <缺失类型>"
        safe_feedback = critic_feedback
        if "|" not in critic_feedback and len(critic_feedback) > 200:
            safe_feedback = critic_feedback[:200] + "\n…[反馈过长已截断]"
        parts.append(
            f"\n## ⚠️ 上一轮回复需要改进\n{safe_feedback}\n请针对以上问题修正你的回复。"
        )

    # ── Layer 11: 怎么说 + 格式指引 ──────────────────────
    parts.append(f"## 表达风格\n{character.expression_guide}")
    parts.append(agent_profile.output_format_guide)

    # ── Layer 12: 硬约束 ──────────────────────────────────
    parts.append(character.guardrails)

    # ── Layer 13: 熔断指令（如有） ────────────────────────
    if last_chance_instruction:
        parts.append(last_chance_instruction)

    return "\n\n".join(parts)
