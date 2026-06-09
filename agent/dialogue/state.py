"""
Dialogue Agent 状态定义

极简 State：5 字段，无 Critic 相关字段（critic_status / critic_feedback / error_flag）。
最大 3 轮迭代（vs Research 的 10），速度 > 准确。
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage


class DialogueState(TypedDict):
    """Dialogue Agent 全局状态。

    比 Research AgentState 少 3 个字段：无 critic_status、critic_feedback、error_flag。
    Dialogue Agent 不做质量自省——模型自主判断输出质量。

    Attributes:
        messages: 对话历史。Annotated[list, operator.add] 保证追加语义。
        iterations: 当前轮次。每轮推理 +1，用于熔断控制（上限 3）。
        query_intent: 意图分类结果（复用 agent.classifier 的 6 类）。
        session_id: 会话标识（预留）。
        user_id: 用户标识（预留）。
    """

    messages: Annotated[list[BaseMessage], operator.add]
    """对话历史，使用 operator.add 保证节点间追加语义。"""

    iterations: int
    """当前循环轮次，从 0 开始计数。"""

    query_intent: str
    """查询意图：chitchat | factual | lookup | discovery | realtime | unknown。"""

    session_id: str
    """会话 ID（Layer 2 预留）。"""

    user_id: str
    """用户 ID（Layer 3 预留）。"""


# ── Agent 全局常量 ────────────────────────────────────────────────────

_MAX_ITERATIONS = 3
"""最大 ReAct 迭代轮次。Dialogue Agent 追求速度，3 轮足够完成
search → detail 的串行依赖链。"""
