"""
Dialogue Agent 节点函数

dialogue_reasoning_node：极简推理节点——记忆截断 → 意图分类 → LLM 调用。
无 Critic、无消化态引导指令。
"""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agent.classifier import classify_intent
from agent.dialogue.prompts import build_dialogue_prompt
from agent.dialogue.state import _MAX_ITERATIONS, DialogueState
from agent.guardrails import (
    check_duplicate_tool_calls,
    is_terminal_response,
    strip_tool_call_xml,
)
from agent.llm import create_llm
from agent.memory import DIALOGUE_MAX_TOKENS, manage_memory
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
        - error_flag 兜底——没有 Critic 触发 error_flag
        - critic_feedback 消费——没有 Critic

    Args:
        state: 当前 Dialogue Agent 全局状态。

    Returns:
        包含 messages、iterations、query_intent 更新的字典。
    """
    new_iterations = state.get("iterations", 0) + 1

    messages = state.get("messages", [])

    # ── Step 2: 意图分类（仅第一轮） ───────────────────────
    query_intent = state.get("query_intent", "unknown")
    intent_method = "cached"

    if state.get("iterations", 0) == 0:
        user_input = _extract_user_input(state)
        if user_input:
            classifier_llm = create_llm(temperature=0, max_tokens=10, request_timeout=10)
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

    # ── Step 3: 构建消息列表（不含截断——截断在重复检测后执行） ──
    system_content = build_dialogue_prompt()
    messages_for_llm = [SystemMessage(content=system_content)]

    skipped_system = 0
    for m in messages:
        if isinstance(m, SystemMessage):
            skipped_system += 1
            continue
        messages_for_llm.append(m)
    if skipped_system > 0:
        logger.debug("dialogue: 跳过 %d 条旧 SystemMessage，使用新 SystemPrompt", skipped_system)

    # ── Step 4: LLM 调用 ──────────────────────────────────
    llm = create_llm()

    is_digesting = messages and isinstance(messages[-1], ToolMessage)
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

    # ── 重复工具调用检测 ───────────────────────────────────
    # 检测 LLM 是否连续两轮调用相同工具/参数——如果工具返回空或错误，
    # LLM 可能陷入无效重试。检测到重复时注入引导指令。
    dup_feedback = check_duplicate_tool_calls(messages)
    if dup_feedback:
        logger.info("dialogue: 检测到重复工具调用 → 注入引导指令")
        messages_for_llm.append(
            HumanMessage(
                content=(
                    f"（系统指令：{dup_feedback}。"
                    "如果数据确实不存在，直接告诉用户并给出建议，不要继续搜索。）"
                )
            )
        )

    # ── 记忆截断（在完整消息列表构建后执行） ──
    # 在 prompt 构建和重复检测引导追加之后执行截断，确保
    # manage_memory 感知完整的 SystemPrompt + 注入指令的实际大小。
    # Dialogue Agent 预算为 4000 tokens（比 Research 的 8000 更紧）。
    messages_for_llm = manage_memory(messages_for_llm, max_tokens=DIALOGUE_MAX_TOKENS)

    try:
        response: AIMessage = await llm_to_use.ainvoke(messages_for_llm)
    except Exception as e:
        logger.exception("dialogue_reasoning_node: LLM 调用失败")
        return {
            "messages": [AIMessage(content=f"啧，脑子短路了。{e}")],
            "query_intent": query_intent,
            "iterations": new_iterations,
        }

    # ── Step 5: 终端回复逃逸舱 ─────────────────────────────
    # 如果当前在消化工具结果，且 LLM 回复表明数据不存在/建议调整搜索等，
    # 提前终止迭代——不需要等到 _MAX_ITERATIONS 熔断。
    if is_digesting and response.content and is_terminal_response(response.content):
        logger.info("dialogue: 终端回复（逃逸舱）→ 强制结束")
        new_iterations = _MAX_ITERATIONS  # 让路由函数熔断到 END

    # ── Step 6: XML 泄漏防护 ──────────────────────────────
    # 两种情况会触发 XML 泄漏：
    # 1. chitchat/factual 无工具通道 — DeepSeek 在无 function-calling 通道时
    #    将 <function_calls> 喷到 .content
    # 2. 消化态 — 工具已绑定但模型仍可能在 .content 中输出 XML 标签
    needs_xml_guard = (
        query_intent in _NO_TOOL_INTENTS  # 无工具通道
        or is_digesting                    # 消化态
    )
    if needs_xml_guard and response.content:
        cleaned, was_stripped = strip_tool_call_xml(response.content)
        if was_stripped:
            logger.warning(
                "dialogue: %s 回复中检测到 XML 泄漏，已清理",
                "chitchat/factual" if query_intent in _NO_TOOL_INTENTS else "消化态",
            )
            if not cleaned:
                cleaned = "啧，脑子有点乱，你再说一遍？"
            response = AIMessage(
                content=cleaned,
                response_metadata=getattr(response, "response_metadata", {}),
                id=getattr(response, "id", None),
            )

    # ── Step 7: 日志 ──────────────────────────────────────
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
