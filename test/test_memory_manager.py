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


# ═══════════════════════════════════════════════════════════════════════════
# 画像更新逻辑单元测试
# ═══════════════════════════════════════════════════════════════════════════


class TestUpdateGenres:
    """_update_genres() — 从实体名关键词推断类型频率"""

    def test_empty_entities_preserves_prefs(self):
        """空 entities 时 prefs 不变"""
        prefs = {"favorite_genres": [{"genre": "机战", "count": 3}]}
        result = MemoryManager._update_genres(prefs, [])
        assert result == prefs

    def test_new_entity_adds_genre(self):
        """新实体匹配关键词 → 新增类型条目"""
        prefs = {}
        entities = [{"type": "subject", "name": "高达SEED"}]
        result = MemoryManager._update_genres(prefs, entities)
        assert len(result["favorite_genres"]) == 1
        assert result["favorite_genres"][0] == {"genre": "机战", "count": 1}

    def test_existing_genre_increments_count(self):
        """已有类型 → count 累加"""
        prefs = {"favorite_genres": [{"genre": "机战", "count": 3}]}
        entities = [{"type": "subject", "name": "高达W"}]
        result = MemoryManager._update_genres(prefs, entities)
        assert result["favorite_genres"][0] == {"genre": "机战", "count": 4}

    def test_multiple_entities_same_genre(self):
        """多个实体同一类型 → count 累加多次"""
        prefs = {}
        entities = [
            {"name": "高达SEED"},
            {"name": "高达00"},
            {"name": "机器人笔记"},
        ]
        result = MemoryManager._update_genres(prefs, entities)
        assert result["favorite_genres"][0] == {"genre": "机战", "count": 3}

    def test_first_keyword_match_wins(self):
        """"高达机器人" 同时匹配"高达"和"机器人" → 只取第一个匹配"""
        prefs = {}
        entities = [{"name": "高达机器人"}]
        result = MemoryManager._update_genres(prefs, entities)
        # "高达" 在 genre_hints 中优先于 "机器人"，break 后不再匹配
        assert result["favorite_genres"][0] == {"genre": "机战", "count": 1}
        assert len(result["favorite_genres"]) == 1

    def test_unmatched_entity_no_change(self):
        """无匹配关键词的实体不产生新条目"""
        prefs = {"favorite_genres": [{"genre": "科幻", "count": 1}]}
        entities = [{"name": "进击的巨人"}]  # 不在关键词表中
        result = MemoryManager._update_genres(prefs, entities)
        assert result["favorite_genres"] == [{"genre": "科幻", "count": 1}]

    def test_top10_truncation(self):
        """超过 10 个类型时截断最低 count。

        已有 10 个类型（count 10..1），新增 "机战" count=1。
        排序稳定性保证"机战"排在已有 type1 之后，被 [:10] 截出。"""
        prefs = {
            "favorite_genres": [
                {"genre": f"type{i}", "count": i} for i in range(10, 0, -1)
            ]
        }
        entities = [{"name": "高达SEED"}]  # → "机战" count=1
        result = MemoryManager._update_genres(prefs, entities)
        assert len(result["favorite_genres"]) == 10
        # 机战 count=1 与 type1 count=1 同分，排序稳定性保持 type1 在前
        # → 机战排在 type1 之后，被 [:10] 截出
        genres_in_result = {g["genre"] for g in result["favorite_genres"]}
        assert "机战" not in genres_in_result  # 被截出
        assert "type1" in genres_in_result

    def test_no_favorite_genres_key(self):
        """prefs 无 favorite_genres key → 正常处理"""
        prefs = {}
        entities = [{"name": "高达SEED"}]
        result = MemoryManager._update_genres(prefs, entities)
        assert "favorite_genres" in result
        assert result["favorite_genres"][0] == {"genre": "机战", "count": 1}

    def test_skips_malformed_genre_entry(self):
        """已有 genre dict 无 'genre' key → 过滤不掉崩溃"""
        prefs = {"favorite_genres": [
            {"count": 5},  # 畸形：缺 genre key
            {"genre": "机战", "count": 3},
        ]}
        entities = [{"name": "高达SEED"}]
        result = MemoryManager._update_genres(prefs, entities)
        # 畸形条目被丢弃，机战 count 正确累加
        genres = {g["genre"]: g["count"] for g in result["favorite_genres"] if "genre" in g}
        assert genres["机战"] == 4

    def test_case_insensitive_keyword_match(self):
        """关键词匹配不区分大小写"""
        prefs = {}
        entities = [{"name": "高达seed"}]
        result = MemoryManager._update_genres(prefs, entities)
        assert result["favorite_genres"][0] == {"genre": "机战", "count": 1}


