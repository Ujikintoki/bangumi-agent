"""
条件边路由函数 — LangGraph conditional edges

从 ``graph.py`` 中提取，与图构建逻辑分离。
"""

from __future__ import annotations

import logging
from typing import Literal

from langgraph.graph import END

from agent.config import _PIPELINE_CONFIDENCE_THRESHOLD, get_max_iterations
from agent.guardrails import check_duplicate_tool_calls, is_terminal_response
from agent.state import AgentState

logger = logging.getLogger("bgm-agent.graph")


# ── 条件路由: classify_node → per-intent 分发 ──────────────


def route_after_classify(
    state: AgentState,
) -> Literal[
    "fetch_pipeline", "realtime_pipeline", "profile_pipeline",
    "reasoning_node", "__end__",
]:
    """classify_node 后的条件边。置信度路由 + per-intent 分发。

    - chat → END（跳过一切，main.py 直接 render）
    - 低置信度（<0.7）→ reasoning_node（ReAct 安全网）
    - 高置信度 pipeline intent → 对应的 pipeline 子图
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

    # Pipeline intents → 子图
    if intent == "fetch":
        logger.info("route_after_classify: fetch (conf=%.2f) → fetch_pipeline", confidence)
        return "fetch_pipeline"
    if intent == "realtime":
        logger.info("route_after_classify: realtime (conf=%.2f) → realtime_pipeline", confidence)
        return "realtime_pipeline"
    if intent == "profile":
        logger.info("route_after_classify: profile (conf=%.2f) → profile_pipeline", confidence)
        return "profile_pipeline"

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
    from langchain_core.messages import AIMessage

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
            # [PHASE5-A] 空搜索早停 — grep: EMPTY_SEARCH_EARLY_STOP
            if _last_search_was_empty(messages):
                logger.info(
                    "route_after_tool: fetch 空搜索早停 → synthesize (跳过 detail)"
                )
                return "synthesize"
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


# ═══════════════════════════════════════════════════════════════════
# 空搜索检测
# ═══════════════════════════════════════════════════════════════════


def _count_consecutive_empty_searches(messages: list) -> int:
    """检测最近连续多少次 ``search_bangumi_subject`` 返回空结果。

    从最近的 ToolMessage 往前数，检查 JSON content 中的
    ``"results": []``、``"total": 0`` 或 ``"_error"`` 信号。
    遇到 AIMessage（含 tool_calls）时说明开始新一轮 → 计数器重置。

    Args:
        messages: 消息历史列表。

    Returns:
        连续空结果次数。非 search 工具的 ToolMessage 不计数但也不打断。
    """
    from langchain_core.messages import AIMessage, ToolMessage

    count = 0
    for m in reversed(messages):
        if isinstance(m, AIMessage) and hasattr(m, "tool_calls") and m.tool_calls:
            break
        if isinstance(m, ToolMessage):
            name = getattr(m, "name", "") or ""
            if name != "search_bangumi_subject":
                continue
            content = getattr(m, "content", "") or ""
            if not content:
                continue
            if (
                '"results":[]' in content.replace(" ", "")
                or '"total":0' in content.replace(" ", "")
                or '"_error"' in content
            ):
                count += 1
            else:
                break
    return count


# [PHASE5-A] 空搜索检测 — grep: EMPTY_SEARCH_EARLY_STOP


def _last_search_was_empty(messages: list) -> bool:
    """检查最近一轮 search_bangumi_subject 是否返回了空结果。

    用于 fetch pipeline 早停：search 无结果时跳过 detail 节点。

    Args:
        messages: 消息历史列表。

    Returns:
        True 如果最新一次 search 返回了空结果。
    """
    from langchain_core.messages import ToolMessage

    for m in reversed(messages):
        if isinstance(m, ToolMessage) and getattr(m, "name", "") == "search_bangumi_subject":
            content = getattr(m, "content", "") or ""
            no_spaces = content.replace(" ", "")
            return '"results":[]' in no_spaces or '"total":0' in no_spaces
        if hasattr(m, "type") and m.type == "human":
            break
    return False
