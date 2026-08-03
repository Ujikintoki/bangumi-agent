"""
Bangumi Agent 图谱编排 — v2 纯 ReAct 拓扑

  fast / deep 两种深度模式共享同一张图，差异仅在参数：
  - fast: 5 轮上限、10000 tok
  - deep: 12 轮上限、16000 tok

  START → reasoning_node ⇄ tool_node → END

Render 在 main.py 后处理，与路径无关。
"""

from __future__ import annotations

import logging
from typing import Literal

from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from agent.orchestrate.guardrails import format_tool_error
from agent.orchestrate.nodes import reasoning_node
from agent.state import AgentState, get_max_iterations
from tools.bgm_tools import get_agent_tools

logger = logging.getLogger("bgm-agent.graph")


# ── 条件路由: tool_node → reasoning / END ───────────────────


def route_after_tool(
    state: AgentState,
) -> Literal["reasoning_node", "__end__"]:
    """tool_node 后的条件边。

    1. 硬熔断：iterations >= max → END
    2. submit_facts_to_render → END
    3. 其他 → reasoning_node（继续 ReAct 循环）
    """
    from langchain_core.messages import ToolMessage

    # 硬熔断
    depth = state.get("depth", "fast")
    max_iter = get_max_iterations(depth)
    current_iter = state.get("iterations", 0)
    if current_iter >= max_iter:
        logger.warning(
            "route_after_tool: 硬熔断！iter=%d >= max=%d (depth=%s) → END",
            current_iter, max_iter, depth,
        )
        return END

    # submit_facts 检测
    messages = state.get("messages", [])
    for m in reversed(messages):
        if isinstance(m, ToolMessage):
            if getattr(m, "name", "") == "submit_facts_to_render":
                logger.info("route_after_tool: submit_facts_to_render → END")
                return END
            break

    # 继续 ReAct
    return "reasoning_node"


# ── 条件路由: reasoning → tool / END ────────────────────────


def route_after_reasoning(
    state: AgentState,
) -> Literal["tool_node", "__end__"]:
    """reasoning_node 后的条件边。

    AIMessage.tool_calls 非空 → tool_node；否则 → END。
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

    logger.debug(
        "route_after_reasoning: depth=%s action=%s → END",
        state.get("depth", "fast"),
        state.get("query_intent", "unknown"),
    )
    return END


# ── 图谱构建 ────────────────────────────────────────────────


def build_graph(tools: list | None = None) -> StateGraph:
    """构建并编译 LangGraph 状态图。v2 纯 ReAct 拓扑。

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

    # ── 固定边 ────────────────────────────────────────────
    graph.add_edge(START, "reasoning_node")

    # ── reasoning_node → tool / END ───────────────────────
    graph.add_conditional_edges(
        "reasoning_node",
        route_after_reasoning,
        {
            "tool_node": "tool_node",
            END: END,
        },
    )

    # ── tool_node → reasoning / END ───────────────────────
    graph.add_conditional_edges(
        "tool_node",
        route_after_tool,
        {
            "reasoning_node": "reasoning_node",
            END: END,
        },
    )

    logger.info("Bangumi Agent 图谱编译完成 v2（%d 个工具）", len(tools))
    return graph.compile()


# ── 模块级编译实例 ──────────────────────────────────────────

agent_app = build_graph()
"""预编译的 Bangumi Agent 图谱实例，v2 纯 ReAct 拓扑。"""
