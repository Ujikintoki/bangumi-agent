"""
Tool Configuration — Per-intent 工具子集 + tool_choice 策略

从 ``orchestrate/prompt_builder.py`` 提取。
"""

from __future__ import annotations

# ═══════════════════════════════════════════════════════════════════════════
# Tool Guidance — 工具使用指引
# ═══════════════════════════════════════════════════════════════════════════

TOOL_GUIDANCE = """\
## 你的工具

**什么时候查**
- 用户问到了你不知道的 → 查一下
- 用户没问到的 → 不主动扩展（除非搜索深度指令要求）
- 常识问题 → 基于搜索深度指令判断是否需要查

**多少算够**
- 搜索深度（调多少工具、取多少数据）遵循下方搜索深度指令——不同场景深度不同
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
# v4: Per-intent 工具子集
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
        "search_local_bangumi",
    ],
    "discuss": [
        "search_bangumi_subject", "get_bangumi_subject_detail",
        "get_person_detail", "get_character_detail",
        "get_subject_opinions", "get_subject_characters",
        "get_subject_episodes", "get_entity_comments",
        "get_episode_comments",
    ],
    "profile": [
        "get_user_profile", "get_user_timeline",
    ],
    "realtime": [
        "get_calendar", "get_trending_subjects",
        "get_hot_topics",
    ],
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
