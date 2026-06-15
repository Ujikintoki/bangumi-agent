"""
L2/L3 MemoryManager 测试

覆盖 _format_memory_context、_format_profile_summary、_extract_key_entities、
_format_conversation_text 等纯函数，加上 DB 读写冒烟。
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agent.memory_manager import MemoryManager, get_memory_manager


# ═══════════════════════════════════════════════════════════════════════════
# 纯函数测试
# ═══════════════════════════════════════════════════════════════════════════


class TestExtractKeyEntities:
    """_extract_key_entities — 已废弃（Bug 3 修复）

    原正则提取（「」""）已被 _summarize_session 的 LLM JSON 输出替代。
    方法保留仅为向后兼容，所有输入返回空列表。
    """

    def test_chinese_brackets_deprecated(self):
        """废弃方法：中文书名号不再提取，返回 []"""
        entities = MemoryManager._extract_key_entities(
            "用户询问了「高达Seed」和「星际牛仔」的评分"
        )
        assert entities == []

    def test_chinese_quotes_deprecated(self):
        """废弃方法：中文引号不再提取，返回 []"""
        entities = MemoryManager._extract_key_entities(
            '用户对"进击的巨人"表现出强烈兴趣'
        )
        assert entities == []

    def test_deduplication_deprecated(self):
        """废弃方法：去重逻辑不再适用，返回 []"""
        entities = MemoryManager._extract_key_entities(
            "用户提到了「高达Seed」，对「高达Seed」评价很高"
        )
        assert entities == []

    def test_empty_summary(self):
        assert MemoryManager._extract_key_entities("") == []

    def test_no_brackets(self):
        entities = MemoryManager._extract_key_entities("用户想找好看的动漫")
        assert entities == []


class TestFormatConversationText:
    """_format_conversation_text — 消息列表转纯文本"""

    def test_basic_conversation(self):
        messages = [
            SystemMessage(content="你是助手"),
            HumanMessage(content="推荐机战番"),
            AIMessage(content="推荐高达Seed，评分8.5"),
        ]
        text = MemoryManager._format_conversation_text(messages, "最终回复")
        assert "用户: 推荐机战番" in text
        assert "助手: 推荐高达Seed" in text
        assert "助手: 最终回复" in text
        assert "你是助手" not in text  # SystemMessage 被过滤

    def test_tool_messages_filtered(self):
        # 注意：ToolMessage 被过滤，只有 HumanMessage 和有 content 的 AIMessage 保留
        messages = [
            HumanMessage(content="搜索高达"),
            ToolMessage(content='{"id": 1}', name="search", tool_call_id="t1"),
            AIMessage(content="找到高达Seed"),
        ]
        text = MemoryManager._format_conversation_text(messages, "")
        assert "用户: 搜索高达" in text
        assert "找到高达Seed" in text
        assert '{"id": 1}' not in text  # ToolMessage 被过滤
        assert "search" not in text  # tool name 也不该出现


class TestFormatMemoryContext:
    """_format_memory_context — 记忆上下文格式化"""

    @staticmethod
    def _make_mock_session(summary: str, days_ago: int = 1):
        """构造最小 mock SessionMemory。"""
        from datetime import timedelta

        sm = MagicMock()
        sm.summary_text = summary
        sm.created_at = datetime.now(timezone.utc) - timedelta(days=days_ago)
        return sm

    def test_formats_session_with_time_string(self):
        sm = self._make_mock_session("用户询问了高达Seed", days_ago=0)
        result = MemoryManager._format_memory_context(
            MagicMock(), [sm], None, max_tokens=500
        )
        assert "今天" in result
        assert "用户询问了高达Seed" in result
        assert "用户历史" in result

    def test_empty_sessions_no_profile_returns_empty(self):
        mm = MagicMock()
        result = MemoryManager._format_memory_context(mm, [], None, max_tokens=500)
        assert result == ""

    def test_includes_profile_when_present(self):
        profile = MagicMock()
        profile.preferences_json = {
            "favorite_genres": [{"genre": "机战", "count": 5}],
            "entity_affinities": {"高达Seed": {"name": "高达Seed", "interest_score": 0.8}},
        }
        profile.total_sessions = 10

        # _format_memory_context 是实例方法，调用 self._format_profile_summary。
        # 用 MagicMock 作 self 并 patch _format_profile_summary 返回实际文本
        mm_mock = MagicMock()
        mm_mock._format_profile_summary = lambda p: MemoryManager._format_profile_summary(p)

        result = MemoryManager._format_memory_context(
            mm_mock, [], profile, max_tokens=500
        )
        assert "机战" in result


class TestFormatProfileSummary:
    """_format_profile_summary — 画像摘要文本"""

    def test_genres_and_affinities(self):
        profile = MagicMock()
        profile.preferences_json = {
            "favorite_genres": [
                {"genre": "机战", "count": 5},
                {"genre": "科幻", "count": 3},
            ],
            "entity_affinities": {
                "高达Seed": {"name": "高达Seed", "interest_score": 0.9},
            },
        }
        summary = MemoryManager._format_profile_summary(profile)
        assert "机战" in summary
        assert "高达Seed" in summary

    def test_empty_prefs(self):
        profile = MagicMock()
        profile.preferences_json = {}
        assert MemoryManager._format_profile_summary(profile) == ""

    def test_none_prefs(self):
        profile = MagicMock()
        profile.preferences_json = None
        assert MemoryManager._format_profile_summary(profile) == ""


# ═══════════════════════════════════════════════════════════════════════════
# DB 冒烟测试
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.database
class TestSessionMemoryDB:
    """session_memories 表读写冒烟。需要 PostgreSQL + pgvector。"""

    @pytest.fixture(autouse=True)
    def _init_tables(self):
        from database.engine import init_db

        init_db()

    def test_insert_and_query(self):
        """写入 session_memory → 按 user_id 查询成功"""
        from uuid import uuid4

        from sqlmodel import Session, select

        from database.engine import engine
        from database.memory_tables import SessionMemory

        sid = str(uuid4())
        uid = "test_mem_user"

        with Session(engine) as db:
            sm = SessionMemory(
                session_id=sid,
                user_id=uid,
                summary_text="测试摘要：用户询问高达Seed的评分",
                key_entities=[{"type": "subject", "name": "高达Seed"}],
                intent_distribution={"lookup": 1},
            )
            db.add(sm)
            db.commit()

            result = db.exec(
                select(SessionMemory).where(SessionMemory.user_id == uid)
            ).first()

        assert result is not None
        assert result.summary_text == "测试摘要：用户询问高达Seed的评分"
        assert "高达Seed" in str(result.key_entities)

        # 清理
        with Session(engine) as db:
            db.delete(result)
            db.commit()

    def test_anonymous_guard(self):
        """user_id='anonymous' 时 recall_for_prompt 返回空字符串"""
        import asyncio

        mm = get_memory_manager()
        result = asyncio.run(
            mm.recall_for_prompt(user_id="anonymous", query="测试查询")
        )
        assert result == ""
