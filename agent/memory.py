"""
短期记忆管理 — Layer 1 滑动窗口截断

使用 tiktoken ``cl100k_base`` 精确计数（非 ``len//4`` 估算），
在 Token 预算超限时从头部截断旧消息，保留 SystemMessage 和最近消息。

设计决策：
    - 使用 tiktoken 而非 len//4 估算：中文单字占 1.5-2.5 tokens，JSON 中大括号/引号
      各占 1 token，生产环境中 len//4 会低估 30-50%，导致 context_length_exceeded。
    - 编码器选用 ``cl100k_base``：GPT-4/DeepSeek/Qwen 的通用编码，无需按模型切换。
    - 触发时机在 reasoning_node 开头（方式二）：不改图拓扑，实现更简单。
      工具返回的数据量最不可控，在进入下一轮推理前截断最可靠。

用法::

    from agent.memory import manage_memory

    messages = manage_memory(state["messages"], max_tokens=8000)
"""

from __future__ import annotations

import logging

import tiktoken
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

logger = logging.getLogger("bgm-agent.memory")

# ── 全局编码器（cl100k_base 是 GPT-4/DeepSeek/Qwen 的通用编码） ─
try:
    _ENCODER = tiktoken.get_encoding("cl100k_base")
except Exception:
    logger.warning(
        "tiktoken 编码器初始化失败（版本不兼容或编码缺失），"
        "将回退到 len//2 字符估算。建议: pip install tiktoken>=0.5.0"
    )
    _ENCODER = None

# ── Token 预算分配（Phase 5 显式划分） ─
DEFAULT_MAX_TOKENS = 8000
"""Research Agent 总 Token 预算。

分配：
  System Prompt (BASE + intent):  ~1200 tokens
  L2 记忆注入:                    ≤500 tokens
  对话历史:                       ~5300 tokens
  LLM 输出缓冲:                   ~1000 tokens
"""

DIALOGUE_MAX_TOKENS = 4000
"""Dialogue Agent 总 Token 预算。

分配：
  System Prompt (Bangumi娘人格):   ~600 tokens
  L2 记忆注入:                    ≤300 tokens
  对话历史:                       ~2500 tokens
  LLM 输出缓冲:                    ~600 tokens
"""

L2_MEMORY_BUDGET_TOKENS = 500
"""L2 记忆注入预留 Token 数（Research Agent）。"""

L2_MEMORY_BUDGET_DIALOGUE = 300
"""L2 记忆注入预留 Token 数（Dialogue Agent）。"""

# 单条消息最大 Token 数（超出则截断内容，主要针对 ToolMessage 返回的海量 JSON）
_MAX_SINGLE_MESSAGE_TOKENS = 2000

# 截断标记
_TRUNCATION_MARKER = "\n\n...[内容已截断]"


