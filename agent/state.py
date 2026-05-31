"""
LangGraph Agent 状态定义

使用 TypedDict 定义 AgentState，配合 Annotated[list, operator.add]
实现节点间消息的自动合并（追加而非覆盖），避免跨节点消息丢失。
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict


class AgentState(TypedDict):
    """Agent 全局状态，在 LangGraph 节点间流转。

    本骨架不接入真实 LLM 或 RAG，所有字段仅用于验证
    图谱控制流与优雅降级机制的完整性。

    Attributes:
        messages: 对话历史列表。使用 ``operator.add`` 作为 reducer，
            节点返回的新消息会自动追加到现有列表末尾，而非覆盖。
        iterations: 当前 ReAct 循环次数。每轮推理 +1，用于熔断控制。
        critic_status: 自省节点的判定结果。
            ``"PENDING"`` — 尚未评估；
            ``"PASS"``   — 输出合格，可结束；
            ``"REVISE"`` — 需修正，回到推理节点重试。
        error_flag: 优雅降级标记。当底层组件异常或循环超限时置为
            ``True``，通知下游走兜底路径而非崩溃。
    """

    messages: Annotated[list, operator.add]
    """对话历史，使用 Annotated[list, operator.add] 保证节点间追加语义。"""

    iterations: int
    """当前循环轮次，从 0 开始计数。"""

    critic_status: str
    """自省判定：PENDING / PASS / REVISE。"""

    needs_tool: bool
    """是否需要调用工具。由 reasoning_node 根据用户意图判定。
    ``True`` 时经条件边进入 tool_node；``False`` 时跳过工具直达 critic_node。
    """

    error_flag: bool
    """优雅降级标记，默认 False。置 True 时 reasoning_node 进入兜底模式。"""
