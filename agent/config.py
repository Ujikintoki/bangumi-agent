"""
Agent 配置 — 迭代上限、置信度阈值等运行时参数。

从 ``state.py`` 和 ``graph.py`` 中提取，与 state schema 分离。
"""

from __future__ import annotations

# ── Depth-dependent max iterations ────────────────────────────────────

_MAX_ITERATIONS_FAST = 5
"""fast 模式最大迭代轮次（旧，无 intent 参数时的兜底值）。"""

_MAX_ITERATIONS_DEEP = 12
"""deep 模式最大迭代轮次（旧，无 intent 参数时的兜底值）。"""

# ── Per-intent max iterations（v4: 7 intent + 3 个旧别名）─────────────

_INTENT_MAX_ITERATIONS: dict[str, int] = {
    "chat": 0,  # 不走工具循环
    "fetch": 3,  # search → detail → synthesize
    "explore": 3,  # search → multi-detail → 停
    "discuss": 4,  # search → detail → comments → 停
    "profile": 2,  # user_profile → user_timeline → 停
    "realtime": 2,  # calendar/trending → 停
    "fallback": 2,  # 同 fetch，保守
    # 向后兼容旧 intent
    "chitchat": 0,
    "lookup": 2,
    "discovery": 3,
}

_INTENT_DEEP_OVERRIDES: dict[str, int] = {
    "explore": 5,
    "discovery": 5,  # 旧 intent 别名
    "discuss": 6,
}

# ── 置信度阈值：低于此值不进 pipeline，走 ReAct fallback ─────────────

_PIPELINE_CONFIDENCE_THRESHOLD = 0.7


def get_max_iterations(depth: str, intent: str | None = None) -> int:
    """按 depth 和 intent 返回最大迭代轮次。

    Args:
        depth: 深度模式（"fast" | "deep"）。
        intent: 查询意图（6 intent 之一或旧 intent 值）。
                为 None 时使用旧 depth-only 兜底值。

    Returns:
        对应模式的最大迭代轮次。chat intent 返回 0（不进入工具循环）。
    """
    if intent is not None and intent in _INTENT_MAX_ITERATIONS:
        base = _INTENT_MAX_ITERATIONS[intent]
        if depth == "deep" and intent in _INTENT_DEEP_OVERRIDES:
            return _INTENT_DEEP_OVERRIDES[intent]
        return base
    # 向后兼容：无 intent 参数时使用旧逻辑
    if depth == "deep":
        return _MAX_ITERATIONS_DEEP
    return _MAX_ITERATIONS_FAST
