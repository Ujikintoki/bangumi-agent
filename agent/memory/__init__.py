"""记忆层 — 能记住什么。

L1 短记忆（滑动窗口截断）+ L2 跨会话语义记忆（pgvector 语义召回 + 时间衰减）。
"""

from agent.memory.short_term import (  # noqa: F401
    DEFAULT_MAX_TOKENS,
    DEPTH_TOKEN_BUDGETS,
    count_tokens,
    estimate_tokens,
    manage_memory,
    trim_messages,
)
from agent.memory.cache import get_session_cache  # noqa: F401


def __getattr__(name):
    # 保留公开导入兼容；基础 L1 不加载 PostgreSQL 依赖。
    if name in {"MemoryManager", "get_memory_manager"}:
        from agent.memory import long_term
        return getattr(long_term, name)
    raise AttributeError(name)



__all__ = [
    "DEFAULT_MAX_TOKENS",
    "DEPTH_TOKEN_BUDGETS",
    "MemoryManager",
    "count_tokens",
    "estimate_tokens",
    "get_memory_manager",
    "get_session_cache",
    "manage_memory",
    "trim_messages",
]
