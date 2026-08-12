"""
Tool Configuration — Per-intent 工具子集 + tool_choice 策略

Prompt 文本（TOOL_GUIDANCE）已归位到 ``agent/prompts/aggregator.py``。
本文件仅保留工具绑定逻辑。
"""

from __future__ import annotations

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
