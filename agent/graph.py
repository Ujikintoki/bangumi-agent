"""
Companion Agent 图谱编排 — 统一 ReAct 拓扑

Phase 6: 合并 Research 和 Dialogue 两个 graph 为单一 StateGraph。
critic_node 仅 depth=="deep" 时条件注册。

核心拓扑
========

                   START
                     │
                     ▼
              reasoning_node ◄──────────────────┐
                     │                           │
                     ▼                           │
              ┌──────┼──────────┐                │
              │      │          │                │
         tool_node   │     chitchat              │
              │      │     (快速通道)             │
              │      │          │                │
              │  depth=="deep"?  END             │
              │   ┌──┴──┐                        │
              │   │     │                        │
              │  YES    NO                       │
              │   │     │                        │
              │   ▼     ▼                        │
              │ critic  END                      │
              │   │                              │
              │   ▼                              │
              │ (PASS→END, REVISE→reasoning) ────┘
              │
              └──→ reasoning_node（消化工具结果）

决策矩阵
========

route_after_reasoning（原生消息路由，读 messages[-1]）:
    - AIMessage.tool_calls 非空 → tool_node → reasoning_node（消化结果）
    - intent = chitchat         → END（快速通道）
    - depth == "deep"           → critic_node
    - 其他                      → END

route_after_critic（仅 depth=="deep" 时有效）:
    - PASS               → END
    - REVISE + iter < 12 → reasoning_node（重试）
    - REVISE + iter >= 12→ END（熔断）
"""

from __future__ import annotations

import logging
from typing import Literal

from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from agent.guardrails import format_tool_error
from agent.nodes import critic_node, reasoning_node
from agent.state import AgentState, get_max_iterations
from tools.bgm_tools import get_agent_tools

logger = logging.getLogger("bgm-agent.graph")

_FAST_PATH_INTENTS = frozenset({"chitchat"})


# ── 条件路由: reasoning → tool / critic / END ──────────────


def route_after_reasoning(
    state: AgentState,
) -> Literal["tool_node", "critic_node", "__end__"]:
    """reasoning_node 后的条件边（原生消息路由）。

    四级路由（优先级从高到低）：
        1. AIMessage.tool_calls 非空 → tool_node
        2. query_intent = chitchat   → END（快速通道）
        3. depth == "deep"          → critic_node
        4. 其他                     → END
    """
    from langchain_core.messages import AIMessage

    messages = state.get("messages", [])
    last_msg = messages[-1] if messages else None
    has_tool_calls = (
        isinstance(last_msg, AIMessage)
        and hasattr(last_msg, "tool_calls")
        and last_msg.tool_calls
    )
    if has_tool_calls:
        logger.debug(
            "route_after_reasoning: tool_calls=%s → tool_node",
            [tc.get("name", "?") for tc in last_msg.tool_calls],
        )
        return "tool_node"

    query_intent = state.get("query_intent", "unknown")
    if query_intent in _FAST_PATH_INTENTS:
        logger.debug(
            "route_after_reasoning: intent=%s → 快速通道 END", query_intent
        )
        return END

    depth = state.get("depth", "auto")
    if depth == "deep":
        logger.debug(
            "route_after_reasoning: depth=deep intent=%s 无工具调用 → critic_node",
            query_intent,
        )
        return "critic_node"

    logger.debug(
        "route_after_reasoning: depth=%s intent=%s 无工具调用 → END",
        depth, query_intent,
    )
    return END


# ── 条件路由: critic → retry / END ──────────────────────────


def route_after_critic(state: AgentState) -> Literal["reasoning_node", "__end__"]:
    """critic_node 后的条件边。

    决策矩阵：
        +----------------+----------------+----------------+
        | critic_status  | iterations < N | iterations >= N |
        +================+================+================+
        | PASS           | → END          | → END          |
        +----------------+----------------+----------------+
        | REVISE         | → reasoning    | → END（强制）  |
        +----------------+----------------+----------------+
    """
    depth = state.get("depth", "deep")
    max_iterations = get_max_iterations(depth)
    iterations = state.get("iterations", 0)
    status = state.get("critic_status", "PENDING")

    if iterations >= max_iterations:
        logger.info("迭代次数已达上限 %d，强制终止", max_iterations)
        return END

    if status == "PASS":
        logger.info("自省通过 (iterations=%d)，结束图谱", iterations)
        return END

    logger.info("自省要求修正 (iterations=%d)，返回 reasoning_node", iterations)
    return "reasoning_node"


# ── 图谱构建 ──────────────────────────────────────────────


def build_graph(tools: list | None = None) -> StateGraph:
    """构建并编译 LangGraph 状态图。

    critic_node 始终注册到 graph 中（LangGraph 编译时节点必须存在），
    但实际运行时 depth!="deep" 的请求不会路由到 critic_node。

    Args:
        tools: LangChain 工具列表。None 时自动加载 ``get_agent_tools()``。

    Returns:
        编译后的 ``StateGraph`` 实例。
    """
    if tools is None:
        tools = get_agent_tools()

    graph = StateGraph(AgentState)

    # ── 注册节点 ──────────────────────────────────────────
    graph.add_node("reasoning_node", reasoning_node)
    graph.add_node("tool_node", ToolNode(tools, handle_tool_errors=format_tool_error))
    graph.add_node("critic_node", critic_node)

    # ── 固定边 ────────────────────────────────────────────
    graph.add_edge(START, "reasoning_node")
    graph.add_edge("tool_node", "reasoning_node")

    # ── 条件边 1: reasoning → tool / critic / END ──────────
    graph.add_conditional_edges(
        "reasoning_node",
        route_after_reasoning,
        {
            "tool_node": "tool_node",
            "critic_node": "critic_node",
            END: END,
        },
    )

    # ── 条件边 2: critic → retry / END ─────────────────────
    graph.add_conditional_edges(
        "critic_node",
        route_after_critic,
        {
            "reasoning_node": "reasoning_node",
            END: END,
        },
    )

    logger.info("Companion Agent 图谱编译完成（%d 个工具）", len(tools))
    return graph.compile()


# ── 模块级编译实例 ──────────────────────────────────────────

agent_app = build_graph()
"""预编译的 Companion Agent 图谱实例，可直接 ``agent_app.invoke(state)`` 调用。"""
