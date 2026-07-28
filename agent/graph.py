"""
Companion Agent 图谱编排 — 纯 ReAct 拓扑

Phase 6: 合并 Research 和 Dialogue 两个 graph 为单一 StateGraph。
Phase 6.5: 新增 render_node——工具调用后的回复过 render 做风格转换。
Phase 9: Critic 暂时屏蔽——三种 depth 共享同一拓扑，仅通过参数区分行为。

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

三种 depth 的区别不在拓扑——在参数：

| 维度         | quick         | auto          | deep          |
|-------------|---------------|---------------|---------------|
| Token 预算   | 6,000         | 10,000        | 16,000        |
| 最大迭代     | 3             | 5             | 12            |
| depth_taste  | 0.30          | 0.70（默认）   | 0.90          |
| initiative   | 0.20          | 0.60（默认）   | 0.80          |
| Scene Hints  | COMPANION     | COMPANION     | DEEP          |
| 字数上限     | 120           | 200           | 350           |

Critic 节点保留在图中（用于未来重新激活），当前不被路由到。
"""

from __future__ import annotations

import logging
from typing import Literal

from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from agent.orchestrate.guardrails import format_tool_error
from agent.orchestrate.nodes import critic_node, reasoning_node
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


# ── 条件路由: reasoning → tool / critic / render / END ──────


def route_after_reasoning(
    state: AgentState,
) -> Literal["tool_node", "critic_node", "render_node", "__end__"]:
    """reasoning_node 后的条件边。纯 ReAct 路由。

    Phase 9: Critic 暂时屏蔽——三种 depth 共享同一拓扑。
        1. AIMessage.tool_calls 非空 → tool_node
        2. 其他                      → render_node → END
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

    # 所有非工具路径统一走 render_node
    # _should_skip_render 在 render_node 内部自动跳过短闲聊
    logger.debug(
        "route_after_reasoning: depth=%s intent=%s → render_node",
        state.get("depth", "auto"), state.get("query_intent", "unknown"),
    )
    return "render_node"


# ── 条件路由: critic → retry / END ──────────────────────────


def route_after_critic(
    state: AgentState,
) -> Literal["reasoning_node", "render_node", "__end__"]:
    """critic_node 后的条件边。

    决策矩阵：
        +----------------+---------------------------+
        | critic_status  | 路由                       |
        +================+===========================+
        | PASS + 有工具   | → render_node → END       |
        +----------------+---------------------------+
        | PASS + 无工具   | → END                     |
        +----------------+---------------------------+
        | REVISE + iter<N | → reasoning_node（重试）  |
        +----------------+---------------------------+
        | REVISE + iter>=N| → END（熔断）             |
        +----------------+---------------------------+
    """
    depth = state.get("depth", "deep")
    max_iterations = get_max_iterations(depth)
    iterations = state.get("iterations", 0)
    status = state.get("critic_status", "PENDING")
    messages = state.get("messages", [])

    if iterations >= max_iterations:
        logger.info("迭代次数已达上限 %d，强制终止", max_iterations)
        return END

    if status == "PASS":
        if _has_tool_calls_in_current_turn(messages):
            logger.info("自省通过 + 有工具调用 → render_node")
            return "render_node"
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
    graph.add_node("render_node", render_node)

    # ── 固定边 ────────────────────────────────────────────
    graph.add_edge(START, "reasoning_node")
    graph.add_edge("tool_node", "reasoning_node")
    graph.add_edge("render_node", END)

    # ── 条件边 1: reasoning → tool / critic / render / END ──
    graph.add_conditional_edges(
        "reasoning_node",
        route_after_reasoning,
        {
            "tool_node": "tool_node",
            "critic_node": "critic_node",
            "render_node": "render_node",
            END: END,
        },
    )

    # ── 条件边 2: critic → retry / render / END ────────────
    graph.add_conditional_edges(
        "critic_node",
        route_after_critic,
        {
            "reasoning_node": "reasoning_node",
            "render_node": "render_node",
            END: END,
        },
    )

    logger.info("Companion Agent 图谱编译完成（%d 个工具）", len(tools))
    return graph.compile()


# ── 模块级编译实例 ──────────────────────────────────────────

agent_app = build_graph()
"""预编译的 Companion Agent 图谱实例，可直接 ``agent_app.invoke(state)`` 调用。"""
