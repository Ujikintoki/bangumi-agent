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

Depth = Literal["auto", "quick", "deep"]
"""深度模式：
- ``"auto"``（默认）：LLM 自行判断，轻量 ReAct ≤5 轮，无 Critic
- ``"quick"``：强制浅层 1-3 轮，无 Critic
- ``"deep"``：高预算（16000 tok）+ 深度人格参数，12 轮迭代上限
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
        _memory_context: 首轮 L2 记忆召回缓存。空字符串表示未召回。（未来可考虑优化）
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
    """查询意图分类：chitchat | factual | lookup | discovery | realtime | unknown。"""

    session_id: str
    """会话 ID（L1 多轮上下文缓存）。"""

    user_id: str
    """用户 ID（L2 跨会话记忆）。"""

    error_flag: bool
    """降级标记，默认 False。置 True 时 reasoning_node 进入兜底模式。"""

    _memory_context: str
    """首轮 L2 记忆召回缓存。空字符串表示未召回或无需召回。"""

    output_style: str
    """输出渲染风格：neutral | bangumi | bangumi_cold | bangumi_cute。控制 System Prompt 中风格附录的注入。"""

    depth: str
    """深度模式：auto | quick | deep。控制迭代上限、工具策略。"""


# ── Depth-dependent max iterations ────────────────────────────────────

_MAX_ITERATIONS_QUICK = 3
"""quick 模式最大迭代轮次。强制浅层，1-3 轮覆盖日常场景。"""

_MAX_ITERATIONS_DEFAULT = 5
"""auto/默认模式最大迭代轮次。轻量 ReAct，≤5 轮覆盖 95% 场景。"""

_MAX_ITERATIONS_DEEP = 12
"""deep 模式最大迭代轮次。Research Skill 激活，深度链式调用 + Critic。"""


def get_max_iterations(depth: str) -> int:
    """按 depth 返回最大迭代轮次。

    Args:
        depth: 深度模式（"auto" | "quick" | "deep"）。

    Returns:
        对应模式的最大迭代轮次。
    """
    if depth == "deep":
        return _MAX_ITERATIONS_DEEP
    if depth == "quick":
        return _MAX_ITERATIONS_QUICK
    return _MAX_ITERATIONS_DEFAULT
