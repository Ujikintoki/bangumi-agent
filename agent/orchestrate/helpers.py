"""
两个 Agent 共享的推理辅助函数。

纯函数/async 函数，不依赖具体的 State TypedDict（通过 .get() 访问字典字段）。
提取自 research/nodes.py 和 dialogue/nodes.py 的重复代码。
"""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agent.orchestrate.classifier import classify_intent
from agent.orchestrate.guardrails import strip_tool_call_xml

logger = logging.getLogger("bgm-agent.reasoning_core")


def extract_user_input(state: dict) -> str:
    """从消息历史中提取最后一条 HumanMessage 的文本。

    用于意图分类器和记忆召回——只需要用户的原始问题，不需要对话上下文。

    Args:
        state: 包含 ``messages`` 列表的字典。

    Returns:
        用户原始输入文本。未找到时返回空字符串。
    """
    messages = state.get("messages", [])
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            return m.content if hasattr(m, "content") else str(m)
    return ""


async def classify_intent_step(state: dict) -> tuple[str, str, bool]:
    """意图分类：仅首轮（iterations==0）执行，后续轮次复用缓存。

    使用轻量 LLM（temperature=0, max_tokens=10）做单阶段分类。

    Args:
        state: 包含 ``iterations``、``query_intent``、``messages`` 的字典。

    Returns:
        ``(query_intent, method, did_classify)`` 三元组。
        - ``method``: ``"cached"``（复用）、``"llm"``（新分类）
        - ``did_classify``: True 表示本函数执行了分类（调用方据此决定是否打 log）
    """
    query_intent = state.get("query_intent", "unknown")

    # 后续轮次复用首轮结果
    if state.get("iterations", 0) != 0:
        return query_intent, "cached", False

    user_input = extract_user_input(state)
    if not user_input:
        return "unknown", "llm", False

    from agent.llm import create_llm

    classifier_llm = create_llm(temperature=0, max_tokens=10, request_timeout=10)
    query_intent, method = await classify_intent(user_input, classifier_llm)
    return query_intent, method, True


async def recall_memory_step(
    state: dict,
    *,
    max_tokens: int,
    recall_threshold: float | None = None,
) -> str:
    """L2 跨会话记忆召回：语义检索 + 格式化。

    仅在 ``_memory_context`` 为空且 ``user_id != "anonymous"`` 时触发。
    语义阈值自然过滤不相关查询，无需按 intent 跳过。

    Args:
        state: 包含 ``_memory_context``、``user_id``、``messages`` 的字典。
        max_tokens: 注入文本的最大 Token 数。
        recall_threshold: 语义检索的余弦距离阈值。None 时使用全局默认值。

    Returns:
        格式化的记忆文本，无相关记忆或跳过时返回空字符串。
    """
    memory_context = state.get("_memory_context", "")
    if memory_context:
        return memory_context

    user_id = state.get("user_id", "anonymous")
    if user_id == "anonymous":
        return ""

    user_query = extract_user_input(state)
    if not user_query:
        return ""

    try:
        from agent.memory.long_term import get_memory_manager

        mm = get_memory_manager()
        kwargs: dict = {"user_id": user_id, "query": user_query, "max_tokens": max_tokens}
        if recall_threshold is not None:
            kwargs["recall_threshold"] = recall_threshold

        result = await mm.recall_for_prompt(**kwargs)
        if result:
            logger.info(
                "[Memory] 召回 %d 字 (user=%s, threshold=%s)",
                len(result),
                user_id,
                recall_threshold or "default",
            )
        return result
    except Exception:
        logger.warning("[Memory] 召回异常 (user=%s)", user_id, exc_info=True)
        return ""


def build_message_list(messages: list, system_content: str) -> list:
    """用新 SystemMessage 替换旧的，追加历史消息。

    每个推理轮次都重建消息列表——System Prompt 可能因 critic_feedback、
    last_chance 指令等发生变化。

    Args:
        messages: 当前 state 中的完整消息历史。
        system_content: 新的 System Prompt 文本。

    Returns:
        以新 SystemMessage 开头、历史非 SystemMessage 追加在后的列表。
    """
    result = [SystemMessage(content=system_content)]

    skipped = 0
    for m in messages:
        if isinstance(m, SystemMessage):
            skipped += 1
            continue
        result.append(m)

    if skipped > 0:
        logger.debug("跳过 %d 条旧 SystemMessage，使用新的 SystemPrompt", skipped)

    return result


def guard_xml_leak(
    response: AIMessage,
    *,
    is_digesting: bool,
    fallback_text: str,
    log: logging.Logger | None = None,
) -> AIMessage:
    """消化态 XML 泄漏安全网。

    DeepSeek 等模型在消化工具结果时可能在 .content 中输出
    ``<function_calls>`` XML 标签。检测并剥离，防止脏数据进入路由器和 Critic。

    仅在 ``is_digesting=True`` 且 response.content 非空时执行检查。

    Args:
        response: LLM 返回的 AIMessage。
        is_digesting: 当前是否在消化工具结果。
        fallback_text: XML 剥离后内容为空时的兜底文案。
        log: 可选的 logger。None 时使用模块级 logger。

    Returns:
        清理后的 AIMessage（可能为原 response 或新建的兜底 AIMessage）。
    """
    _log = log or logger

    if not is_digesting or not response.content:
        return response

    cleaned, was_stripped = strip_tool_call_xml(response.content)
    if not was_stripped:
        return response

    _log.warning("消化态检测到泄露的工具调用 XML，已自动清理")
    if not cleaned:
        _log.warning("消化态 XML 剥离后内容为空，使用兜底回复")
        cleaned = fallback_text

    return AIMessage(
        content=cleaned,
        response_metadata=getattr(response, "response_metadata", {}),
        id=getattr(response, "id", None),
    )
