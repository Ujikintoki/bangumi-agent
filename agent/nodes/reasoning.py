"""
统一推理节点（ReAct 路径）— v4: 纯 Aggregator

reasoning_node 是 explore/discuss/fallback 意图的核心推理引擎。
使用 ReAct 循环：LLM 调工具 → 消化结果 → 决定下一步。

fast / deep 两种深度模式共享同一逻辑，差异在迭代上限和 token 预算。
"""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent.config import get_max_iterations
from agent.helpers import build_message_list, guard_xml_leak, recall_memory_step
from agent.llm import create_llm
from agent.memory.short_term import (
    DEFAULT_MAX_TOKENS,
    DEPTH_TOKEN_BUDGETS,
    manage_memory,
)
from agent.persona.profiles import get_character
from agent.prompts.aggregator import build_aggregator_prompt
from agent.prompts.scene_hints import COMPANION_INTENT_PROMPTS, COMPANION_SCENE_HINTS
from agent.prompts.scene_hints_deep import DEEP_SCENE_HINTS
from agent.prompts.tool_config import TOOLS_BY_INTENT, get_tool_choice
from agent.state import AgentState
from core.config import get_settings
from tools.bgm_tools import get_agent_tools

logger = logging.getLogger("bgm-agent.nodes")


# ═══════════════════════════════════════════════════════════════════
# 统一推理节点（ReAct 路径）
# ═══════════════════════════════════════════════════════════════════


async def reasoning_node(state: AgentState) -> dict:
    """推理节点：意图分类 + LLM function-calling 决策。纯 ReAct。

    两种 depth 共享同一逻辑，差异在迭代上限和 token 预算。
    depth_taste 来自角色实例（CharacterProfile），控制搜索深度而
    非人格表达。

    流程：
        1. 意图分类（仅首轮）
        2. L2 记忆召回
        3. 构建 System Prompt（按 depth 传不同的 personality 参数和 scene hints）
        4. LLM 调用（始终绑定工具，除非 last_chance）
        5. XML 泄漏防护

    Args:
        state: 当前 Agent 全局状态。

    Returns:
        包含 messages、iterations、query_intent 等更新的字典。
    """
    depth = state.get("depth", "fast")
    query_intent = state.get("query_intent", "fallback")
    max_iterations = get_max_iterations(depth, query_intent)
    is_deep = depth == "deep"

    new_iterations = state.get("iterations", 0) + 1
    messages = state.get("messages", [])

    # ── Step 1: 记忆召回（仅首轮） ─────────────────────────
    if new_iterations == 1:
        if is_deep:
            memory_context = await recall_memory_step(
                state,
                max_tokens=get_settings().MEMORY_MAX_INJECT_TOKENS,
            )
        else:
            memory_context = await recall_memory_step(
                state,
                max_tokens=get_settings().MEMORY_DIALOGUE_MAX_INJECT_TOKENS,
                recall_threshold=get_settings().MEMORY_DIALOGUE_RECALL_THRESHOLD,
            )
    else:
        memory_context = state.get("_memory_context", "") or ""

    # ── Step 2: 构建 Aggregator System Prompt（首轮） ──────
    if new_iterations == 1:
        output_style = state.get("output_style", "bangumi")
        character = get_character(output_style)
        scene_hints = DEEP_SCENE_HINTS if is_deep else COMPANION_SCENE_HINTS
        system_content = build_aggregator_prompt(
            character=character,
            depth="deep" if is_deep else depth,
            intent=query_intent,
            scene_hints=scene_hints,
            memory_context=memory_context,
        )
    else:
        system_content = None

    # ── Step 3: 构建消息列表 ───────────────────────────────
    messages_for_llm = build_message_list(messages, system_content)

    # ── Step 4: Dynamic Tool Binding + Forced Tool Choice ────
    tool_names = TOOLS_BY_INTENT.get(query_intent, TOOLS_BY_INTENT["fallback"])
    all_tools = get_agent_tools()
    intent_tools = [t for t in all_tools if t.name in tool_names]

    tool_choice = get_tool_choice(
        intent=query_intent,
        iterations=new_iterations,
        max_iterations=max_iterations,
    )

    llm = create_llm(
        _telemetry_label=f"reasoning#{new_iterations}",
        extra_body={"thinking": {"type": "disabled"}},
    )
    llm_to_use = llm.bind_tools(intent_tools, tool_choice=tool_choice)

    is_digesting = messages and isinstance(messages[-1], ToolMessage)
    at_last_round = new_iterations >= max_iterations - 1
    logger.debug(
        "reasoning_node: intent=%s iter=%d/%d tools=%d tool_choice=%s%s",
        query_intent, new_iterations, max_iterations,
        len(intent_tools), str(tool_choice),
        " (digesting)" if is_digesting else "",
    )

    # ── 消化态软引导 ──────────────────────────────────
    if is_digesting and at_last_round:
        from agent.prompts.aggregator import _LAST_CHANCE_DIGEST_HINT
        messages_for_llm.append(HumanMessage(content=_LAST_CHANCE_DIGEST_HINT))
    elif is_digesting:
        # [PHASE5-A] 检测 search-only 模式
        # grep: SEARCH_ONLY_DETECTION
        search_only = _detect_search_only_stall(messages, query_intent)
        if search_only:
            digest_hint = (
                "（系统指令：你上一轮只调了 search。如果用户问的是作品内容/人物详情/声优列表，"
                "search 返回的片段信息不够——至少调一次对应的 detail/characters/person 工具。"
                "如果确实是简单的评分/排名查询，search 结果够用就直接输出文本摘要。）"
            )
        else:
            digest_hint = (
                "（系统指令：工具数据已返回。判断是否够回答用户问题："
                "够→输出文本摘要结束，不够→继续查。）"
            )
        messages_for_llm.append(HumanMessage(content=digest_hint))

    # ── L1 记忆截断 ─────────────────────────────────────────
    token_budget = DEPTH_TOKEN_BUDGETS.get(depth, DEFAULT_MAX_TOKENS)
    messages_for_llm = manage_memory(messages_for_llm, max_tokens=token_budget)

    # ── 消息状态日志 ────────────────────────────────────────
    _log_message_state(messages_for_llm, new_iterations)

    # ── LLM 调用 ────────────────────────────────────────────
    try:
        response: AIMessage = await llm_to_use.ainvoke(messages_for_llm)
    except Exception as e:
        logger.exception("reasoning_node: LLM 调用失败")
        fallback = (
            f"抱歉，AI 服务暂时不可用：{e}" if is_deep else f"啧，脑子短路了。{e}"
        )
        return {
            "messages": [AIMessage(content=fallback)],
            "query_intent": query_intent,
            "iterations": new_iterations,
            "_memory_context": memory_context or "",
        }

    # ── XML 泄漏防护 ────────────────────────────────────────
    fallback_text = (
        "抱歉，我无法正确处理工具返回的数据。请尝试换个方式提问，或提供更具体的信息。"
        if is_deep
        else "啧，脑子有点乱，你再说一遍？"
    )
    response = guard_xml_leak(
        response, is_digesting=is_digesting,
        fallback_text=fallback_text, log=logger,
    )

    # ── Step 5: 日志 ────────────────────────────────────────
    tool_calls = (
        list(response.tool_calls)
        if hasattr(response, "tool_calls") and response.tool_calls
        else []
    )
    logger.info(
        "[Reasoning] depth=%s intent=%s iter=%d/%d tool_calls=%s",
        depth, query_intent, new_iterations, max_iterations,
        [tc.get("name", "?") for tc in tool_calls],
    )

    return {
        "messages": [response],
        "iterations": new_iterations,
        "query_intent": query_intent,
        "_memory_context": memory_context or "",
    }


