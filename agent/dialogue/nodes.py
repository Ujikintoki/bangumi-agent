"""
Dialogue Agent 节点函数

dialogue_reasoning_node：极简推理节点——记忆截断 → 意图分类 → LLM 调用。
无 Critic、无消化态引导指令、无 XML 安全网——拓扑保证工具始终绑定。
"""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agent.classifier import classify_intent
from agent.dialogue.prompts import build_dialogue_prompt
from agent.dialogue.state import DialogueState
from agent.llm import create_llm
from agent.memory import manage_memory
from tools.bgm_tools import get_agent_tools

logger = logging.getLogger("bgm-agent.dialogue")

# 不绑定工具的意图（LLM 直接回复）
_NO_TOOL_INTENTS = frozenset({"chitchat", "factual"})


async def dialogue_reasoning_node(state: DialogueState) -> dict:
    """Dialogue 推理节点：意图分类 + LLM function-calling 决策。

    流程（比 Research reasoning_node 简化 ~60%）：
        1. 记忆截断（manage_memory，同 Research）
        2. 意图分类（仅首轮，复用 classify_intent）
        3. 构建 System Prompt（Bangumi娘人格）
        4. LLM 调用：chitchat/factual 不绑工具，其余绑工具并让模型自主判断

    不需要的东西（vs Research）：
        - 消化态引导指令——模型自己会停
        - XML 泄漏安全网——工具始终绑定，不会泄漏到 .content
        - error_flag 兜底——没有 Critic 触发 error_flag
        - critic_feedback 消费——没有 Critic

    Args:
        state: 当前 Dialogue Agent 全局状态。

    Returns:
        包含 messages、iterations、query_intent 更新的字典。
    """
    new_iterations = state.get("iterations", 0) + 1

    # ── Step 1: 记忆截断 ───────────────────────────────────
    messages = state.get("messages", [])
    trimmed_messages = manage_memory(messages)

    # ── Step 2: 意图分类（仅第一轮） ───────────────────────
    query_intent = state.get("query_intent", "unknown")
    intent_method = "cached"

    if state.get("iterations", 0) == 0:
        user_input = _extract_user_input(state)
        if user_input:
            classifier_llm = create_llm(temperature=0, max_tokens=10)
            query_intent, intent_method = await classify_intent(user_input, classifier_llm)
            logger.info(
                "[Dialogue Intent] query='%s' → intent=%s (method=%s)",
                user_input[:80],
                query_intent,
                intent_method,
            )
        else:
            query_intent = "unknown"
            intent_method = "rule(empty)"

    # ── Step 3: 构建消息列表 ─────────────────────────────
    system_content = build_dialogue_prompt()
    messages_for_llm = [SystemMessage(content=system_content)]

    for m in trimmed_messages:
        if isinstance(m, SystemMessage):
            continue
        messages_for_llm.append(m)

    # ── Step 4: LLM 调用 ──────────────────────────────────
    llm = create_llm()

    is_digesting = trimmed_messages and isinstance(trimmed_messages[-1], ToolMessage)
    if is_digesting:
        logger.debug("dialogue_reasoning_node: 消化态 — 最后一条消息为 ToolMessage")

    if query_intent in _NO_TOOL_INTENTS:
        llm_to_use = llm
        logger.debug("dialogue_reasoning_node: intent=%s → 不绑定工具", query_intent)
    else:
        tools = get_agent_tools()
        llm_to_use = llm.bind_tools(tools)
        logger.debug(
            "dialogue_reasoning_node: intent=%s → 绑定 %d 个工具%s",
            query_intent,
            len(tools),
            " (消化态)" if is_digesting else "",
        )

    try:
        response: AIMessage = await llm_to_use.ainvoke(messages_for_llm)
    except Exception as e:
        logger.exception("dialogue_reasoning_node: LLM 调用失败")
        return {
            "messages": [AIMessage(content=f"啧，脑子短路了。{e}")],
            "query_intent": query_intent,
            "iterations": new_iterations,
        }

    # ── Step 5: 日志 ──────────────────────────────────────
    tool_calls = (
        list(response.tool_calls)
        if hasattr(response, "tool_calls") and response.tool_calls
        else []
    )

    logger.info(
        "[Dialogue] intent=%s iterations=%d tool_calls=%s",
        query_intent,
        new_iterations,
        [tc.get("name", "?") for tc in tool_calls],
    )

    return {
        "messages": [response],
        "iterations": new_iterations,
        "query_intent": query_intent,
    }


def _extract_user_input(state: DialogueState) -> str:
    """从消息历史中提取用户原始输入。"""
    messages = state.get("messages", [])
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            return m.content if hasattr(m, "content") else str(m)
    return ""
