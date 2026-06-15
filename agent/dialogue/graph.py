"""
Dialogue Agent 图谱编排 — 2 节点 ReAct 拓扑

核心拓扑
========

              START
                │
                ▼
     dialogue_reasoning_node ◄──────────┐
                │                        │
                ▼                        │
         ┌──────┴──────┐                │
         │              │                │
    tool_calls      无工具调用           │
         │              │                │
         ▼              ▼                │
     tool_node        END                │
         │                               │
         └───────────────────────────────┘
              (固定边: tool → reasoning)

决策矩阵
========

route_after_dialogue_reasoning（原生消息路由）:
    - iterations >= 4         → END（熔断）
    - AIMessage.tool_calls 非空 → tool_node
    - 其他                     → END

对比 Research Agent: 无 critic_node 分支，条件边只有 2 路。
"""

from __future__ import annotations

import logging
from typing import Literal

from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from agent.dialogue.nodes import dialogue_reasoning_node
from agent.dialogue.state import _MAX_ITERATIONS, DialogueState
from agent.guardrails import format_tool_error
from tools.bgm_tools import get_agent_tools

logger = logging.getLogger("bgm-agent.dialogue.graph")


# ── 条件路由: reasoning → tool / END ────────────────────────


def route_after_dialogue_reasoning(
    state: DialogueState,
) -> Literal["tool_node", "__end__"]:
    """dialogue_reasoning_node 后的条件边。

    二级路由（优先级从高到低）：
        1. iterations >= _MAX_ITERATIONS → END（熔断）
        2. AIMessage.tool_calls 非空    → tool_node
        3. 其他                         → END

    Args:
        state: 当前 Dialogue Agent 全局状态。

    Returns:
        ``"tool_node"`` 或 ``"__end__"``。
    """
    from langchain_core.messages import AIMessage

    iterations = state.get("iterations", 0)
    if iterations >= _MAX_ITERATIONS:
        logger.info(
            "dialogue route: iterations=%d >= %d → 熔断 END",
            iterations,
            _MAX_ITERATIONS,
        )
        return END

    messages = state.get("messages", [])
    last_msg = messages[-1] if messages else None
    has_tool_calls = (
        isinstance(last_msg, AIMessage)
        and hasattr(last_msg, "tool_calls")
        and last_msg.tool_calls
    )

    if has_tool_calls:
        logger.debug(
            "dialogue route: tool_calls=%s → tool_node",
            [tc.get("name", "?") for tc in last_msg.tool_calls],
        )
        return "tool_node"

    logger.debug("dialogue route: 无工具调用 → END")
    return END


# ── 图谱构建 ──────────────────────────────────────────────


def build_dialogue_graph(tools: list | None = None) -> StateGraph:
    """构建并编译 Dialogue Agent 的 LangGraph 状态图。

    Args:
        tools: LangChain 工具列表。None 时自动加载 ``get_agent_tools()``。

    Returns:
        编译后的 ``StateGraph`` 实例。
    """
    if tools is None:
        tools = get_agent_tools()

    graph = StateGraph(DialogueState)

    # ── 注册节点 ──────────────────────────────────────────
    graph.add_node("dialogue_reasoning_node", dialogue_reasoning_node)
    graph.add_node("tool_node", ToolNode(tools, handle_tool_errors=format_tool_error))

    # ── 固定边 ────────────────────────────────────────────
    graph.add_edge(START, "dialogue_reasoning_node")
    graph.add_edge("tool_node", "dialogue_reasoning_node")

    # ── 条件边: reasoning → tool / END ─────────────────────
    graph.add_conditional_edges(
        "dialogue_reasoning_node",
        route_after_dialogue_reasoning,
        {
            "tool_node": "tool_node",
            END: END,
        },
    )

    logger.info("Dialogue Agent 图谱编译完成（%d 个工具）", len(tools))
    return graph.compile()


# ── 模块级编译实例 ──────────────────────────────────────────

dialogue_app = build_dialogue_graph()
"""预编译的 Dialogue Agent 图谱实例，可直接 ``dialogue_app.invoke(state)`` 调用。"""
