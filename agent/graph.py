"""
Companion Agent 图谱编排 — 纯 ReAct 拓扑

Phase 9+: Critic 已移除。Character Card + Render 两层人格表达：
  - Character Card（System Prompt）→ 决定 agent 怎么思考
  - Render Node（独立 LLM 调用）→ 决定输出怎么表达

核心拓扑
========

                   START
                     │
                     ▼
              reasoning_node ◄──────────┐
                     │                   │
                     ▼                   │
              ┌──────┼──────┐            │
              │      │      │            │
         tool_node  render  END          │
              │      │                   │
              │      └───────────────────┘
              │
              └──→ reasoning_node（消化工具结果）
"""

from __future__ import annotations

import logging
from typing import Literal

from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from agent.orchestrate.guardrails import format_tool_error
from agent.orchestrate.nodes import reasoning_node
from agent.persona.render import render_node
from agent.state import AgentState, get_max_iterations
from tools.bgm_tools import get_agent_tools

logger = logging.getLogger("bgm-agent.graph")


def _has_tool_calls_in_current_turn(messages: list) -> bool:
    """检查当前轮次是否有工具调用。

    从最后一条真实用户消息（跳过系统注入的 HumanMessage）开始查找 ToolMessage。
    仅检测当前轮次的工具调用，避免历史轮次的 ToolMessage 触发 render。

    Args:
        messages: 完整消息历史。

    Returns:
        True 如果当前轮次中存在 ToolMessage。
    """
    from langchain_core.messages import HumanMessage, ToolMessage

    # 找到最后一条"真实"用户消息（跳过系统注入的指令）
    last_user_idx = 0
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], HumanMessage):
            content = messages[i].content if hasattr(messages[i], "content") else ""
            if content and not str(content).startswith("（系统指令："):
                last_user_idx = i
                break

    for m in messages[last_user_idx:]:
        if isinstance(m, ToolMessage):
            return True
    return False


# ── 条件路由: reasoning → tool / render / END ──────────────


def route_after_reasoning(
    state: AgentState,
) -> Literal["tool_node", "render_node", "__end__"]:
    """reasoning_node 后的条件边。纯 ReAct 拓扑。

        1. AIMessage.tool_calls 非空 → tool_node
        2. 其他                      → render_node（风格渲染后输出）
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
        "route_after_reasoning: depth=%s intent=%s → render_node",
        state.get("depth", "auto"), state.get("query_intent", "unknown"),
    )
    return "render_node"


# ── route_after_critic 已移除（Phase 10, 2026-07-30） ──
# critic_node 及相关路由已从纯 ReAct 拓扑中移除。
# 如需恢复 Critic，参考 git history (Phase 9 之前)。


# ── 图谱构建 ──────────────────────────────────────────────


def build_graph(tools: list | None = None) -> StateGraph:
    """构建并编译 LangGraph 状态图。纯 ReAct 拓扑，无 Critic。

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
    graph.add_node("render_node", render_node)

    # ── 固定边 ────────────────────────────────────────────
    graph.add_edge(START, "reasoning_node")
    graph.add_edge("tool_node", "reasoning_node")
    graph.add_edge("render_node", END)

    # ── 条件边: reasoning → tool / render / END ─────────
    graph.add_conditional_edges(
        "reasoning_node",
        route_after_reasoning,
        {
            "tool_node": "tool_node",
            "render_node": "render_node",
            END: END,
        },
    )

    logger.info("Companion Agent 图谱编译完成（%d 个工具）", len(tools))
    return graph.compile()


# ── 模块级编译实例 ──────────────────────────────────────────

agent_app = build_graph()
"""预编译的 Companion Agent 图谱实例，可直接 ``agent_app.invoke(state)`` 调用。"""