class TestUpdateAffinities:
    """_update_affinities() — 实体亲和度 EMA 更新"""

    def test_empty_entities_preserves_prefs(self):
        """空 entities 时 prefs 不变"""
        prefs = {"entity_affinities": {"高达SEED": {"name": "高达SEED", "type": "subject", "interest_score": 0.5}}}
        entities: list[dict] = []
        result = MemoryManager._update_affinities(prefs, entities)
        assert result == prefs

    def test_new_entity_initial_score(self):
        """新实体 → interest_score=0.5, type 默认为 subject"""
        prefs = {}
        entities = [{"name": "高达SEED"}]
        result = MemoryManager._update_affinities(prefs, entities)
        assert result["entity_affinities"]["高达SEED"] == {
            "name": "高达SEED",
            "type": "subject",
            "interest_score": 0.5,
        }

    def test_existing_entity_ema_update(self):
        """已有实体 → EMA: 0.9 × old + 0.1"""
        prefs = {"entity_affinities": {"高达SEED": {"name": "高达SEED", "type": "subject", "interest_score": 0.5}}}
        entities = [{"name": "高达SEED"}]
        result = MemoryManager._update_affinities(prefs, entities)
        # 0.9 × 0.5 + 0.1 = 0.55
        assert result["entity_affinities"]["高达SEED"]["interest_score"] == 0.55

    def test_ema_converges_toward_one(self):
        """多次 EMA 更新 → 分数渐近逼近 1.0"""
        prefs = {}
        entity = [{"name": "高达SEED"}]
        expected = 0.5
        for _ in range(5):
            prefs = MemoryManager._update_affinities(prefs, entity)
            assert prefs["entity_affinities"]["高达SEED"]["interest_score"] == expected
            expected = 0.9 * expected + 0.1
        # 5 次更新后: 0.5 → 0.55 → 0.595 → 0.6355 → 0.67195
        assert 0.67 < prefs["entity_affinities"]["高达SEED"]["interest_score"] < 0.68

    def test_score_capped_at_one(self):
        """interest_score 已为 1.0 时，EMA 不使其超出（min 封顶）"""
        prefs = {"entity_affinities": {"高达SEED": {"name": "高达SEED", "type": "subject", "interest_score": 1.0}}}
        entities = [{"name": "高达SEED"}]
        result = MemoryManager._update_affinities(prefs, entities)
        # 0.9*1.0 + 0.1 = 1.0, min(1.0, 1.0) = 1.0
        assert result["entity_affinities"]["高达SEED"]["interest_score"] == 1.0

    def test_empty_name_skipped(self):
        """空 entity name → 跳过，不产生条目"""
        prefs = {}
        entities = [{"name": "", "type": "subject"}]
        result = MemoryManager._update_affinities(prefs, entities)
        assert result == {"entity_affinities": {}}

    def test_missing_type_defaults_to_subject(self):
        """entity 无 type 字段 → 默认 'subject'"""
        prefs = {}
        entities = [{"name": "高达SEED"}]  # 无 type
        result = MemoryManager._update_affinities(prefs, entities)
        assert result["entity_affinities"]["高达SEED"]["type"] == "subject"

    def test_top20_truncation(self):
        """超过 20 个实体时截断最低 score"""
        prefs = {
            "entity_affinities": {
                f"entity{i}": {"name": f"entity{i}", "type": "subject", "interest_score": float(i) / 100}
                for i in range(20)
            }
        }
        # 新实体 score=0.5，应进入 top-20
        entities = [{"name": "高达SEED"}]
        result = MemoryManager._update_affinities(prefs, entities)
        assert len(result["entity_affinities"]) == 20
        assert "高达SEED" in result["entity_affinities"]
        # score=0.01 的 entity1 被截出
        assert "entity0" not in result["entity_affinities"]

    def test_unaffected_entities_unchanged(self):
        """未被本轮 entities 命中的旧实体 → score 不变（无全局衰减）"""
        prefs = {"entity_affinities": {
            "高达SEED": {"name": "高达SEED", "type": "subject", "interest_score": 0.8},
            "星际牛仔": {"name": "星际牛仔", "type": "subject", "interest_score": 0.6},
        }}
        entities = [{"name": "高达SEED"}]  # 只命中高达SEED
        result = MemoryManager._update_affinities(prefs, entities)
        # 高达SEED 被更新
        assert result["entity_affinities"]["高达SEED"]["interest_score"] > 0.8
        # 星际牛仔 保持不变（未被命中）
        assert result["entity_affinities"]["星际牛仔"]["interest_score"] == 0.6

    def test_no_entity_affinities_key(self):
        """prefs 无 entity_affinities key → 正常处理"""
        prefs = {}
        entities = [{"name": "高达SEED"}]
        result = MemoryManager._update_affinities(prefs, entities)
        assert "entity_affinities" in result
        assert result["entity_affinities"]["高达SEED"]["interest_score"] == 0.5
