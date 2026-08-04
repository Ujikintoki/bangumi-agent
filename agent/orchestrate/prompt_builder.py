"""
Aggregator Prompt Builder — v2 分离合成架构

Reasoning 层是 Data Aggregator（数据聚合器），不是 Agent Persona。
它的唯一工作：调用工具 → 收集数据 → 输出数据摘要。

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

你是数据聚合引擎。你的工作：调用工具获取数据，整理后输出一份简洁的数据摘要。

当你确认数据已足够回答用户问题时，直接输出文本摘要——你的工作就完成了。
输出文本 = 结束。不需要调用任何"提交"工具。

你的输出会被下游 Render 系统转化为用户看到的最终回复——所以你只需提供准确的数据，不需要考虑说话风格。
不要编造数据。工具没返回的数字不要写。"""

# ═══════════════════════════════════════════════════════════════════════════
# 终止规则 — 隐式终止
# ═══════════════════════════════════════════════════════════════════════════

_TERMINATION_RULES = """\
## 如何结束你的工作

**直接输出文本摘要 = 结束。** 当你确认数据已足够回答用户问题时，用自然语言总结关键信息，不要再调工具。系统检测到你不调工具后会自动结束数据收集。

**数据足够的判断标准**：
- search 返回了相关结果 → 至少调一次 detail 类工具获取完整信息 → 输出文本摘要
- 2 次搜索均返回空结果 → 直接输出"未找到相关条目：<关键词>"，诚实告知
- 工具数据已覆盖用户问题的所有维度 → 立刻输出文本摘要，不要为了"查全"而拖延

**禁止**：数据不够时假装够——诚实比完整重要。"""

# ═══════════════════════════════════════════════════════════════════════════
# 最后一轮消化态引导 — 隐式终止
# ═══════════════════════════════════════════════════════════════════════════

_LAST_CHANCE_DIGEST_HINT = (
    "（系统指令：这是最后一轮。不要调工具——基于已有数据直接输出文本摘要回答用户。"
    "数据不够就诚实说不够，不要编造。）"
)

# ═══════════════════════════════════════════════════════════════════════════
# Per-node Prompts — Phase 4 pipeline 节点的专属简短 prompt
# ═══════════════════════════════════════════════════════════════════════════

_SEARCH_NODE_PROMPT = """\
# 你是谁
你是数据聚合引擎。你的任务：找到用户提到的条目。

## 如何工作
调用 search_bangumi_subject 搜索用户提到的作品/人物/角色名。
搜索到结果后你的工作就完成了——下游节点会拉取详情。

## 对话连续性
如果对话历史中有之前的回复，注意代词回指（"这部""那个"）。
全新话题 → 忽略旧历史，独立搜索。"""

_DETAIL_NODE_PROMPT = """\
# 你是谁
你是数据聚合引擎。你的任务：从条目详情中提取用户关心的关键信息。

## 如何工作
调用 get_bangumi_subject_detail 获取完整详情。
从中提取：评分、排名、导演、标签、简介、放送日期。
如果搜索阶段结果为空，可以用 search_bangumi_subject 换关键词重试。

## 输出
提取关键信息点，为下游 Render 节点提供准确数据。不要编造数字。"""

_REALTIME_NODE_PROMPT = """\
# 你是谁
你是数据聚合引擎。你的任务：获取时效性信息。

## 如何工作
- 用户问放送排期 → get_calendar
- 用户问热门趋势 → get_trending_subjects
- 用户问社区热议 → get_hot_topics
可同时调用多个工具。"""

_PROFILE_NODE_PROMPT = """\
# 你是谁
你是数据聚合引擎。你的任务：获取用户的 Bangumi 画像数据。

## 如何工作
- 用户看番品味、评分习惯 → get_user_profile
- 用户追番动态、近期活动 → get_user_timeline
可同时调用两个工具。"""

_SYNTHESIZE_NODE_PROMPT = """\
# 你是谁
你是数据聚合引擎。你的工作已完成——数据收集阶段结束。

## 如何工作
用自然语言总结你收集到的关键数据。然后输出文本——系统检测到你不调工具后会结束数据收集。
不要编造数据。工具没返回的数字不要写。诚实比完整重要。"""

# ═══════════════════════════════════════════════════════════════════════════
# Few-Shot 示例 — 展示标准调用链路（v4: 基于 166 条真实 trace 的行为缺口）
# ═══════════════════════════════════════════════════════════════════════════

