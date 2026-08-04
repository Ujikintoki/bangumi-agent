"""
Bangumi Agent 图谱编排 — v5 异质拓扑（Phase 4: Per-intent 执行计划）

  Pipeline intents（编译时确定性步骤）: chat | fetch | realtime | profile
  ReAct intents（运行时 LLM 自主探索）: explore | discuss | fallback

  START → classify_node ─┬── [chat] ──────────→ END
                          ├── [fetch] ─────────→ fetch_search → tool → fetch_detail → tool → synthesize → END
                          ├── [realtime] ──────→ realtime_search → tool → synthesize → END
                          ├── [profile] ───────→ profile_search → tool → synthesize → END
                          └── [explore|discuss|fallback] → reasoning_node ⇄ tool_node → END

  - classify_node: LLM 分类（7 intent） → 置信度路由
  - pipeline 节点: 各绑自己的工具集 + 专属 prompt，不退化为 while 循环
  - synthesize_node: 纯文本出口，所有 pipeline 共享
  - reasoning_node: ReAct 循环，用于探索/讨论/兜底

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
    fetch_detail_node,
    fetch_search_node,
    profile_search_node,
    realtime_search_node,
    reasoning_node,
    synthesize_node,
)
from agent.state import AgentState, get_max_iterations
from tools.bgm_tools import get_agent_tools

logger = logging.getLogger("bgm-agent.graph")

# ── 置信度阈值：低于此值不进 pipeline，走 ReAct fallback ──

_PIPELINE_CONFIDENCE_THRESHOLD = 0.7


# ── 条件路由: classify_node ──────────────────────────────────


def route_after_classify(
    state: AgentState,
) -> Literal[
    "fetch_search", "realtime_search", "profile_search",
    "reasoning_node", "__end__",
]:
    """classify_node 后的条件边。置信度路由 + per-intent 分发。

    - chat → END（跳过一切，main.py 直接 render）
    - 低置信度（<0.7）→ reasoning_node（ReAct 安全网）
    - 高置信度 pipeline intent → 对应的 pipeline 入口节点
    - explore/discuss/fallback → reasoning_node（ReAct 探索）
    """
    intent = state.get("query_intent", "fallback")
    confidence = state.get("classifier_confidence", 0.0) or 0.0

    if intent == "chat":
        logger.info("route_after_classify: chat → END")
        return END

    # 低置信度 → ReAct 兜底
    if confidence < _PIPELINE_CONFIDENCE_THRESHOLD:
        logger.info(
            "route_after_classify: intent=%s conf=%.2f < %.2f → ReAct fallback",
            intent, confidence, _PIPELINE_CONFIDENCE_THRESHOLD,
        )
        return "reasoning_node"

    # Pipeline intents
    if intent == "fetch":
        logger.info("route_after_classify: fetch (conf=%.2f) → fetch_search", confidence)
        return "fetch_search"
    if intent == "realtime":
        logger.info("route_after_classify: realtime (conf=%.2f) → realtime_search", confidence)
        return "realtime_search"
    if intent == "profile":
        logger.info("route_after_classify: profile (conf=%.2f) → profile_search", confidence)
        return "profile_search"

    # ReAct intents
    logger.info(
        "route_after_classify: intent=%s conf=%.2f → reasoning_node (ReAct)",
        intent, confidence,
    )
    return "reasoning_node"


# ── 条件路由: tool_node → next step / END ────────────────────


def route_after_tool(
    state: AgentState,
) -> Literal[
    "fetch_detail", "synthesize",
    "reasoning_node", "__end__",
]:
    """tool_node 后的条件边——控制中枢（v5: pipeline 步骤路由 + ReAct 路由）。

    1. 硬熔断：iterations >= per-intent max → END
    2. Pipeline 步骤路由（intent + iterations）
    3. 连续 2 次空搜索 → END
    4. 重复工具调用 → END
    5. ReAct → reasoning_node
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

    # ── Pipeline 步骤路由 ──
    if intent == "fetch":
        if current_iter == 1:
            logger.info("route_after_tool: fetch step 1 → fetch_detail")
            return "fetch_detail"
        elif current_iter == 2:
            logger.info("route_after_tool: fetch step 2 → synthesize")
            return "synthesize"

    if intent in ("realtime", "profile"):
        if current_iter == 1:
            logger.info("route_after_tool: %s step 1 → synthesize", intent)
            return "synthesize"

    # ── 公共熔断 ──
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

    # ReAct 继续
    return "reasoning_node"


# ── 条件路由: reasoning/pipeline → tool / END ────────────────


def route_after_reasoning(
    state: AgentState,
) -> Literal["tool_node", "__end__"]:
    """reasoning_node 或 pipeline 节点后的条件边。

    - AIMessage 含 tool_calls → tool_node
    - 终端回复 → END
    - 其他 → END（隐式终止）
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

    # 终端回复检测
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
    """构建并编译 LangGraph 状态图。v5 异质拓扑。

    Args:
        tools: LangChain 工具列表。None 时自动加载 ``get_agent_tools()``。
    """
    if tools is None:
        tools = get_agent_tools()

    graph = StateGraph(AgentState)

    # ── 注册节点 ──────────────────────────────────────────
    graph.add_node("classify_node", classify_node)

    # Pipeline 节点
    graph.add_node("fetch_search", fetch_search_node)
    graph.add_node("fetch_detail", fetch_detail_node)
    graph.add_node("realtime_search", realtime_search_node)
    graph.add_node("profile_search", profile_search_node)
    graph.add_node("synthesize", synthesize_node)

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
            "fetch_search": "fetch_search",
            "realtime_search": "realtime_search",
            "profile_search": "profile_search",
            "reasoning_node": "reasoning_node",
            END: END,
        },
    )

    # ── pipeline 节点 → tool/END ─────────────────────────────
    for node in ("fetch_search", "fetch_detail", "realtime_search",
                  "profile_search", "synthesize", "reasoning_node"):
        graph.add_conditional_edges(
            node,
            route_after_reasoning,
            {"tool_node": "tool_node", END: END},
        )

    # ── tool_node → pipeline next / ReAct / END ──────────────
    graph.add_conditional_edges(
        "tool_node",
        route_after_tool,
        {
            "fetch_detail": "fetch_detail",
            "synthesize": "synthesize",
            "reasoning_node": "reasoning_node",
            END: END,
        },
    )

    logger.info(
        "Bangumi Agent 图谱编译完成 v5（%d 个工具，7 intent，5 pipeline 节点）",
        len(tools),
    )
    return graph.compile()


# ── 模块级编译实例 ──────────────────────────────────────────

agent_app = build_graph()
"""预编译的 Bangumi Agent 图谱实例，v5 异质拓扑。"""