# ═══════════════════════════════════════════════════════════════════
# 消化态检测
# ═══════════════════════════════════════════════════════════════════

# [PHASE5-A] Search-only 检测 — grep: SEARCH_ONLY_DETECTION

_SEARCH_ONLY_TOOLS = frozenset({"search_bangumi_subject", "search_local_bangumi"})
"""只有这些工具被调用过 → search-only 模式。"""

_DETAIL_TOOLS = frozenset({
    "get_bangumi_subject_detail", "get_person_detail", "get_character_detail",
    "get_subject_opinions", "get_subject_characters", "get_subject_episodes",
    "get_entity_comments", "get_episode_comments",
})
"""这些工具被调过 → 不是 search-only。"""


def _detect_search_only_stall(messages: list, intent: str) -> bool:
    """检测是否陷入 search-only 模式——只搜了，没有跟进 detail。

    在消化态引导时使用：如果一条 detail 类工具都没调过，注入更强提示。

    Args:
        messages: 完整消息历史。
        intent: 查询意图。

    Returns:
        True 如果上一轮只用了 search 类工具。
    """
    # 收集本轮（从最后一个 HumanMessage 之后）的所有 ToolMessage
    tools_called: set[str] = set()
    for m in reversed(messages):
        if isinstance(m, ToolMessage) and getattr(m, "name", ""):
            tools_called.add(m.name)
        elif hasattr(m, "type") and m.type == "human":
            break

    if not tools_called:
        return False

    has_detail = bool(tools_called & _DETAIL_TOOLS)
    has_search = bool(tools_called & _SEARCH_ONLY_TOOLS)

    # 只调了 search 但没跟进 detail — 可能陷入 search-only
    return has_search and not has_detail


def _log_message_state(messages: list, iteration: int) -> None:
    """记录消息列表结构（DEBUG 级别）。"""
    if not logger.isEnabledFor(logging.DEBUG):
        return

    logger.debug("── 消息状态 (iter=%d, 共 %d 条) ──", iteration, len(messages))
    for i, m in enumerate(messages):
        mtype = type(m).__name__
        content = m.content if hasattr(m, "content") else str(m)
        if isinstance(content, str):
            preview = content[:200].replace("\n", "\\n")
        else:
            preview = str(content)[:200]

        if isinstance(m, ToolMessage):
            tc_id = getattr(m, "tool_call_id", "?")
            name = getattr(m, "name", "?")
            logger.debug(
                "  [%d] %s name=%s tc_id=%s content=%s",
                i, mtype, name, tc_id, content if isinstance(content, str) else str(content),
            )
        elif isinstance(m, AIMessage):
            tcs = getattr(m, "tool_calls", []) or []
            tc_names = [tc.get("name", "?") for tc in tcs]
            logger.debug("  [%d] %s tool_calls=%s preview=%s", i, mtype, tc_names, preview)
        else:
            logger.debug("  [%d] %s preview=%s", i, mtype, preview)
