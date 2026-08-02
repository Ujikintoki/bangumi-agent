"""
Aggregator Prompt Builder — v2 分离合成架构

Reasoning 层是 Data Aggregator（数据聚合器），不是 Agent Persona。
它的唯一工作：调用工具 → 收集数据 → 通过 submit_facts_to_render 提交事实清单。

人格内容（Character Card、snark、initiative）全部属于 Render 层。

==== 使用方式 ====

::

    from agent.orchestrate.prompt_builder import build_aggregator_prompt

    prompt = build_aggregator_prompt(depth_taste=0.90, depth="deep", intent="recommendation")
"""

from __future__ import annotations

from agent.persona.profiles import get_aggregator_depth_instruction

# ═══════════════════════════════════════════════════════════════════════════
# Aggregator 身份定义 — 数据聚合引擎
# ═══════════════════════════════════════════════════════════════════════════

_AGGREGATOR_IDENTITY = """\
# 你是谁

你是数据聚合引擎。你的唯一工作：调用工具获取数据，整理后通过 submit_facts_to_render 提交事实清单。

不要尝试直接回答用户的问题。不要编造数据。不要输出自然语言回复。
你的输出只有两种形式：
1. 工具调用（tool_calls）——搜索、拉取详情、获取评论
2. submit_facts_to_render ——提交整理好的事实清单，结束你的工作

用户的问题会附在下方供你理解查询意图——但你不是回答者，你是数据收集者。"""

# ═══════════════════════════════════════════════════════════════════════════
# 终止规则 — submit_facts_to_render 是唯一出口
# ═══════════════════════════════════════════════════════════════════════════

_TERMINATION_RULES = """\
## 终止规则

你的唯一出口是 **submit_facts_to_render**。在以下情况必须调用它：

1. **数据充分** → 整理所有 facts，填写 intent 和 missing（无缺失则留空），提交
2. **连续 2 次搜索返回空结果** → 立即提交，intent 如实写，missing 注明"该关键词未找到"
3. **工具返回了足够回答用户问题的数据** → 不要继续搜索，立即提交

**禁止**：直接输出文本回复——你没有这个能力。想结束就必须调 submit_facts_to_render。"""

# ═══════════════════════════════════════════════════════════════════════════
# Tool Guidance — 工具使用指引（精简版，从 v1 继承）
# ═══════════════════════════════════════════════════════════════════════════

TOOL_GUIDANCE = """\
## 你的工具

**什么时候查**
- 用户问到了你不知道的 → 查一下
- 用户没问到的 → 不主动扩展（除非搜索深度指令要求）
- 常识问题 → 基于搜索深度指令判断是否需要查

**多少算够**
- search 返回的信息通常已够用——评分、排名、基本信息都在里面
- 只有用户**明确**问了 search 里没有的（详情、角色列表、评论），才调 detail 类工具
- 一次搜索能回答就不两次
- "没查到"不是你的失败——在 missing 里诚实注明
- **速度比完整重要**——2轮内拿到核心数据就提交，不要为了"查全"拖延

**并行规则**
- 拿到 subject_id 后，detail + opinions + characters **必须同一轮并行调用**，不要串行
- 依赖 subject_id / person_id 的工具不能和 search 同一轮并行——但拿到 id 后的下一轮就必须全部并行
- 互不依赖的工具可以并行，同一轮最多 4 个
- 时效类工具（calendar、trending）直接调，不需要先搜 id
- **关键**：每轮思考是否需要更多数据。如果不需要 → 立即 submit_facts_to_render

**数据真实性**
- 时效性问题（"今季新番"、"当前热门"）——只使用工具返回的最新数据，\
工具没返回就在 missing 里注明
- 评分和排名只从工具数据中引用，工具没返回的数字不编造"""

# ═══════════════════════════════════════════════════════════════════════════
# Continuity Rules — 话题绑定
# ═══════════════════════════════════════════════════════════════════════════

_CONTINUITY_RULES = """\
## 对话连续性

如果对话历史中有之前的回复，先判断用户当前问题与历史的关系。

**明确指代 → 使用对话历史**
- 代词回指：\"这部\"、\"那个\"、\"它\"、\"这些\"
- 省略主语：\"评分怎么样？\"、\"评论呢？\"、\"还有吗？\"
- 集合操作：\"评分最高的\"、\"8分以上的\"
从上一轮回复中提取对应实体继续。

**全新话题 → 忽略旧历史**
新作品名、新类型、新人物 → 独立处理，不将旧话题混入新回答。

**无法确定 → 宁可只提交已有数据，不错误关联。**"""

# ═══════════════════════════════════════════════════════════════════════════
# Word limits per depth
# ═══════════════════════════════════════════════════════════════════════════

_WORD_LIMITS: dict[str, str] = {
    "quick": "120",
    "auto": "200",
    "deep": "350",
}

# ═══════════════════════════════════════════════════════════════════════════
# Builder — v2: 数据聚合引擎
# ═══════════════════════════════════════════════════════════════════════════


