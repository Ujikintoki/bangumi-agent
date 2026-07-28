"""
Unified System Prompt Builder — Phase 7 重设计

5 段组装（从 8 层简化）：Character Card → Tool Intuition → Continuity →
Scene Hint → Guardrails。

核心变化：
- Character Card（SHOW 风格）替代碎片化的 identity + motivation + expression_guide
- TOOL_INTUITION（行为性描述）替代 _TOOL_CALLING_RULES 的过程式规则
- Scene Hint（一句话提示）替代冗长的 intent strategy 段落
- _DATA_INTERPRETATION 删除——数据呈现规则完全属于 render 层
- guardrails {word_limit} 按 depth 格式化

==== 使用方式 ====

::

    from agent.persona.profiles import get_character, get_agent_profile
    from agent.orchestrate.prompt_builder import build_system_prompt

    character = get_character("bangumi")
    agent = get_agent_profile("companion")
    prompt = build_system_prompt(agent_profile=agent, character=character, depth="auto")
"""

from __future__ import annotations

from agent.persona.profiles import (
    AgentProfile,
    CharacterProfile,
    _render_tone,
    get_character_card,
)

# ═══════════════════════════════════════════════════════════════════════════
# Tool Intuition — 行为性工具使用指引（替代 _TOOL_CALLING_RULES）
# ═══════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════
# Tool Guidance — 统一工具使用指引（Phase 8: 五合一合并）
#
# 替换：TOOL_INTUITION + tool_strategy + tool_behavior +
#       TOOL_DEPENDENCY_CONSTRAINT + _DATA_MODEL_CONSTRAINT
# ═══════════════════════════════════════════════════════════════════════════

TOOL_GUIDANCE = """\
## 你的工具

你查数据不是为了报数据——是为了验证你的直觉。查到数据后，说你的判断。\
数据是注脚，不是正文。一个恰到好处的数据点比三个无关的数据点有说服力得多。

**什么时候查**
- 用户问到了你不知道的 → 查一下，结果放回去就继续聊
- 用户没问到的 → 不主动扩展
- 常识问题 → 基于训练知识直接回答，不查

**多少算够**
- search 返回的信息通常已够用——评分、排名、基本信息都在里面
- 只有用户**明确**问了 search 里没有的（详情、角色列表、评论），才调 detail 类工具
- 一次搜索能回答就不两次
- 数据够了直接回复，不要无意义地继续调工具
- "没查到"不是你的失败——诚实说没找到

**并行规则**
- 依赖 subject_id / person_id 的工具（detail、characters、opinions）\
不能和 search 在同一轮并行——必须先 search 拿 id
- 互不依赖的工具可以并行，但同一轮最多 3 个——更多的分批进行
- 时效类工具（calendar、trending）直接调，不需要先搜 id

**数据真实性**
- 时效性问题（"今季新番"、"当前热门"）——只使用工具返回的最新数据，\
工具没返回就诚实说无法获取。不要用训练知识编造"当前"的作品列表
- 评分和排名只从工具数据中引用，工具没返回的数字不编造
- 角色和声优没有评分——只有作品有"""

# Phase 8 向后兼容别名
TOOL_INTUITION = TOOL_GUIDANCE

# ═══════════════════════════════════════════════════════════════════════════
# Continuity Rules — 话题绑定（保留，略精简）
# ═══════════════════════════════════════════════════════════════════════════

_CONTINUITY_RULES = """\
## 对话连续性

如果对话历史中有你的回复，先判断用户当前问题与历史的关系。

**明确指代 → 使用对话历史**
- 代词回指：\"这部\"、\"那个\"、\"它\"、\"这些\"
- 省略主语：\"评分怎么样？\"、\"评论呢？\"、\"还有吗？\"
- 集合操作：\"评分最高的\"、\"8分以上的\"
从上一轮回复中提取对应实体继续。

**全新话题 → 忽略旧历史**
新作品名、新类型、新人物 → 独立处理，不将旧话题混入新回答。

**无法确定 → 宁可追问，不错误关联。**"""

# ═══════════════════════════════════════════════════════════════════════════
# Word limits per depth（与 render._RENDER_WORD_LIMIT 保持一致）
# ═══════════════════════════════════════════════════════════════════════════

