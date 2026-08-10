"""
Pipeline 节点 — v5 异质拓扑中编译时确定的执行计划

包含：
- ``_pipeline_step``: 所有 pipeline 节点的通用实现
- ``fetch_search_node`` / ``fetch_detail_node``: Fetch pipeline
- ``realtime_search_node``: Realtime pipeline
- ``profile_search_node``: Profile pipeline
- ``synthesize_node``: 所有 pipeline 的出口（纯文本总结）
"""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage

from agent.helpers import build_message_list, recall_memory_step
from agent.llm import create_llm
from agent.memory.short_term import (
    DEFAULT_MAX_TOKENS,
    DEPTH_TOKEN_BUDGETS,
    manage_memory,
)
from agent.state import AgentState
from core.config import get_settings
from tools.bgm_tools import get_agent_tools

logger = logging.getLogger("bgm-agent.nodes")


# ═══════════════════════════════════════════════════════════════════
# 通用 Pipeline Step
# ═══════════════════════════════════════════════════════════════════


async def _pipeline_step(
    state: AgentState,
    tool_names: list[str],
    tool_choice: str | dict,
    system_content: str,
    *,
    is_first_step: bool = False,
) -> dict:
    """Pipeline 节点的通用实现：绑指定工具 → LLM 调用 → 返回结果。

    Args:
        state: Agent 全局状态。
        tool_names: 此步骤可用的工具名列表。
        tool_choice: tool_choice 参数。
        system_content: 此步骤专属的 System Prompt。
        is_first_step: 是否为首步（需要记忆召回 + 消息列表构建）。
    """
    new_iterations = state.get("iterations", 0) + 1
    messages = state.get("messages", [])
    depth = state.get("depth", "fast")
    query_intent = state.get("query_intent", "fallback")

    # 首步：记忆召回 + 构建消息列表
    if is_first_step:
        memory_context = await recall_memory_step(
            state,
            max_tokens=get_settings().MEMORY_DIALOGUE_MAX_INJECT_TOKENS,
            recall_threshold=get_settings().MEMORY_DIALOGUE_RECALL_THRESHOLD,
        )
        built = build_message_list(messages, system_content)
    else:
        memory_context = state.get("_memory_context", "") or ""
        built = list(messages)

    # L1 记忆截断
    token_budget = DEPTH_TOKEN_BUDGETS.get(depth, DEFAULT_MAX_TOKENS)
    built = manage_memory(built, max_tokens=token_budget)

    # 工具绑定
    all_tools = get_agent_tools()
    intent_tools = [t for t in all_tools if t.name in tool_names]

    llm = create_llm(
        _telemetry_label=f"pipeline#{new_iterations}",
        extra_body={"thinking": {"type": "disabled"}},
    )

    llm_to_use = llm.bind_tools(intent_tools, tool_choice=tool_choice) if intent_tools else llm

    logger.info(
        "[Pipeline] intent=%s step=%d tools=%s tool_choice=%s",
        query_intent, new_iterations,
        [t.name for t in intent_tools], str(tool_choice),
    )

    try:
        response: AIMessage = await llm_to_use.ainvoke(built)
    except Exception as e:
        logger.exception("pipeline_step: LLM 调用失败")
        return {
            "messages": [AIMessage(content=f"数据收集失败：{e}")],
            "iterations": new_iterations,
            "_memory_context": memory_context or "",
        }

    return {
        "messages": [response],
        "iterations": new_iterations,
        "_memory_context": memory_context or "",
    }


# ═══════════════════════════════════════════════════════════════════
# Fetch Pipeline
# ═══════════════════════════════════════════════════════════════════


async def fetch_search_node(state: AgentState) -> dict:
    """Fetch pipeline step 1: 搜索条目。"""
    from agent.prompts.pipeline import _SEARCH_NODE_PROMPT
    return await _pipeline_step(
        state,
        tool_names=["search_bangumi_subject"],
        tool_choice="required",
        system_content=_SEARCH_NODE_PROMPT,
        is_first_step=True,
    )


async def fetch_detail_node(state: AgentState) -> dict:
    """Fetch pipeline step 2: 拉取详情。"""
    from agent.prompts.pipeline import _DETAIL_NODE_PROMPT
    return await _pipeline_step(
        state,
        tool_names=["get_bangumi_subject_detail", "search_bangumi_subject"],
        tool_choice="required",
        system_content=_DETAIL_NODE_PROMPT,
    )


# ═══════════════════════════════════════════════════════════════════
# Realtime Pipeline
# ═══════════════════════════════════════════════════════════════════


async def realtime_search_node(state: AgentState) -> dict:
    """Realtime pipeline step 1: 时效数据。"""
    from agent.prompts.pipeline import _REALTIME_NODE_PROMPT
    return await _pipeline_step(
        state,
        tool_names=["get_calendar", "get_trending_subjects", "get_hot_topics"],
        tool_choice="required",
        system_content=_REALTIME_NODE_PROMPT,
        is_first_step=True,
    )


# ═══════════════════════════════════════════════════════════════════
# Profile Pipeline
# ═══════════════════════════════════════════════════════════════════


async def profile_search_node(state: AgentState) -> dict:
    """Profile pipeline step 1: 用户画像。"""
    from agent.prompts.pipeline import _PROFILE_NODE_PROMPT
    return await _pipeline_step(
        state,
        tool_names=["get_user_profile", "get_user_timeline"],
        tool_choice="required",
        system_content=_PROFILE_NODE_PROMPT,
        is_first_step=True,
    )


# ═══════════════════════════════════════════════════════════════════
# Synthesize（所有 pipeline 的出口）
# ═══════════════════════════════════════════════════════════════════


async def synthesize_node(state: AgentState) -> dict:
    """Pipeline 出口: 纯文本总结，无工具。"""
    from agent.prompts.pipeline import _SYNTHESIZE_NODE_PROMPT
    return await _pipeline_step(
        state,
        tool_names=[],
        tool_choice="auto",
        system_content=_SYNTHESIZE_NODE_PROMPT,
    )