def build_aggregator_prompt(
    *,
    depth: str = "auto",
    depth_taste: float = 0.70,
    intent: str | None = None,
    scene_hints: dict[str, str] | None = None,
    intent_strategies: dict[str, str] | None = None,
    memory_context: str = "",
) -> str:
    """组装 Aggregator System Prompt — v2 分离合成架构。

    Aggregator 是数据聚合引擎，不是人格化角色。
    - 不含 Character Card（属于 Render）
    - 不含 snark / initiative 语气（属于 Render）
    - 含搜索深度行为指令（来自 depth_taste）
    - 含 submit_facts_to_render 终止规则

    Args:
        depth: 深度模式（\"auto\" | \"quick\" | \"deep\"），控制字数上限。
        depth_taste: 搜索深度 0.0-1.0 (5 档)，控制工具调用策略。
        intent: 查询意图。
        scene_hints: Phase 7 简短场景提示 dict。
        intent_strategies: [deprecated] 旧意图策略 dict，向后兼容 fallback。
        memory_context: L2 记忆召回 + tone 提示的格式化文本。

    Returns:
        完整的 Aggregator System Prompt 字符串。
    """
    parts: list[str] = []

    # ── Section 1: Aggregator 身份 ──────────────────────────
    parts.append(_AGGREGATOR_IDENTITY)

    # ── Section 2: 搜索深度指令 ─────────────────────────────
    depth_instruction = get_aggregator_depth_instruction(depth_taste)
    parts.append(f"## 搜索深度\n{depth_instruction}")

    # ── Section 3: 工具指引 ─────────────────────────────────
    parts.append(TOOL_GUIDANCE)

    # ── Section 4: 对话连续性 ───────────────────────────────
    parts.append(_CONTINUITY_RULES)

    # ── Section 5: Scene Hint ───────────────────────────────
    hint = None
    if scene_hints and intent:
        hint = scene_hints.get(intent, scene_hints.get("unknown", ""))
    elif intent_strategies and intent:
        hint = intent_strategies.get(intent, intent_strategies.get("unknown", ""))
    if hint:
        parts.append(hint)

    # ── Section 6: Memory Context ────────────────────────────
    if memory_context:
        parts.append(memory_context)

    # ── Section 7: 输出约束 ─────────────────────────────────
    word_limit = _WORD_LIMITS.get(depth, _WORD_LIMITS["auto"])
    parts.append(f"## 输出约束\nfacts 中每条 summary 不超过 200 字。")

    # ── Section 8: 终止规则（必须放在最后——近因效应）─────────
    parts.append(_TERMINATION_RULES)

    return "\n\n".join(parts)


# ═══════════════════════════════════════════════════════════════════════════
# [DEPRECATED] build_system_prompt — 旧版统一 System Prompt Builder
#
# 在 v2 分离合成架构中已废弃。
# 人格内容（Character Card、snark、initiative）全部迁移至 Render 层。
# 新代码请使用 build_aggregator_prompt()。
#
# 保留此兼容包装以避免破坏旧调用路径（如测试、eval 脚本）。
# 计划在 Phase 2 清理时移除。
# ═══════════════════════════════════════════════════════════════════════════

from agent.persona.profiles import (
    AgentProfile,
    CharacterProfile,
    _render_tone,
    get_character_card,
)


def build_system_prompt(
    agent_profile: AgentProfile,
    character: CharacterProfile,
    *,
    depth: str = "auto",
    intent: str | None = None,
    intent_strategies: dict[str, str] | None = None,
    scene_hints: dict[str, str] | None = None,
    memory_context: str = "",
    snark: float | None = None,
    depth_taste: float | None = None,
    initiative: float | None = None,
) -> str:
    """[DEPRECATED v2] 旧版 System Prompt Builder。

    在分离合成架构中已由 build_aggregator_prompt() 替代。
    保留此函数以保证测试和旧调用路径的向后兼容。

    新代码请使用::

        from agent.orchestrate.prompt_builder import build_aggregator_prompt
        prompt = build_aggregator_prompt(depth_taste=0.70, depth="auto", intent="recommendation")
    """
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
        parts.append(f"# {character.identity}\n\n{character.motivation}")
        if character.expression_guide:
            parts.append(f"## 表达风格\n{character.expression_guide}")

    # ── Section 1.5: 今天的语气 ─────────────────────────────
    parts.append(f"## 今天的语气\n{tone_parts['tone']}")

    # ── Section 2: Capabilities + Tool Guidance ──────────────
    parts.append(agent_profile.capabilities)
    parts.append(TOOL_GUIDANCE)

    # ── Section 2.5: 输出格式 ────────────────────────────────
    if agent_profile.output_format_guide:
        parts.append(agent_profile.output_format_guide)

    # ── Section 3: Continuity Rules ──────────────────────────
    parts.append(_CONTINUITY_RULES)

    # ── Section 4: Scene Hint ───────────────────────────────
    hint = None
    if scene_hints and intent:
        hint = scene_hints.get(intent, scene_hints.get("unknown", ""))
    elif intent_strategies and intent:
        hint = intent_strategies.get(intent, intent_strategies.get("unknown", ""))
    if hint:
        parts.append(hint)

    # ── Section 5: Memory Context ────────────────────────────
    if memory_context:
        parts.append(memory_context)

    # ── Section 6: Guardrails ────────────────────────────────
    word_limit = _WORD_LIMITS.get(depth, _WORD_LIMITS["auto"])
    parts.append(character.guardrails.format(word_limit=word_limit))

    return "\n\n".join(parts)