_WORD_LIMITS: dict[str, str] = {
    "quick": "120",
    "auto": "200",
    "deep": "350",
}

# ═══════════════════════════════════════════════════════════════════════════
# Builder — 5 段组装
# ═══════════════════════════════════════════════════════════════════════════


def build_system_prompt(
    agent_profile: AgentProfile,
    character: CharacterProfile,
    *,
    depth: str = "auto",
    intent: str | None = None,
    intent_strategies: dict[str, str] | None = None,
    scene_hints: dict[str, str] | None = None,
    memory_context: str = "",
    critic_feedback: str = "",
    snark: float | None = None,
    depth_taste: float | None = None,
    initiative: float | None = None,
) -> str:
    """组装 System Prompt — 4 段结构。

    Phase 8: TOOL_GUIDANCE 五合一替代碎片化工具指引，
    tool_constraint 参数移除——TOOL_GUIDANCE 覆盖所有深度模式需求。

    Args:
        agent_profile: Agent 配置。
        character: 当前使用的角色人格。
        depth: 深度模式（\"auto\" | \"quick\" | \"deep\"）。
        intent: 查询意图。
        intent_strategies: [deprecated] 意图策略变体 dict。
            向后兼容——传入但未传 scene_hints 时作为 fallback。
        scene_hints: Phase 7 新增——意图对应的简短场景提示。
        memory_context: L2 记忆召回 + tone 提示的格式化文本。
        critic_feedback: Critic 的定向反馈（仅 deep 模式传入）。
        snark: 覆盖 character.snark。None 时使用角色默认值。
        depth_taste: 覆盖 character.depth_taste。
        initiative: 覆盖 character.initiative。

    Returns:
        完整的 System Prompt 字符串。
    """
    # ── 参数解析 ─────────────────────────────────────────────
    _snark = snark if snark is not None else character.snark
    _dt = depth_taste if depth_taste is not None else character.depth_taste
    _init = initiative if initiative is not None else character.initiative
    tone_parts = _render_tone(_snark, _dt, _init)

    parts: list[str] = []

    # ── Section 1: Character Card ────────────────────────────
    card = get_character_card(character.key)
    if card:
        parts.append(f"# 你是谁\n\n{card}")
    else:
        # 向后兼容：无 Character Card 时用旧字段
        parts.append(f"# {character.identity}\n\n{character.motivation}")
        if character.expression_guide:
            parts.append(f"## 表达风格\n{character.expression_guide}")

    # ── Section 1.5: 今天的语气（轻量，从参数生成） ─────────
    parts.append(f"## 今天的语气\n{tone_parts['tone']}")

    # ── Section 2: Capabilities + Tool Guidance（Phase 8 合并） ──
    parts.append(agent_profile.capabilities)
    parts.append(TOOL_GUIDANCE)

    # ── Section 2.5: 输出格式（纯格式，不含风格） ────────────
    if agent_profile.output_format_guide:
        parts.append(agent_profile.output_format_guide)

    # ── Section 3: Continuity Rules ──────────────────────────
    parts.append(_CONTINUITY_RULES)

    # ── Section 4: Scene Hint ───────────────────────────────
    hint = None
    if scene_hints and intent:
        hint = scene_hints.get(intent, scene_hints.get("unknown", ""))
    elif intent_strategies and intent:
        # 向后兼容：旧 intent_strategies → 完整注入（deprecated path）
        hint = intent_strategies.get(intent, intent_strategies.get("unknown", ""))
    if hint:
        parts.append(hint)

    # ── Section 5: Context（memory + critic_feedback） ──────
    if memory_context:
        parts.append(memory_context)
    if critic_feedback:
        safe_feedback = critic_feedback
        if "|" not in critic_feedback and len(critic_feedback) > 200:
            safe_feedback = critic_feedback[:200] + "\n…[反馈过长已截断]"
        parts.append(
            f"\n## ⚠️ 上一轮回复需要改进\n{safe_feedback}\n请针对以上问题修正你的回复。"
        )

    # ── Section 6: Guardrails ───────────────────────────────
    word_limit = _WORD_LIMITS.get(depth, _WORD_LIMITS["auto"])
    parts.append(character.guardrails.format(word_limit=word_limit))

    return "\n\n".join(parts)
