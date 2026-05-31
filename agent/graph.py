"""
LangGraph 图谱编排

核心拓扑：reasoning → (条件边: needs_tool?) → tool/critic → (条件边) → END/retry

两条条件边确保：
  1. **route_after_reasoning**: 无工具意图时跳过 tool_node，避免"你好"
     也触发 Bangumi 查询的灾难性资源浪费。
  2. **route_after_critic**: 自省通过即结束、未通过则重试、超限强制熔断。
"""

from __future__ import annotations

import logging
from typing import Literal

from langgraph.graph import END, START, StateGraph

from agent.nodes import critic_node, reasoning_node, tool_node
from agent.state import AgentState

logger = logging.getLogger("bgm-agent.graph")

# 最大允许的迭代轮次，超过此值强制终止以防无限递归
_MAX_ITERATIONS = 3


# ── 条件路由: reasoning → tool / critic ──────────────────────


def route_after_reasoning(state: AgentState) -> Literal["tool_node", "critic_node"]:
    """reasoning_node 之后的条件边：依据 needs_tool 决定是否调用工具。

    这是避免"你好"类请求浪费 Token 和数据库查询的关键分叉：
        - needs_tool=True  → tool_node → critic_node
        - needs_tool=False → 跳过工具，直达 critic_node

    Args:
        state: 当前 Agent 全局状态。

    Returns:
        ``"tool_node"`` 或 ``"critic_node"``。
    """
    if state.get("needs_tool", False):
        logger.debug("route_after_reasoning: needs_tool=True → tool_node")
        return "tool_node"
    logger.debug("route_after_reasoning: needs_tool=False → critic_node（跳过工具）")
    return "critic_node"


# ── 条件路由: critic → retry / END ───────────────────────────


def route_after_critic(state: AgentState) -> Literal["reasoning_node", "__end__"]:
    """自省节点后的条件边路由逻辑。

    决策矩阵：

        +----------------+----------------+----------------+
        | critic_status  | iterations < 3 | iterations >= 3|
        +================+================+================+
        | PASS           | → END          | → END          |
        +----------------+----------------+----------------+
        | REVISE         | → reasoning    | → END（强制）  |
        +----------------+----------------+----------------+

    当 ``iterations >= 3`` 时无论 critic_status 为何，均强制走向
    ``END``，与 ``critic_node`` 的 ``error_flag=True`` 配合形成
    双层熔断保护。

    Args:
        state: 当前 Agent 全局状态。

    Returns:
        ``"reasoning_node"`` 回到推理节点重试，或 ``"__end__"`` 结束图谱。
    """
    iterations = state.get("iterations", 0)
    status = state.get("critic_status", "PENDING")

    # 熔断保护：超过最大迭代次数直接终止
    if iterations >= _MAX_ITERATIONS:
        logger.info("迭代次数已达上限 %d，强制终止", _MAX_ITERATIONS)
        return END

    if status == "PASS":
        logger.info("自省通过 (iterations=%d)，结束图谱", iterations)
        return END

    # status == "REVISE" 且 iterations < MAX
    logger.info("自省要求修正 (iterations=%d)，返回 reasoning_node", iterations)
    return "reasoning_node"


# ── 图谱构建 ──────────────────────────────────────────────────


def build_graph() -> StateGraph:
    """构建并编译 LangGraph 状态图。

    图谱拓扑::

                    START
                      │
                      ▼
               reasoning_node
                      │
                      ▼ (条件边: needs_tool?)
               ┌──────┴──────┐
               │             │
            tool_node     (skip)
               │             │
               └──────┬──────┘
                      ▼
                critic_node
                      │
                      ▼ (条件边: PASS? 超限?)
               ┌──────┴──────┐
               │             │
              END      reasoning_node
                       (REVISE + 未超限)

    Returns:
        编译后的 ``StateGraph`` 实例，可直接调用 ``.invoke()``。
    """
    graph = StateGraph(AgentState)

    # ── 注册节点 ──────────────────────────────────────────
    graph.add_node("reasoning_node", reasoning_node)
    graph.add_node("tool_node", tool_node)
    graph.add_node("critic_node", critic_node)

    # ── 固定边 ────────────────────────────────────────────
    graph.add_edge(START, "reasoning_node")
    graph.add_edge("tool_node", "critic_node")

    # ── 条件边 1: reasoning → tool 或跳过工具直达 critic ──
    graph.add_conditional_edges(
        "reasoning_node",
        route_after_reasoning,
        {
            "tool_node": "tool_node",
            "critic_node": "critic_node",
        },
    )

    # ── 条件边 2: critic → retry 或 END ───────────────────
    graph.add_conditional_edges(
        "critic_node",
        route_after_critic,
        {
            "reasoning_node": "reasoning_node",
            END: END,
        },
    )

    logger.info("Agent 图谱编译完成")
    return graph.compile()


# ── 模块级编译实例 ──────────────────────────────────────────

agent_app = build_graph()
"""预编译的 Agent 图谱实例，可直接 ``agent_app.invoke(state)`` 调用。"""