def count_tokens(text: str) -> int:
    """精确 Token 计数（tiktoken cl100k_base）。

    Args:
        text: 任意文本。

    Returns:
        Token 数量。编码失败时回退到 ``len(text) // 2`` 估算。
    """
    try:
        return len(_ENCODER.encode(text))
    except Exception:
        logger.warning("tiktoken encode 失败，使用 len//2 估算（%d 字符）", len(text))
        return max(1, len(text) // 2)


def estimate_tokens(messages: list[BaseMessage]) -> int:
    """精确计算消息列表的 Token 总数。

    对每条消息的 ``content`` 字段进行 tiktoken 编码计数。
    支持 ``content`` 为 ``str`` 或 ``list[dict]``（如 AIMessage 的多模态 content）。

    Args:
        messages: LangChain 消息列表。

    Returns:
        所有消息的总 Token 数。
    """
    total = 0
    for m in messages:
        content = m.content if hasattr(m, "content") else str(m)
        if isinstance(content, str):
            total += count_tokens(content)
        elif isinstance(content, list):
            # AIMessage content 可能为 list[dict]（如 tool_call 结果）
            total += count_tokens(str(content))
        # ToolMessage 的 tool_call_id 不计入（非 LLM 消费内容）
    return total


def _truncate_text_by_tokens(text: str, max_tokens: int) -> str:
    """按 token 数精确截断文本（tiktoken 编码后截断再解码）。

    Args:
        text: 原始文本。
        max_tokens: 保留的最大 token 数。

    Returns:
        截断后的文本。编码失败时回退到字符截断。
    """
    try:
        tokens = _ENCODER.encode(text)
        if len(tokens) <= max_tokens:
            return text
        return _ENCODER.decode(tokens[:max_tokens])
    except Exception:
        logger.warning("tiktoken encode/decode 失败，使用字符截断")
        return text[: max_tokens * 2]  # 退避：中文字符约 2 tokens/字


def _truncate_message_content(msg: BaseMessage, max_tokens: int) -> BaseMessage:
    """截断单条消息内容到指定 token 预算内。

    保留消息元数据（ToolMessage 的 tool_call_id/name、
    AIMessage 的 tool_calls）。非字符串 content 不做截断。

    Args:
        msg: 原始消息。
        max_tokens: 内容 token 上限（含截断标记）。

    Returns:
        截断后的消息（新对象），无需截断时返回原消息。
    """
    content = msg.content if hasattr(msg, "content") else str(msg)
    if not isinstance(content, str):
        return msg

    current_tokens = count_tokens(content)
    if current_tokens <= max_tokens:
        return msg

    marker_tokens = count_tokens(_TRUNCATION_MARKER)
    available = max(50, max_tokens - marker_tokens)
    truncated = _truncate_text_by_tokens(content, available) + _TRUNCATION_MARKER

    logger.debug(
        "memory: 截断消息内容 %d→%d tokens", current_tokens, count_tokens(truncated)
    )

    if isinstance(msg, ToolMessage):
        return ToolMessage(
            content=truncated,
            tool_call_id=getattr(msg, "tool_call_id", ""),
            name=getattr(msg, "name", None),
        )
    elif isinstance(msg, AIMessage):
        new_msg = AIMessage(content=truncated)
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            new_msg.tool_calls = msg.tool_calls
        return new_msg
    elif isinstance(msg, HumanMessage):
        return HumanMessage(content=truncated)

    return msg


def _truncate_oversized_messages(
    messages: list[BaseMessage],
    max_single_tokens: int = _MAX_SINGLE_MESSAGE_TOKENS,
) -> list[BaseMessage]:
    """截断超过单条上限的消息内容（主要针对 ToolMessage 海量 JSON）。

    在列表级截断之前执行，防止一条 ToolMessage 挤占全部上下文窗口。

    Args:
        messages: 消息列表。
        max_single_tokens: 单条消息 token 上限。

    Returns:
        新列表；无变化时返回原列表避免不必要的复制。
    """
    changed = False
    result: list[BaseMessage] = []
    for m in messages:
        content = m.content if hasattr(m, "content") else str(m)
        if isinstance(content, str) and count_tokens(content) > max_single_tokens:
            result.append(_truncate_message_content(m, max_single_tokens))
            changed = True
            logger.info(
                "memory: 截断超大消息 (%s)，%d → ≤%d tokens",
                type(m).__name__,
                count_tokens(content),
                max_single_tokens,
            )
        else:
            result.append(m)
    return result if changed else messages


def trim_messages(
    messages: list[BaseMessage],
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> list[BaseMessage]:
    """滑动窗口截断：保留 SystemMessage + 最近消息。

    策略：
        1. SystemMessage 始终保留（系统提示词不可丢失）
        2. 从消息列表尾部向头部遍历，逐条加入直到 Token 预算耗尽
        3. 超出预算的旧消息被丢弃

    Args:
        messages: 完整消息列表。
        max_tokens: Token 预算上限。默认 8000。

    Returns:
        截断后的消息列表。
    """
    # 分离系统消息（始终保留）
    system_msgs: list[BaseMessage] = [m for m in messages if isinstance(m, SystemMessage)]
    other_msgs: list[BaseMessage] = [m for m in messages if not isinstance(m, SystemMessage)]

    # 计算系统消息的 Token 开销
    system_tokens = estimate_tokens(system_msgs)

    # 从尾部向头部保留（保留最近的消息）
    kept: list[BaseMessage] = []
    token_count = system_tokens

    for m in reversed(other_msgs):
        estimated = estimate_tokens([m])
        if token_count + estimated > max_tokens:
            # 超大单条 ToolMessage：截断内容而非整条丢弃
            if isinstance(m, ToolMessage):
                remaining = max_tokens - token_count
                if remaining > 100:  # 至少保留 100 tokens 才有意义
                    truncated_m = _truncate_message_content(m, remaining)
                    kept.insert(0, truncated_m)
                    token_count += estimate_tokens([truncated_m])
                    logger.warning(
                        "memory: ToolMessage 超出预算，截断至 %d tokens (%s)",
                        remaining,
                        getattr(m, "name", "?"),
                    )
            break
        kept.insert(0, m)
        token_count += estimated

    trimmed_count = len(other_msgs) - len(kept)
    if trimmed_count > 0:
        logger.info(
            "memory: 截断 %d 条旧消息（%d → %d 条），Token: %d/%d",
            trimmed_count,
            len(other_msgs),
            len(kept),
            token_count,
            max_tokens,
        )

    return system_msgs + kept


def manage_memory(
    messages: list[BaseMessage],
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> list[BaseMessage]:
    """记忆管理入口：先截断超大单条消息，再检查总预算。

    两步策略：
        1. 单条截断：ToolMessage 超过 ``_MAX_SINGLE_MESSAGE_TOKENS``
           的内容先被截断，防止一条消息挤占全部上下文。
        2. 列表截断：总 Token 超预算时滑动窗口丢弃旧消息，
           ToolMessage 优先截断而非丢弃。

    在 reasoning_node 开头调用。

    Args:
        messages: 当前消息列表。
        max_tokens: Token 预算上限。

    Returns:
        可能截断后的消息列表。
    """
    # Step 0: 截断超大单条消息（主要针对 ToolMessage）
    messages = _truncate_oversized_messages(messages)

    # Step 1: 检查总预算
    current_tokens = estimate_tokens(messages)
    if current_tokens <= max_tokens:
        logger.debug("memory: Token %d ≤ 预算 %d，无需截断", current_tokens, max_tokens)
        return messages

    logger.info(
        "memory: Token %d > 预算 %d，触发滑动窗口截断",
        current_tokens,
        max_tokens,
    )
    return trim_messages(messages, max_tokens)
