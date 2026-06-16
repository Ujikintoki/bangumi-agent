"""
Research Agent 状态定义

使用 TypedDict 定义 AgentState，配合 Annotated[list, operator.add]
实现节点间消息的自动合并（追加而非覆盖），避免跨节点消息丢失。

消息类型从 ``list[str]`` 升级为 ``list[BaseMessage]``，
新增意图分类、Critic 定向反馈、会话/用户标识字段。
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage


class AgentState(TypedDict):
    """Agent 全局状态，在 LangGraph 节点间流转。

    Attributes:
        messages: 对话历史列表。使用 ``operator.add`` 作为 reducer，
            节点返回的新消息会自动追加到现有列表末尾，而非覆盖。
            元素类型为 LangChain ``BaseMessage``（HumanMessage / AIMessage / ToolMessage / SystemMessage）。
        iterations: 当前 ReAct 循环次数。每轮推理 +1，用于熔断控制。
        critic_status: 自省节点的判定结果。
            ``"PENDING"`` — 尚未评估；
            ``"PASS"``   — 输出合格，可结束；
            ``"REVISE"`` — 需修正，回到推理节点重试。
        critic_feedback: Critic 的具体改进建议。
            PASS 时为确认描述，REVISE 时为 ``"<缺陷> | <建议> | <缺失>"`` 格式的定向反馈。
            下一轮 reasoning_node 将其注入 prompt 以指导 LLM 定向修正。
        query_intent: 查询意图分类结果，由 reasoning_node 内部分类器设置。
            ``"chitchat" | "factual" | "lookup" | "discovery" | "realtime" | "unknown"``。
            影响 prompt 变体选择，不直接参与路由决策。
        session_id: 会话标识（Lay2 会话记忆预留）。由 /chat 端点传入。
        user_id: 用户标识（Lay3 用户画像预留）。由 /chat 端点传入。
        error_flag: 降级标记。当底层组件异常或循环超限时置为
            ``True``，通知下游走兜底路径而非崩溃。
    """

    messages: Annotated[list[BaseMessage], operator.add]
    """对话历史，使用 Annotated[list[BaseMessage], operator.add] 保证节点间追加语义。"""

    iterations: int
    """当前循环轮次，从 0 开始计数。"""

    critic_status: str
    """自省判定：PENDING / PASS / REVISE。"""

    critic_feedback: str
    """Critic 的具体改进建议。REVISE 时为定向反馈，下一轮注入 reasoning prompt。"""

    query_intent: str
    """查询意图分类：chitchat | factual | lookup | discovery | realtime | unknown。"""

    session_id: str
    """会话 ID（Layer 2 预留）。"""

    user_id: str
    """用户 ID（Layer 3 预留）。"""

    error_flag: bool
    """降级标记，默认 False。置 True 时 reasoning_node 进入兜底模式。"""

    _memory_context: str
    """首轮 L2 记忆召回缓存。空字符串表示未召回或无需召回。
    设置后在同一 graph 调用的后续轮次（工具消化、REVISE 重入）中复用，
    避免重复 embedding + pgvector 检索。由 reasoning_node 首轮填充。"""


# ── Agent 全局常量 ────────────────────────────────────────────────────

_MAX_ITERATIONS = 12
"""最大 ReAct 迭代轮次。graph 条件边和 critic 节点都引用此值做熔断。"""
