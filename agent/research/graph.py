"""
LangGraph 图谱编排 — 标准 ReAct 拓扑

核心拓扑
========

                   START
                     │
                     ▼
              reasoning_node ◄──────────────┐
                     │         (消化工具结果) │
                     ▼                      │
              ┌──────┼──────────┐           │
              │      │          │           │
         tool_node   │     chitchat         │
              │      │     (快速通道)        │
              │  critic_node    │           │
              │      │          │           │
              │      ▼          ▼           │
              │  (PASS?超限?)   END          │
              │   ┌──┴──┐                   │
              │   │     │                   │
              │  END  reasoning (REVISE) ───┘
              │
              └──→ reasoning（消化工具结果，消化态禁工具绑定）

决策矩阵
========

route_after_reasoning（原生消息路由，读 messages[-1]）:
    - AIMessage.tool_calls 非空 → tool_node → reasoning_node（消化结果）
    - intent = chitchat         → END（快速通道）
    - 其他无工具调用            → critic_node

route_after_critic:
    - PASS               → END
    - REVISE + iter < 10 → reasoning_node（重试）
    - REVISE + iter >= 10→ END（熔断）
"""

from __future__ import annotations

import logging
from typing import Literal

from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from agent.guardrails import format_tool_error
from agent.research.nodes import critic_node, reasoning_node
from agent.research.state import _MAX_ITERATIONS, AgentState
from tools.bgm_tools import get_agent_tools

logger = logging.getLogger("bgm-agent.graph")

# 快速通道意图：纯闲聊跳过 Critic，直接结束。
_FAST_PATH_INTENTS = frozenset({"chitchat"})


# ── 条件路由: reasoning → tool / critic / END ──────────────


def route_after_reasoning(
    state: AgentState,
) -> Literal["tool_node", "critic_node", "__end__"]:
    """reasoning_node 后的条件边（原生消息路由）。

    直接读取 ``state["messages"][-1]`` 的 ``tool_calls`` 属性判定路由，
    不依赖冗余的 ``last_tool_calls`` 状态字段。

    三级路由（优先级从高到低）：
        1. AIMessage.tool_calls 非空 → tool_node（执行后回到 reasoning 消化结果）
        2. query_intent = chitchat   → END（快速通道，跳过 tool 和 critic）
        3. 其他（无工具调用）       → critic_node（直接评估回复质量）

    Args:
        state: 当前 Agent 全局状态。

    Returns:
        ``"tool_node"``、``"critic_node"`` 或 ``"__end__"``。
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
        logger.debug("route_after_reasoning: intent=%s → 快速通道 END", query_intent)
        return END

    logger.debug(
        "route_after_reasoning: intent=%s 无工具调用 → critic_node", query_intent
    )
    return "critic_node"


# ── 条件路由: critic → retry / END ──────────────────────────


def route_after_critic(state: AgentState) -> Literal["reasoning_node", "__end__"]:
    """critic_node 后的条件边。

    决策矩阵：

        +----------------+----------------+----------------+
        | critic_status  | iterations < 5 | iterations >= 5|
        +================+================+================+
        | PASS           | → END          | → END          |
        +----------------+----------------+----------------+
        | REVISE         | → reasoning    | → END（强制）  |
        +----------------+----------------+----------------+

    Args:
        state: 当前 Agent 全局状态。

    Returns:
        ``"reasoning_node"`` 回到推理节点重试，或 ``"__end__"`` 结束图谱。
    """
    iterations = state.get("iterations", 0)
    status = state.get("critic_status", "PENDING")

    if iterations >= _MAX_ITERATIONS:
        logger.info("迭代次数已达上限 %d，强制终止", _MAX_ITERATIONS)
        return END

    if status == "PASS":
        logger.info("自省通过 (iterations=%d)，结束图谱", iterations)
        return END

    logger.info("自省要求修正 (iterations=%d)，返回 reasoning_node", iterations)
    return "reasoning_node"


# ── 图谱构建 ──────────────────────────────────────────────


def build_graph(tools: list | None = None) -> StateGraph:
    """构建并编译 LangGraph 状态图。

    Args:
        tools: LangChain 工具列表。None 时自动加载 ``get_agent_tools()``。
            测试时可注入 mock 工具以避免真实 API 调用。

    Returns:
        编译后的 ``StateGraph`` 实例，可直接调用 ``.invoke()``。
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

    logger.info("Agent 图谱编译完成（%d 个工具）", len(tools))
    return graph.compile()


# ── 模块级编译实例 ──────────────────────────────────────────

agent_app = build_graph()
"""预编译的 Agent 图谱实例，可直接 ``agent_app.invoke(state)`` 调用。"""
