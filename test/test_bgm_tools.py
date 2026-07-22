"""
工具输出格式测试

验证 compact 模式（Dialogue）vs full 模式（Research）的输出差异，
以及 search_local_bangumi 异步化。
"""
from __future__ import annotations

import asyncio
import contextvars
import inspect

import pytest

from agent.memory import count_tokens
from tools.bgm_tools import (
    _format_subject_detail,
    _format_subject_detail_compact,
    _format_subject_detail_discovery,
    _format_subject_detail_full,
    _format_character_detail,
    _format_person_detail,
    _get_agent_type,
    _tool_agent_type,
    search_local_bangumi,
    set_tool_agent_type,
)


# ═══════════════════════════════════════════════════════════════════════════
# 测试数据
# ═══════════════════════════════════════════════════════════════════════════


def _make_detail() -> dict:
    """构造一个典型的条目详情 dict。"""
    return {
        "id": 8,
        "name": "機動戦士ガンダムSEED",
        "name_cn": "机动战士高达SEED",
        "type": "动画",
        "eps": 50,
        "score": 7.3,
        "rank": 1277,
        "total_rating_count": 4694,
        "rating_count": [89, 45, 67, 112, 234, 456, 1234, 1345, 567, 230],
        "collection": {"1": 1234, "2": 45678, "3": 567, "4": 234, "5": 89},
        "summary": "在宇宙世纪的基础上，调整者与自然人的战争。少年基拉·大和被迫驾驶强袭高达，卷入了一场改变世界的冲突。",
        "tags": [
            {"name": "高达", "count": 2345},
            {"name": "SEED", "count": 1567},
            {"name": "机战", "count": 1234},
            {"name": "偶像剧", "count": 317},
            {"name": "福田", "count": 234},
            {"name": "萝卜", "count": 189},
        ],
    }


def _make_character_detail() -> dict:
    return {
        "id": 100,
        "name": "キラ・ヤマト",
        "name_cn": "基拉·大和",
        "role": "主角",
        "info": "CV: 保志総一朗",
        "summary": "调整者少年，被迫驾驶强袭高达。性格温和但战斗力极强，" * 10,
        "collects": 5678,
        "nsfw": False,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Agent type contextvar
# ═══════════════════════════════════════════════════════════════════════════


class TestAgentTypeContextVar:
    def test_default_is_research(self):
        assert _get_agent_type() == "research"

    def test_set_and_get(self):
        set_tool_agent_type("dialogue")
        assert _get_agent_type() == "dialogue"
        set_tool_agent_type("research")  # restore

    def test_context_isolation(self):
        """contextvars 应在不同 Context 中隔离。"""
        ctx1 = contextvars.copy_context()
        ctx2 = contextvars.copy_context()

        ctx1.run(set_tool_agent_type, "dialogue")
        ctx2.run(set_tool_agent_type, "research")

        assert ctx1.run(_get_agent_type) == "dialogue"
        assert ctx2.run(_get_agent_type) == "research"


# ═══════════════════════════════════════════════════════════════════════════
# Compact vs Full 输出
# ═══════════════════════════════════════════════════════════════════════════


class TestSubjectDetailCompact:
    def test_compact_has_key_fields(self):
        detail = _make_detail()
        set_tool_agent_type("dialogue")
        result = _format_subject_detail(detail, intent="lookup")
        set_tool_agent_type("research")  # restore

        assert "机动战士高达SEED" in result
        assert "评分 7.3" in result
        assert "排名 #1277" in result
        assert "4694 人评" in result
        assert "类型: 动画" in result
        assert "集数: 50" in result
        assert "条目 8 详情" in result

    def test_compact_excludes_heavy_fields(self):
        detail = _make_detail()
        set_tool_agent_type("dialogue")
        result = _format_subject_detail(detail, intent="lookup")
        set_tool_agent_type("research")

        assert "评分分布" not in result
        assert "收藏分布" not in result
        assert "📊 信号" not in result
        # top-5 应包含前5个标签，不应包含第6个"萝卜"
        assert "高达" in result
        assert "萝卜" not in result  # 第 6 个标签，不在 top-5

    def test_compact_smaller_than_full(self):
        detail = _make_detail()
        set_tool_agent_type("dialogue")
        compact = _format_subject_detail(detail, intent="lookup")
        set_tool_agent_type("research")
        full = _format_subject_detail(detail, intent="lookup")

        compact_tokens = count_tokens(compact)
        full_tokens = count_tokens(full)
        assert compact_tokens < full_tokens * 0.7, (
            f"compact={compact_tokens}, full={full_tokens}"
        )

    def test_full_still_has_heavy_fields(self):
        """Research 模式下全量字段应存在。"""
        detail = _make_detail()
        # default is research
        result = _format_subject_detail(detail, intent="lookup")
        assert "评分分布" in result
        assert "收藏分布" in result
        assert "📊 信号" in result
        assert "萝卜" in result  # 第 6 个标签，full 模式 top-10 包含

    def test_discovery_is_minimal(self):
        """discovery 意图下走极简模式（不受 agent_type 影响）。"""
        detail = _make_detail()
        set_tool_agent_type("research")
        result = _format_subject_detail(detail, intent="discovery")
        set_tool_agent_type("research")

        assert "评分分布" not in result
        assert "简介" not in result
        assert "条目" not in result  # discovery 格式无 footer


class TestCharacterDetailCompact:
    def test_dialogue_truncates_background(self):
        detail = _make_character_detail()
        set_tool_agent_type("dialogue")
        result = _format_character_detail(detail)
        set_tool_agent_type("research")

        assert "基拉·大和" in result
        assert "背景" in result
        # 背景应截断至 ~100 字
        bg_start = result.index("背景：")
        bg_text = result[bg_start:]
        assert len(bg_text) < 300  # 原始 10x 重复很长，截断后应 < 300 chars

    def test_research_keeps_full_background(self):
        detail = _make_character_detail()
        # default is research
        result = _format_character_detail(detail)
        assert "基拉·大和" in result
        # 原始背景 10x 重复 ~300+ chars，应全保留
        bg_start = result.index("背景：")
        bg_text = result[bg_start:]
        assert len(bg_text) > 200  # research 保留完整


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
