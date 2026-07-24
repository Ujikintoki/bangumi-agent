"""
工具输出格式测试

验证 search_local_bangumi 异步化。
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from tools.bgm_tools import (
    search_local_bangumi,
)


# ═══════════════════════════════════════════════════════════════════════════
# Agent type contextvar — removed (last consumer _format_person_detail deleted)
# ═══════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════
# search_local_bangumi 异步化
# ═══════════════════════════════════════════════════════════════════════════


class TestSearchLocalAsync:
    def test_underlying_func_is_async(self):
        """底层函数应为 async def（@tool 装饰器将其包装为 StructuredTool）。"""
        # LangChain @tool 将 async def 包装为 StructuredTool，coroutine 属性指向原函数
        coro_func = getattr(search_local_bangumi, "coroutine", None)
        assert coro_func is not None, "search_local_bangumi 应有 coroutine 属性"
        assert inspect.iscoroutinefunction(coro_func)

    def test_no_block_event_loop(self):
        """调用不应阻塞事件循环（即使 RAG 不可用，函数应在超时前返回）。"""
        coro_func = search_local_bangumi.coroutine
        result = asyncio.run(
            asyncio.wait_for(
                coro_func("测试查询", limit=1),
                timeout=5.0,
            )
        )
        assert isinstance(result, str)
        assert len(result) > 0
