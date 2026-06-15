"""
Session 级消息持久化缓存

为 L1 滑动窗口提供跨 HTTP 请求的消息恢复能力。
同一 session_id 的多轮 POST /chat 之间，前序消息从缓存注入
state["messages"]，使 L1 manage_memory 有数据可管理。

设计决策：
    - 仅内存：不落盘、不上数据库。重启即清空——正常，
      用户重新开始对话不需要旧上下文；L2 负责跨 session 语义记忆
    - TTL 淘汰：默认 1 小时。清理僵尸 session，防止内存泄漏
    - 容量上限：最多 1000 个 session。超限时淘汰最旧条目
    - 不缓存 SystemMessage：SystemMessage 每轮重建（含最新 L2 记忆 +
      critic feedback），缓存无意义且浪费内存
    - asyncio.Lock 保护：并发请求读写同一 session 时串行化

用法::

    from agent.session_cache import get_session_cache

    cache = get_session_cache()
    cached = await cache.load(session_id)
    # ... build initial_state with cached messages ...
    await cache.store(session_id, result["messages"])
"""

from __future__ import annotations

import asyncio
import logging
import time

from langchain_core.messages import SystemMessage

logger = logging.getLogger("bgm-agent.session_cache")

# 默认配置
DEFAULT_TTL_SECONDS = 3600   # 1 小时
DEFAULT_MAX_ENTRIES = 1000   # 最多 1000 个活跃 session


class SessionCache:
    """Session 级消息内存缓存。

    每个 session_id 维护一份消息列表（不含 SystemMessage），
    同一 session 的后续请求自动恢复前序对话上下文。

    Attributes:
        _cache: {session_id: (last_access_timestamp, messages)}
        _ttl: 缓存过期时间（秒）
        _max_entries: 最大缓存条目数
        _lock: 并发保护锁
    """

    def __init__(
        self,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        self._cache: dict[str, tuple[float, list]] = {}
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._lock = asyncio.Lock()

    async def load(self, session_id: str) -> list:
        """加载 session 的缓存消息。

        Args:
            session_id: 会话标识。

        Returns:
            消息列表（不含 SystemMessage）。未命中或已过期返回空列表。
            返回的是浅拷贝——调用方可安全修改。
        """
        async with self._lock:
            entry = self._cache.get(session_id)
            if entry is None:
                return []

            last_access, messages = entry

            # TTL 过期检查
            if time.time() - last_access >= self._ttl:
                del self._cache[session_id]
                logger.debug(
                    "session 缓存过期 (session=%s, age=%.0fs)",
                    session_id,
                    time.time() - last_access,
                )
                return []

            # 更新访问时间（LRU）
            self._cache[session_id] = (time.time(), messages)
            logger.debug(
                "session 缓存命中 (session=%s, messages=%d)",
                session_id,
                len(messages),
            )
            return list(messages)  # 浅拷贝，防外部修改

    async def store(
        self,
        session_id: str,
        messages: list,
        max_messages: int = 20,
    ) -> None:
        """存储 session 的最新消息列表。

        自动过滤 SystemMessage——每轮推理重建 System Prompt，
        缓存的 SystemMessage 无意义且占用内存。

        Args:
            session_id: 会话标识。
            messages: Agent Graph 返回的完整消息列表。
            max_messages: 最多保留的消息条数（不含 SystemMessage）。
                超出时只保留最后 N 条。默认 20。
        """
        # 过滤 SystemMessage
        filtered = [m for m in messages if not isinstance(m, SystemMessage)]

        if not filtered:
            return

        # 条目数限制：只保留最后 N 条，防止 L1 缓存无限膨胀
        if len(filtered) > max_messages:
            trimmed_count = len(filtered) - max_messages
            filtered = filtered[-max_messages:]
            logger.debug(
                "session 缓存截断 (session=%s, dropped=%d, kept=%d)",
                session_id,
                trimmed_count,
                len(filtered),
            )

        async with self._lock:
            # 容量保护：超限时淘汰最旧条目
            if len(self._cache) >= self._max_entries and session_id not in self._cache:
                try:
                    oldest_id = min(
                        self._cache,
                        key=lambda k: self._cache[k][0],
                    )
                    del self._cache[oldest_id]
                    logger.debug(
                        "session 缓存淘汰 (evicted=%s, entries=%d)",
                        oldest_id,
                        len(self._cache),
                    )
                except ValueError:
                    pass  # 并发删除导致 dict 为空，忽略

            self._cache[session_id] = (time.time(), filtered)
            logger.debug(
                "session 缓存写入 (session=%s, messages=%d, entries=%d)",
                session_id,
                len(filtered),
                len(self._cache),
            )

    async def clear(self, session_id: str) -> None:
        """手动清除指定 session 的缓存。

        Args:
            session_id: 要清除的会话标识。
        """
        async with self._lock:
            self._cache.pop(session_id, None)

    @property
    def size(self) -> int:
        """当前缓存的 session 数量（非线程安全，仅供监控）。"""
        return len(self._cache)


# ── 全局单例 ──────────────────────────────────────────

_cache_instance: SessionCache | None = None
"""全局 SessionCache 单例。由 get_session_cache() 延迟初始化。"""


def get_session_cache() -> SessionCache:
    """获取全局 SessionCache 单例。

    FastAPI 应用生命周期内唯一实例，所有请求共享同一缓存。
    延迟初始化——首次调用时创建，避免导入时的副作用。

    Returns:
        全局 SessionCache 实例。
    """
    global _cache_instance
    if _cache_instance is None:
        _cache_instance = SessionCache()
        logger.info("SessionCache 全局单例已初始化")
    return _cache_instance
