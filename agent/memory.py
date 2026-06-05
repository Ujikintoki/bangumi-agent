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
_ENCODER = tiktoken.get_encoding("cl100k_base")

# 默认 Token 预算
DEFAULT_MAX_TOKENS = 8000


def count_tokens(text: str) -> int:
    """精确 Token 计数（tiktoken cl100k_base）。

    Args:
        text: 任意文本。

    Returns:
        Token 数量。
    """
    return len(_ENCODER.encode(text))


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
    """记忆管理入口：检查 Token 预算，超限时截断。

    在 reasoning_node 开头调用。如果未超预算则原样返回，
    避免不必要的消息复制。

    Args:
        messages: 当前消息列表。
        max_tokens: Token 预算上限。

    Returns:
        可能截断后的消息列表。
    """
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