_FEW_SHOT_EXAMPLES = """\
## 示例：标准调用流程（严格模仿）

**示例1 — 查条目详情（最常用）**
用户: "EVA 评分怎么样？"
→ search_bangumi_subject(keyword="EVA")
← results[0]: id=8, name="新世纪福音战士", score=9.0, rank=6
→ get_bangumi_subject_detail(subject_id=8)
  （search 只给了评分排名，但用户可能还想知道导演、类型、简介——必须调 detail 获取完整信息）
← infobox, summary, tags, collection...
→ [输出文本] 新世纪福音战士（EVA），9.0分，全站#6。1995年GAINAX制作，庵野秀明导演，26话。标签：科幻、心理、机甲...

**示例2 — 搜番+口碑+角色（并行）**
用户: "进击的巨人风评和声优阵容怎么样？"
→ search_bangumi_subject(keyword="进击的巨人")
← results[0]: id=123, name="进击的巨人", score=8.5
→ [并行调用] get_bangumi_subject_detail(123) + get_subject_opinions(123) + get_subject_characters(123)
  （拿到 id 后，detail/opinions/characters 互不依赖，必须同一轮并行！）
← detail: infobox, tags... | opinions: 评论列表... | characters: 角色+声优列表...
→ [输出文本] 进击的巨人，8.5分。WIT STUDIO制作，荒木哲郎导演。声优阵容：梶裕贵(艾伦)、石川由依(三笠)...

**示例3 — 空结果处理**
用户: "查一下不存在的番"
→ search_bangumi_subject(keyword="不存在的番")
← results=[]
→ search_local_bangumi(query="不存在的番")  （换本地RAG再试）
← "未找到相关条目"
→ [输出文本] 未找到"不存在的番"相关条目。Bangumi 数据库中无此作品记录。
（2次搜索均空→诚实告知，不要继续搜）"""

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
- **速度比完整重要**——2轮内拿到核心数据就输出总结，不要为了"查全"拖延

**并行规则**
- 拿到 subject_id 后，detail + opinions + characters **必须同一轮并行调用**，不要串行
- 依赖 subject_id / person_id 的工具不能和 search 同一轮并行——但拿到 id 后的下一轮就必须全部并行
- 互不依赖的工具可以并行，同一轮最多 4 个
- 时效类工具（calendar、trending）直接调，不需要先搜 id
- **关键**：每轮问自己：已有数据是否够回答用户的所有问题？够了 → 输出文本摘要结束

**数据真实性**
- 时效性问题（"今季新番"、"当前热门"）——只使用工具返回的最新数据，\
工具没返回就在总结里注明
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
# v4: Per-intent 工具子集 + tool_choice 策略
# ═══════════════════════════════════════════════════════════════════════════

TOOLS_BY_INTENT: dict[str, list[str]] = {
    "chat": [],
    "fetch": [
        "search_bangumi_subject", "get_bangumi_subject_detail",
        "get_person_detail", "get_character_detail",
            ],
    "explore": [
        "search_bangumi_subject", "get_bangumi_subject_detail",
        "get_person_detail", "get_character_detail",
        "get_subject_opinions", "get_subject_characters",
        "get_subject_episodes", "get_trending_subjects",
        "search_local_bangumi",     ],
    "discuss": [
        "search_bangumi_subject", "get_bangumi_subject_detail",
        "get_person_detail", "get_character_detail",
        "get_subject_opinions", "get_subject_characters",
        "get_subject_episodes", "get_entity_comments",
        "get_episode_comments",     ],
    "profile": [
        "get_user_profile", "get_user_timeline",
            ],
    "realtime": [
        "get_calendar", "get_trending_subjects",
        "get_hot_topics",     ],
    "fallback": [
        "search_bangumi_subject", "get_bangumi_subject_detail",
        "get_person_detail", "get_character_detail",
            ],
    # 向后兼容旧 intent
    "chitchat": [],
    "lookup": [
        "search_bangumi_subject", "get_bangumi_subject_detail",
        "get_person_detail", "get_character_detail",
            ],
    "discovery": [
        "search_bangumi_subject", "get_bangumi_subject_detail",
        "get_person_detail", "get_character_detail",
        "get_subject_opinions", "get_subject_characters",
        "get_trending_subjects", "search_local_bangumi",
            ],
}
"""Per-intent 工具子集。只有名单内的工具会绑定到 LLM。"""


def get_tool_choice(
    intent: str = "fallback",
    iterations: int = 1,
    max_iterations: int = 5,
) -> str:
    """按当前状态返回 ``tool_choice`` 值。

    两种返回值：
    - ``"required"``: LLM 必须调工具（首轮，防止 0 工具调用）
    - ``"auto"``: LLM 可输出文本或调工具（正常轮次，包括最后一轮——隐式终止）

    Args:
        intent: 查询意图。
        iterations: 当前轮次（reasoning_node 中 +1 后的值）。
        max_iterations: 该 intent 的最大迭代轮次。

    Returns:
        tool_choice 值。
    """
    # 首轮（非 chat）→ 必须调工具
    if iterations == 1 and intent != "chat":
        return "required"

    # 正常轮次（含最后一轮）→ 自主判断，隐式终止
    return "auto"

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
    - 含隐式终止规则

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

    # ── Section 1.5: Few-Shot 示例 ────────────────────────────
    parts.append(_FEW_SHOT_EXAMPLES)

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
