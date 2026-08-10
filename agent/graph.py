"""
Bangumi Agent 图谱编排 — v5 异质拓扑 + Subgraph 封装

  Pipeline intents（编译时确定性步骤）: chat | fetch | realtime | profile
  ReAct intents（运行时 LLM 自主探索）: explore | discuss | fallback

  START → classify_node ─┬── [chat] ─────────────→ END
                          ├── [fetch] ────────────→ fetch_pipeline (subgraph) → END
                          ├── [realtime] ─────────→ realtime_pipeline (subgraph) → END
                          ├── [profile] ──────────→ profile_pipeline (subgraph) → END
                          └── [explore|discuss|fallback] → reasoning_node ⇄ tool_node → END

  每个 pipeline 封装为独立 subgraph：
  - fetch: search → tool → detail → tool → synthesize
  - realtime: search → tool → synthesize
  - profile: search → tool → synthesize

Render 在 main.py 后处理。
"""

from __future__ import annotations

import logging

from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from agent.guardrails import format_tool_error
from agent.nodes.classify import classify_node
from agent.nodes.pipeline import (
    fetch_detail_node,
    fetch_search_node,
    profile_search_node,
    realtime_search_node,
    synthesize_node,
)
from agent.nodes.reasoning import reasoning_node
from agent.routing.routes import (
    route_after_classify,
    route_after_reasoning,
    route_after_tool,
)
from agent.state import AgentState
from tools.bgm_tools import get_agent_tools

logger = logging.getLogger("bgm-agent.graph")


# ═══════════════════════════════════════════════════════════════════
# Pipeline Subgraph Builders
# ═══════════════════════════════════════════════════════════════════


def _build_fetch_pipeline(tools: list) -> StateGraph:
    """构建 fetch pipeline 子图：search → detail → synthesize。

    Args:
        tools: LangChain 工具列表。

    Returns:
        编译后的 fetch pipeline 子图。
    """
    g = StateGraph(AgentState)
    fetch_tools = [
        t for t in tools
        if t.name in ("search_bangumi_subject", "get_bangumi_subject_detail")
    ]

    g.add_node("search", fetch_search_node)
    g.add_node("detail", fetch_detail_node)
    g.add_node("synthesize", synthesize_node)
    g.add_node("tool", ToolNode(fetch_tools, handle_tool_errors=format_tool_error))

    g.add_edge(START, "search")

    # 每个节点后 → tool 或 END
    for node in ("search", "detail", "synthesize"):
        g.add_conditional_edges(
            node, route_after_reasoning,
            {"tool_node": "tool", END: END},
        )

    # tool → 下一步或 END
    g.add_conditional_edges(
        "tool", route_after_tool,
        {"fetch_detail": "detail", "synthesize": "synthesize", END: END},
    )

    return g.compile()


def _build_realtime_pipeline(tools: list) -> StateGraph:
    """构建 realtime pipeline 子图：search → synthesize。

    Args:
        tools: LangChain 工具列表。

    Returns:
        编译后的 realtime pipeline 子图。
    """
    g = StateGraph(AgentState)
    rt_tools = [
        t for t in tools
        if t.name in ("get_calendar", "get_trending_subjects", "get_hot_topics")
    ]

    g.add_node("search", realtime_search_node)
    g.add_node("synthesize", synthesize_node)
    g.add_node("tool", ToolNode(rt_tools, handle_tool_errors=format_tool_error))

    g.add_edge(START, "search")

    for node in ("search", "synthesize"):
        g.add_conditional_edges(
            node, route_after_reasoning,
            {"tool_node": "tool", END: END},
        )

    g.add_conditional_edges(
        "tool", route_after_tool,
        {"synthesize": "synthesize", END: END},
    )

    return g.compile()


def _build_profile_pipeline(tools: list) -> StateGraph:
    """构建 profile pipeline 子图：search → synthesize。

    Args:
        tools: LangChain 工具列表。

    Returns:
        编译后的 profile pipeline 子图。
    """
    g = StateGraph(AgentState)
    pf_tools = [
        t for t in tools
        if t.name in ("get_user_profile", "get_user_timeline")
    ]

    g.add_node("search", profile_search_node)
    g.add_node("synthesize", synthesize_node)
    g.add_node("tool", ToolNode(pf_tools, handle_tool_errors=format_tool_error))

    g.add_edge(START, "search")

    for node in ("search", "synthesize"):
        g.add_conditional_edges(
            node, route_after_reasoning,
            {"tool_node": "tool", END: END},
        )

    g.add_conditional_edges(
        "tool", route_after_tool,
        {"synthesize": "synthesize", END: END},
    )

    return g.compile()


# ═══════════════════════════════════════════════════════════════════
# 主图谱构建
# ═══════════════════════════════════════════════════════════════════


def build_graph(tools: list | None = None) -> StateGraph:
    """构建并编译 LangGraph 状态图。v5 异质拓扑 + Subgraph 封装。

    Args:
        tools: LangChain 工具列表。None 时自动加载 ``get_agent_tools()``。
    """
    if tools is None:
        tools = get_agent_tools()

    graph = StateGraph(AgentState)

    # ── 注册节点 ──────────────────────────────────────────
    graph.add_node("classify_node", classify_node)

    # Pipeline 子图（每个子图内部自包含步骤 + 工具）
    graph.add_node("fetch_pipeline", _build_fetch_pipeline(tools))
    graph.add_node("realtime_pipeline", _build_realtime_pipeline(tools))
    graph.add_node("profile_pipeline", _build_profile_pipeline(tools))

    # ReAct 节点
    graph.add_node("reasoning_node", reasoning_node)
    graph.add_node("tool_node", ToolNode(tools, handle_tool_errors=format_tool_error))

    # ── 固定边 ────────────────────────────────────────────
    graph.add_edge(START, "classify_node")

    # ── classify_node → per-intent 分发 ──────────────────────
    graph.add_conditional_edges(
        "classify_node",
        route_after_classify,
        {
            "fetch_pipeline": "fetch_pipeline",
            "realtime_pipeline": "realtime_pipeline",
            "profile_pipeline": "profile_pipeline",
            "reasoning_node": "reasoning_node",
            END: END,
        },
    )

    # ── Pipeline 子图出口 → END ────────────────────────────
    graph.add_edge("fetch_pipeline", END)
    graph.add_edge("realtime_pipeline", END)
    graph.add_edge("profile_pipeline", END)

    # ── ReAct 循环 ────────────────────────────────────────
    graph.add_conditional_edges(
        "reasoning_node",
        route_after_reasoning,
        {"tool_node": "tool_node", END: END},
    )
    graph.add_conditional_edges(
        "tool_node",
        route_after_tool,
        {
            "reasoning_node": "reasoning_node",
            END: END,
        },
    )

    logger.info(
        "Bangumi Agent 图谱编译完成 v5+subgraph（%d 个工具，7 intent，3 pipeline 子图）",
        len(tools),
    )
    return graph.compile()


# ── 模块级编译实例 ──────────────────────────────────────────

agent_app = build_graph()
"""预编译的 Bangumi Agent 图谱实例，v5 异质拓扑 + Subgraph 封装。"""
