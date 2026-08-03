"""
Bangumi Agent 统一状态定义

``depth`` 字段控制深度模式，``_MAX_ITERATIONS`` 按 depth 分支
分为 quick / auto / deep 三种模式，分别对应 3 / 5 / 12 轮迭代上限。

使用 TypedDict 定义 AgentState，配合 Annotated[list, operator.add]
实现节点间消息的自动合并（追加而非覆盖），避免跨节点消息丢失。
"""

from __future__ import annotations

import operator
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import BaseMessage

Depth = Literal["fast", "deep"]
"""深度模式：
- ``"fast"``（默认）：轻量 ReAct ≤5 轮，10000 tok，快速获取核心数据
- ``"deep"``：高预算（16000 tok）+ 深度人格参数，12 轮迭代上限，深度链式调用
"""


class AgentState(TypedDict):
    """Agent 全局状态，在 LangGraph 节点间流转。

    Attributes:
        messages: 对话历史列表。使用 ``operator.add`` 作为 reducer，
            节点返回的新消息会自动追加到现有列表末尾，而非覆盖。
        iterations: 当前 ReAct 循环次数。每轮推理 +1，用于熔断控制。
        query_intent: 查询意图分类结果，由 reasoning_node 首轮设置。
        session_id: 会话标识（L1 多轮上下文缓存）。(未来可考虑优化)
        user_id: 用户标识（L2 跨会话记忆）。(未来可考虑优化)
        error_flag: 降级标记。底层组件异常或循环超限时置 True。
        _memory_context: 首轮 L2 记忆召回缓存。None 表示未初始化，空字符串表示已召回但无记忆。
        output_style: 输出渲染风格：neutral | bangumi | bangumi_cold | bangumi_cute
        depth: 深度模式：auto | quick | deep。
    """

    messages: Annotated[list[BaseMessage], operator.add]
    """对话历史，使用 Annotated[list[BaseMessage], operator.add] 保证节点间追加语义。"""

    iterations: int
    """当前循环轮次，从 0 开始计数。"""

    # [DEPRECATED Phase 10] critic_status/critic_feedback 已从 AgentState 移除。
    # Critic 节点已从图谱中移除，这两个字段不再被任何节点写入。
    # 保留注释以备未来恢复 Critic 时重新激活。
    # critic_status: str
    # critic_feedback: str

    query_intent: str
    """查询 Intent 分类（v4: 4→6）：chat | fetch | explore | discuss | realtime | fallback。

    旧值 (chitchat/lookup/discovery) 仍被接受以实现向后兼容。
    """

    classifier_confidence: float | None
    """分类器置信度（0.0-1.0）。None 表示未初始化或使用旧分类器。"""

    session_id: str
    """会话 ID（L1 多轮上下文缓存）。"""

    user_id: str
    """用户 ID（L2 跨会话记忆）。"""

    error_flag: bool
    """降级标记，默认 False。"""

    _memory_context: str | None
    """首轮 L2 记忆召回缓存。None 表示未初始化，空字符串表示已召回但无记忆。"""

    output_style: str
    """输出渲染风格：neutral | bangumi | bangumi_cold | bangumi_cute。"""

    depth: str
    """深度模式：auto | quick | deep。控制迭代上限、执行计划。"""


# ── Depth-dependent max iterations ────────────────────────────────────

_MAX_ITERATIONS_FAST = 5
"""fast 模式最大迭代轮次（旧，无 intent 参数时的兜底值）。"""

_MAX_ITERATIONS_DEEP = 12
"""deep 模式最大迭代轮次（旧，无 intent 参数时的兜底值）。"""

# ── Per-intent max iterations（v4: 6 intent）─────────────────────────

_INTENT_MAX_ITERATIONS: dict[str, int] = {
    "chat": 0,         # 不走工具循环
    "fetch": 2,         # search → detail → 停
    "explore": 3,       # search → multi-detail → 停
    "discuss": 4,       # search → detail → comments → 停
    "realtime": 2,      # calendar/trending → 停
    "fallback": 2,      # 同 fetch，保守
    # 向后兼容旧 intent
    "chitchat": 0,
    "lookup": 2,
    "discovery": 3,
}

_INTENT_DEEP_OVERRIDES: dict[str, int] = {
    "explore": 5,
    "discovery": 5,     # 旧 intent 别名
    "discuss": 6,
}


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
