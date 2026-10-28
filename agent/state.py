"""
Bangumi Agent 统一状态定义

``depth`` 字段控制深度模式，``_MAX_ITERATIONS`` 按 depth 分支
只有 fast/deep 两种模式

使用 TypedDict 定义 AgentState，配合 Annotated[list, operator.add]
实现节点间消息的自动合并（追加而非覆盖），避免跨节点消息丢失。
"""

from __future__ import annotations

import operator
from typing import Annotated, Literal, NotRequired, TypedDict

from langchain_core.messages import BaseMessage

Depth = Literal["fast", "deep"]
"""深度模式：
- ``"fast"``（默认）：10000 tok 上下文预算，快速获取核心数据
- ``"deep"``：16000 tok 预算，更高迭代上限，深度链式调用

迭代上限按 intent 细分（``agent/config.py`` per-intent 表）；
5 轮 / 12 轮仅是旧的无 intent 兜底值。
"""


class TurnResult(TypedDict):
    """执行结局与任务验收分开；工具成功不能证明回答已经完成任务。"""

    status: Literal["pending", "success", "partial", "empty", "failed"]
    error_type: NotRequired[str | None]
    missing: list[str]
    sources: NotRequired[list[dict]]
    task_status: NotRequired[Literal["unverified", "incomplete"]]


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
        output_style: 输出渲染风格：neutral | bangumi | bangumi_kawaii
        depth: 深度模式：fast | deep。
    """

    session_context: NotRequired[str]
    """用户显式确认的会话内背景，以数据使用，不自动升级为长期记忆。"""

    messages: Annotated[list[BaseMessage], operator.add]
    """对话历史，使用 Annotated[list[BaseMessage], operator.add] 保证节点间追加语义。"""

    iterations: int
    """当前循环轮次，从 0 开始计数。"""

    query_intent: str
    """查询 Intent 分类（v4: 7 intent）：chat | fetch | explore | discuss | profile | realtime | fallback。

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
    """输出渲染风格：neutral | bangumi | bangumi_kawaii。"""

    depth: str
    """深度模式：fast | deep。控制迭代上限和 token 预算。"""

    turn_result: NotRequired[TurnResult]
    """本轮确定性结局。至少包含 status；失败时只保留脱敏 error_type。"""
