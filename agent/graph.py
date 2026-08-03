"""
Bangumi Agent 图谱编排 — v4 异质拓扑

  6+1 intent 路由：chat 直通 Render，其余进入 per-intent ReAct 循环。

  START → classify_node ─┬── [chat] ──────────→ END
                          └── [6 tool intents] → reasoning_node ⇄ tool_node → END

  - classify_node: 独立 LLM 分类（7 intent） → 置信度路由
  - reasoning_node: 纯 Aggregator — Dynamic Tool Binding + Forced Tool Choice
  - route_after_tool: 控制中枢 — 迭代上限、空搜索、重复调用、submit_facts
  - route_after_reasoning: 终端回复、deep 首轮重试

Render 在 main.py 后处理。
"""

from __future__ import annotations

import logging
from typing import Literal

from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from agent.orchestrate.guardrails import (
    check_duplicate_tool_calls,
    format_tool_error,
    is_terminal_response,
)
from agent.orchestrate.nodes import (
    _count_consecutive_empty_searches,
    classify_node,
    reasoning_node,
)
from agent.state import AgentState, get_max_iterations
from tools.bgm_tools import get_agent_tools

logger = logging.getLogger("bgm-agent.graph")


# ── 条件路由: classify_node → reasoning / END ──────────────


def route_after_classify(
    state: AgentState,
) -> Literal["reasoning_node", "__end__"]:
    """classify_node 后的条件边。chat → 直接 END，其余 → ReAct 循环。"""
    intent = state.get("query_intent", "fallback")
    if intent == "chat":
        logger.info("route_after_classify: chat → END (skip tool loop)")
        return END
    logger.debug("route_after_classify: intent=%s → reasoning_node", intent)
    return "reasoning_node"


# ── 条件路由: tool_node → reasoning / END ──────────────────


def route_after_tool(
    state: AgentState,
) -> Literal["reasoning_node", "__end__"]:
    """tool_node 后的条件边——控制中枢。

    1. 硬熔断：iterations >= per-intent max → END
    2. submit_facts → END
    3. 连续 2 次空搜索 → END
    4. 重复工具调用 → END
    5. 其他 → reasoning_node（继续 ReAct）
    """
    from langchain_core.messages import ToolMessage

    depth = state.get("depth", "fast")
    intent = state.get("query_intent", "fallback")
    max_iter = get_max_iterations(depth, intent)
    current_iter = state.get("iterations", 0)
    messages = state.get("messages", [])

    # 硬熔断
    if current_iter >= max_iter:
        logger.warning(
            "route_after_tool: 硬熔断 intent=%s iter=%d/%d → END",
            intent, current_iter, max_iter,
        )
        return END

    # submit_facts 检测
    for m in reversed(messages):
        if isinstance(m, ToolMessage):
            if getattr(m, "name", "") == "submit_facts_to_render":
                logger.info("route_after_tool: submit_facts → END")
                return END
            break

    # 连续空搜索
    consecutive_empty = _count_consecutive_empty_searches(messages)
    if consecutive_empty >= 2:
        logger.warning(
            "route_after_tool: 连续 %d 次空搜索 → END", consecutive_empty
        )
        return END

    # 重复工具调用
    dup = check_duplicate_tool_calls(messages)
    if dup:
        logger.warning("route_after_tool: 重复调用 '%s' → END", dup[:60])
        return END

    # 继续 ReAct
    return "reasoning_node"


# ── 条件路由: reasoning → tool / END ───────────────────────


def route_after_reasoning(
    state: AgentState,
) -> Literal["tool_node", "__end__"]:
    """reasoning_node 后的条件边。

    - AIMessage 含 tool_calls → tool_node
    - 终端回复（含逃逸信号）→ END
    - deep 首轮 0 工具 → 重路由（替代旧 hack）
    - 其他 → END
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

    # 终端回复检测（替代旧逃逸舱 hack）
    if last_msg and isinstance(last_msg, AIMessage):
        if last_msg.content and is_terminal_response(last_msg.content):
            logger.info("route_after_reasoning: 终端回复 → END")
            return END

    logger.debug(
        "route_after_reasoning: intent=%s iter=%d → END",
        state.get("query_intent", "?"), state.get("iterations", 0),
    )
    return END


# ── 图谱构建 ────────────────────────────────────────────────


def build_graph(tools: list | None = None) -> StateGraph:
    """构建并编译 LangGraph 状态图。v4 异质拓扑。

    Args:
        tools: LangChain 工具列表。None 时自动加载 ``get_agent_tools()``。

    Returns:
        编译后的 ``StateGraph`` 实例。
    """
    if tools is None:
        tools = get_agent_tools()

    graph = StateGraph(AgentState)

    # ── 注册节点 ──────────────────────────────────────────
    graph.add_node("classify_node", classify_node)
    graph.add_node("reasoning_node", reasoning_node)
    graph.add_node("tool_node", ToolNode(tools, handle_tool_errors=format_tool_error))

    # ── 固定边 ────────────────────────────────────────────
    graph.add_edge(START, "classify_node")

    # ── classify_node → chat 直通 / ReAct 循环 ────────────
    graph.add_conditional_edges(
        "classify_node",
        route_after_classify,
        {
            "reasoning_node": "reasoning_node",
            END: END,
        },
    )

    # ── reasoning_node → tool / END ──────────────────────────
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

    logger.info("Bangumi Agent 图谱编译完成 v4（%d 个工具，7 intent）", len(tools))
    return graph.compile()


# ── 模块级编译实例 ──────────────────────────────────────────

agent_app = build_graph()
"""预编译的 Bangumi Agent 图谱实例，v4 异质拓扑。"""
