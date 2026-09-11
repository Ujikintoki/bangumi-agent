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
    route_after_tool_react,
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
# Subgraph 挂载包装
# ═══════════════════════════════════════════════════════════════════


def _pipeline_node(subgraph, name: str):
    """把编译好的 pipeline 子图包成父图节点：只回传子图「新增」的消息。

    子图被当节点用时，LangGraph 把父图 state 传进去，再把子图的**完整**
    final state 还回来——其中包含它调用前收到的那些消息。父图 ``messages``
    是 ``operator.add`` 语义，不区分"新增"与"退还"，于是把输入整体再追加
    一次（P2）。多轮会话下被复制的是**整个历史**，经 ``main.py`` 写进 L1
    session 缓存后，实际只留存约一半的不重复消息，表现为"聊几轮就忘事"。

    LangGraph 的 ``input_schema`` / ``output_schema`` 不解决该问题——它们
    只过滤回传哪些 **key**，而问题出在 ``messages`` 这个 key 的**列表内容**；
    实测三种 schema 写法（无 / output_schema / 子图独立 schema）均仍重复。

    安全性依赖一条契约：**子图内部只增不减**。``agent/nodes/pipeline.py``
    的全部节点只返回新产出的那一条（``manage_memory`` 作用在局部变量
    ``built`` 上，从不改 ``state["messages"]``），因此 ``[n_before:]`` 切出
    的正是新增部分。该契约由 ``test_graph.py::TestSubgraphMessageBoundary``
    钉住——若将来有节点就地裁剪 ``state["messages"]``，这些用例会红。

    Args:
        subgraph: 已编译的 pipeline 子图。
        name: 子图名，仅用于日志。

    Returns:
        可直接传给 ``graph.add_node`` 的异步节点函数。
    """

    async def _node(state: AgentState) -> dict:
        n_before = len(state.get("messages", []))
        result = await subgraph.ainvoke(state)

        delta = dict(result)
        returned = list(result.get("messages", []))
        delta["messages"] = returned[n_before:]
        logger.debug(
            "subgraph %s: 收到 %d 条 → 还回 %d 条，截取增量 %d 条",
            name, n_before, len(returned), len(delta["messages"]),
        )
        return delta

    # 暴露内层子图：test_routes.py 的接线测试要穿透包装断言子图内部路由，
    # 同时可据此判断"这个节点到底有没有被包装"。
    _node.subgraph = subgraph

    return _node


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
    # 经 _pipeline_node 包装：子图会把它收到的消息原样退还，直接挂载会让
    # 父图 operator.add 把输入整体重复追加一次（P2）。
    graph.add_node(
        "fetch_pipeline", _pipeline_node(_build_fetch_pipeline(tools), "fetch")
    )
    graph.add_node(
        "realtime_pipeline",
        _pipeline_node(_build_realtime_pipeline(tools), "realtime"),
    )
    graph.add_node(
        "profile_pipeline",
        _pipeline_node(_build_profile_pipeline(tools), "profile"),
    )

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
    # 父图用 route_after_tool_react：只回 reasoning_node 或 END。
    # 不能用 route_after_tool——它会按 intent 返回 pipeline 步骤名
    # （fetch_detail / synthesize），父图 path_map 里没有这些键 → KeyError（P1）。
    graph.add_conditional_edges(
        "tool_node",
        route_after_tool_react,
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
