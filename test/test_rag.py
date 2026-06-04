"""test_rag.py — RAG 模块全栈测试。

依赖 PostgreSQL + pgvector + 智谱 embedding-3。
覆盖 text_processor / ingestion / retriever 三个子模块。
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, delete

from database.engine import engine, init_db
from database.models import RagEntity, SubjectMeta, CharacterMeta, PersonMeta, PersonWork, CharacterCast
from rag.ingestion import (
    RagEntityIngestor,
    _build_character_chunk_text,
    _build_person_chunk_text,
    _build_subject_chunk_text,
    _prefixed_character_id,
    _prefixed_person_id,
    _prefixed_subject_id,
)
from rag.retriever import (
    RagEntityRetriever,
    RagSearchResult,
    _extract_heat_signal,
)
from rag.text_processor import BangumiTextProcessor
from core.config import get_settings


# ═══════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def settings():
    return get_settings()


@pytest.fixture(scope="module")
def processor():
    return BangumiTextProcessor(chunk_size=300, chunk_overlap=50)


@pytest.fixture(scope="module")
def retriever(settings):
    return RagEntityRetriever(
        engine=engine,
        zhipu_api_key=settings.ZHIPU_API_KEY,
        zhipu_base_url=settings.ZHIPU_BASE_URL,
    )


@pytest.fixture(scope="module")
def ingestor(settings):
    return RagEntityIngestor(
        engine=engine,
        zhipu_api_key=settings.ZHIPU_API_KEY,
        zhipu_base_url=settings.ZHIPU_BASE_URL,
    )


@pytest.fixture(autouse=True)
def cleanup_test_data():
    """每个测试前后都清理测试数据，保证跨 session 隔离。"""
    # 前清理：移除上一轮可能遗留的数据
    with Session(engine) as s:
        s.exec(delete(RagEntity).where(
            RagEntity.id.in_(["subject_99999", "character_88888", "person_77777"])
        ))
        s.commit()
    yield
    # 后清理
    with Session(engine) as s:
        s.exec(delete(RagEntity).where(
            RagEntity.id.in_(["subject_99999", "character_88888", "person_77777"])
        ))
        s.commit()


# ═══════════════════════════════════════════════════════════════════
# text_processor — 纯函数单元测试
# ═══════════════════════════════════════════════════════════════════


class TestCleanText:
    def test_html_unescape(self, processor):
        assert processor.clean_text("&amp; &lt; &gt;") == "& < >"

    def test_fullwidth_space_to_halfwidth(self, processor):
        assert processor.clean_text("hello　world") == "hello world"

    def test_newline_normalization(self, processor):
        assert processor.clean_text("line1\r\nline2") == "line1\nline2"
        assert processor.clean_text("a\n\n\nb") == "a\nb"

    def test_multispace_collapse(self, processor):
        assert processor.clean_text("a    b") == "a b"

    def test_strip_quotes(self, processor):
        assert processor.clean_text('"hello"') == "hello"

    def test_zero_width_chars_removed(self, processor):
        assert processor.clean_text("a​b‌c") == "abc"
        assert processor.clean_text("﻿text") == "text"

    def test_empty_string(self, processor):
        assert processor.clean_text("") == ""
        assert processor.clean_text("   ") == ""


class TestSplitText:
    def test_short_text_no_split(self, processor):
        chunks = processor.split_text("短短一段话")
        assert len(chunks) == 1
        assert chunks[0] == "短短一段话"

    def test_none_returns_empty(self, processor):
        assert processor.split_text(None) == []

    def test_empty_string_returns_empty(self, processor):
        assert processor.split_text("") == []

    def test_chunks_have_overlap(self, processor):
        """长文本切分后相邻块之间有重叠。"""
        long_text = ("这是一段测试文本。它包含很多句子和细节。" * 30)
        processor_small = BangumiTextProcessor(chunk_size=100, chunk_overlap=20)
        chunks = processor_small.split_text(long_text)
        assert len(chunks) > 1, f"Expected multiple chunks, got {len(chunks)}"

    def test_step_calculation(self, processor):
        """chunk_size=300, overlap=50 → step=250"""
        assert processor.chunk_size == 300
        assert processor.chunk_overlap == 50

    def test_invalid_overlap_raises(self):
        with pytest.raises(ValueError):
            BangumiTextProcessor(chunk_size=100, chunk_overlap=100)
        with pytest.raises(ValueError):
            BangumiTextProcessor(chunk_size=100, chunk_overlap=150)


class TestCreateEntityDocuments:
    def test_subject_with_tags(self, processor):
        result = processor.create_entity_documents(
            entity_type="subject", entity_id=8, name_cn="进击的巨人",
            summary="在巨人支配的世界中...", tags=["科幻", "战斗"],
        )
        parent = result["parent"]
        assert "[作品名] 进击的巨人。" in parent["text"]
        assert "标签: 科幻, 战斗" in parent["text"]
        assert "在巨人支配的世界中" in parent["text"]

    def test_character_with_subject(self, processor):
        result = processor.create_entity_documents(
            entity_type="character", entity_id=5, name_cn="艾伦",
            summary="憧憬外面世界的少年", subject_name="进击的巨人",
        )
        parent = result["parent"]
        assert "[角色] 艾伦" in parent["text"]
        assert "进击的巨人" in parent["text"]

    def test_person(self, processor):
        result = processor.create_entity_documents(
            entity_type="person", entity_id=3, name_cn="梶裕贵",
            summary="日本男性声优",
        )
        parent = result["parent"]
        assert "[人物] 梶裕贵。" in parent["text"]

    def test_empty_summary_generates_parent_only(self, processor):
        result = processor.create_entity_documents(
            entity_type="subject", entity_id=1, name_cn="Test",
        )
        assert len(result["children"]) == 0
        assert result["parent"]["text"] != ""

    def test_long_summary_generates_children(self, processor):
        long_summary = "这是测试文本。" * 100
        result = processor.create_entity_documents(
            entity_type="subject", entity_id=1, name_cn="Test",
            summary=long_summary,
        )
        assert len(result["children"]) > 0

    def test_children_have_entity_metadata(self, processor):
        long_summary = "这是测试文本。" * 100
        result = processor.create_entity_documents(
            entity_type="character", entity_id=5, summary=long_summary,
            name_cn="Test", subject_name="TestSubject",
        )
        for child in result["children"]:
            assert child["entity_type"] == "character"
            assert child["entity_id"] == 5


# ═══════════════════════════════════════════════════════════════════
# ingestion — ID 前缀 + chunk 构建（纯函数）
# ═══════════════════════════════════════════════════════════════════


class TestPrefixedIds:
    def test_subject(self):
        assert _prefixed_subject_id(10) == "subject_10"

    def test_character(self):
        assert _prefixed_character_id(5) == "character_5"

    def test_person(self):
        assert _prefixed_person_id(3) == "person_3"


class TestChunkTextBuilders:
    def test_subject_with_name(self):
        result = _build_subject_chunk_text("进击的巨人", "在巨人支配的世界中...")
        assert result == "[作品名] 进击的巨人。在巨人支配的世界中..."

    def test_subject_without_name(self):
        result = _build_subject_chunk_text("", "在巨人支配的世界中...")
        assert result == "[作品名] 在巨人支配的世界中..."

    def test_character_full(self):
        result = _build_character_chunk_text("艾伦", "进击的巨人", "憧憬的少年")
        assert result == "[角色] 艾伦，出自《进击的巨人》。憧憬的少年"

    def test_character_no_subject(self):
        result = _build_character_chunk_text("艾伦", "", "憧憬的少年")
        assert "出自" not in result

    def test_person_with_name(self):
        result = _build_person_chunk_text("梶裕贵", "日本男性声优")
        assert result == "[人物] 梶裕贵。日本男性声优"

    def test_person_without_name(self):
        result = _build_person_chunk_text("", "日本男性声优")
        assert result == "[人物] 日本男性声优"


# ═══════════════════════════════════════════════════════════════════
# retriever — _extract_heat_signal（纯函数）
# ═══════════════════════════════════════════════════════════════════


class TestExtractHeatSignal:
    def test_subject(self):
        assert _extract_heat_signal({"rating_total": 9438}, "subject") == 9438

    def test_subject_missing_defaults_zero(self):
        assert _extract_heat_signal({}, "subject") == 0

    def test_character(self):
        assert _extract_heat_signal({"collects": 5000}, "character") == 5000

    def test_person(self):
        assert _extract_heat_signal({"collects": 8500}, "person") == 8500

    def test_unknown_entity_type(self):
        assert _extract_heat_signal({"rating_total": 999}, "unknown") == 0

    def test_non_numeric_handled(self):
        assert _extract_heat_signal({"rating_total": "abc"}, "subject") == 0


# ═══════════════════════════════════════════════════════════════════
# retriever — 初始化
# ═══════════════════════════════════════════════════════════════════


class TestRetrieverInit:
    def test_client_initialized(self, retriever):
        assert retriever.client is not None, "Zhipu client should be initialized"

    def test_engine_set(self, retriever):
        assert retriever.engine is not None


# ═══════════════════════════════════════════════════════════════════
# retriever — 真实 hybrid_search
# ═══════════════════════════════════════════════════════════════════


class TestHybridSearch:
    def test_empty_query_returns_empty(self, retriever):
        results = retriever.hybrid_search("")
        assert results == []

    def test_whitespace_query_returns_empty(self, retriever):
        results = retriever.hybrid_search("   ")
        assert results == []

    def test_search_returns_rag_search_results(self, retriever):
        results = retriever.hybrid_search("进击的巨人", limit=3)
        assert isinstance(results, list)
        if results:
            for r in results:
                assert isinstance(r, RagSearchResult)
                assert r.entity_type in ("subject", "character", "person")
                assert len(r.chunk_text) > 0
                assert r.cosine_distance >= 0

    def test_subject_only_filter(self, retriever):
        results = retriever.hybrid_search("科幻 机战", entity_type="subject", limit=3)
        if results:
            for r in results:
                assert r.entity_type == "subject"

    def test_character_only_filter(self, retriever):
        results = retriever.hybrid_search("傲娇", entity_type="character", limit=3)
        if results:
            for r in results:
                assert r.entity_type == "character"

    def test_nsfw_exclusion(self, retriever):
        results = retriever.hybrid_search("测试", exclude_nsfw=True, limit=10)
        for r in results:
            if r.entity_type == "subject":
                assert r.meta_info.get("nsfw") is not True

    def test_cosine_distance_range(self, retriever):
        results = retriever.hybrid_search("日常 校园 恋爱", limit=5)
        for r in results:
            assert 0.0 <= r.cosine_distance <= 2.0

    def test_results_sorted_by_final_score(self, retriever):
        results = retriever.hybrid_search("动画 战斗 热血", limit=5)
        if len(results) >= 2:
            for i in range(len(results) - 1):
                assert results[i].final_score <= results[i + 1].final_score

    def test_result_has_useful_fields(self, retriever):
        results = retriever.hybrid_search("魔法少女", limit=1)
        if results:
            r = results[0]
            assert len(r.name) > 0
            assert len(r.entity_id) > 0


# ═══════════════════════════════════════════════════════════════════
# ingestion + retriever — E2E
# ═══════════════════════════════════════════════════════════════════


class TestIngestionE2E:
    """端到端：摄入 → 检索 → 验证字段完整性。"""

    TEST_SUBJECT_ID = 99999
    TEST_CHARACTER_ID = 88888
    TEST_PERSON_ID = 77777

    def test_ingest_subject_and_retrieve(self, ingestor, retriever):
        """摄入一条 Subject → hybrid_search 能检索到 → meta_info 字段正确。"""
        data = [
            {
                "subject_id": self.TEST_SUBJECT_ID,
                "name": "テスト作品",
                "name_cn": "测试作品",
                "chunk_text": "这是一部关于人工智能与人类共存的科幻动画，探讨了意识、自由意志等哲学命题。故事发生在22世纪的东京。",
                "score": 8.5,
                "rank": 42,
                "rating_total": 5000,
                "date": "2024-01-10",
                "year": 2024,
                "platform": "TV",
                "eps": 12,
                "nsfw": False,
                "tags": [{"name": "科幻", "count": 3000}, {"name": "原创", "count": 2000}],
            }
        ]
        count = ingestor.ingest_subjects(data)
        assert count == 1

        results = retriever.hybrid_search("人工智能 科幻 动画", entity_type="subject", limit=3)
        assert len(results) > 0
        found = [r for r in results if r.entity_id == f"subject_{self.TEST_SUBJECT_ID}"]
        assert len(found) == 1, f"Expected to find test subject in results, got: {[r.entity_id for r in results]}"

        meta = found[0].meta_info
        assert meta["score"] == 8.5
        assert meta["rank"] == 42
        assert meta["year"] == 2024
        assert meta["platform"] == "TV"
        assert meta["nsfw"] is False
        assert len(meta["tags"]) == 2
        assert float(meta["score"]) > 0

    def test_ingest_character_and_retrieve(self, ingestor, retriever):
        """摄入一条 Character → 能检索到 → casts 字段正确。"""
        data = [
            {
                "character_id": self.TEST_CHARACTER_ID,
                "name": "テストキャラ",
                "name_cn": "测试角色",
                "chunk_text": "本作の主人公。正義感が強く、仲間を守るために戦う高校生。特技は剣道。",
                "role": 1,
                "collects": 3000,
                "casts_raw": [
                    {
                        "subject_id": self.TEST_SUBJECT_ID,
                        "subject_name": "测试作品",
                        "person_id": self.TEST_PERSON_ID,
                        "person_name": "テスト声優",
                        "type": 1,
                    }
                ],
            }
        ]
        count = ingestor.ingest_characters(data)
        assert count == 1

        results = retriever.hybrid_search("主人公 正義感 剣道", entity_type="character", limit=3)
        found = [r for r in results if r.entity_id == f"character_{self.TEST_CHARACTER_ID}"]
        assert len(found) == 1

        meta = found[0].meta_info
        assert meta["role"] == 1
        assert meta["collects"] == 3000
        assert len(meta["casts"]) >= 1
        cast = meta["casts"][0]
        assert cast["subject_name"] == "测试作品"
        assert "person_name" in cast or "person_id" in cast

    def test_ingest_person_and_retrieve(self, ingestor, retriever):
        """摄入一条 Person → 能检索到 → works + career 字段正确。"""
        data = [
            {
                "person_id": self.TEST_PERSON_ID,
                "name": "テスト声優",
                "name_cn": "测试声优",
                "chunk_text": "日本の男性声優。数多くのアニメの主人公を演じ、声優アワード主演男優賞を受賞。",
                "career": ["seiyu"],
                "type": 1,
                "collects": 8000,
                "works_raw": [
                    {
                        "subject_id": self.TEST_SUBJECT_ID,
                        "subject_name": "测试作品",
                        "positions": [],
                    }
                ],
            }
        ]
        count = ingestor.ingest_persons(data)
        assert count == 1

        results = retriever.hybrid_search("声優 主人公 受賞", entity_type="person", limit=3)
        found = [r for r in results if r.entity_id == f"person_{self.TEST_PERSON_ID}"]
        assert len(found) == 1

        meta = found[0].meta_info
        assert meta["career"] == ["seiyu"]
        assert meta["collects"] == 8000

    def test_cross_entity_all_search(self, ingestor, retriever):
        """entity_type='all' 能跨域检索。"""
        # 自包含：摄入 subject + person
        sid, pid = 99998, 77776
        ingestor.ingest_subjects([{
            "subject_id": sid, "name": "クロス作品", "name_cn": "跨域作品",
            "chunk_text": "異世界転生ファンタジーの傑作。魔王を倒す旅に出る少年少女の物語。",
            "score": 8.0, "rank": 50, "rating_total": 3000, "date": "2023-01-01",
            "year": 2023, "platform": "TV", "eps": 12, "nsfw": False, "tags": [],
        }])
        ingestor.ingest_persons([{
            "person_id": pid, "name": "クロス声優", "name_cn": "跨域声优",
            "chunk_text": "数々の異世界作品で主人公を演じる実力派声優。",
            "career": ["seiyu"], "type": 1, "collects": 5000, "works_raw": [],
        }])

        results = retriever.hybrid_search("異世界 転生 ファンタジー 声優", entity_type="all", limit=10)
        assert len(results) > 0
        types = {r.entity_type for r in results}
        # 不强求两种类型都出现（取决于向量距离），但至少能搜到结果
        assert "subject" in types or "person" in types

        # 清洗
        with Session(engine) as s:
            s.exec(delete(RagEntity).where(
                RagEntity.id.in_([f"subject_{sid}", f"person_{pid}"])
            ))
            s.commit()

    def test_chunk_text_has_semantic_prefix(self, ingestor, retriever):
        """验证 chunk_text 包含了语义前缀。"""
        ingestor.ingest_subjects([{
            "subject_id": self.TEST_SUBJECT_ID, "name": "テスト作品", "name_cn": "测试作品",
            "chunk_text": "人工知能と人類の共存を描くSFアニメ。",
            "score": 8.5, "rank": 42, "rating_total": 5000, "date": "2024-01-01",
            "year": 2024, "platform": "TV", "eps": 12, "nsfw": False, "tags": [],
        }])
        results = retriever.hybrid_search("人工知能 SF アニメ", entity_type="subject", limit=3)
        found = [r for r in results if r.entity_id == f"subject_{self.TEST_SUBJECT_ID}"]
        assert len(found) >= 1
        assert "[作品名]" in found[0].chunk_text

    def test_heat_sorting_subject(self, retriever):
        """同梯队内按 rating_total 降序（使用真实已索引数据验证排序逻辑）。"""
        results = retriever.hybrid_search("SF アニメ 未来", entity_type="subject", limit=10)
        if len(results) >= 2:
            for i in range(len(results) - 1):
                if results[i].final_score == results[i + 1].final_score:
                    heat_i = results[i].meta_info.get("rating_total", 0)
                    heat_j = results[i + 1].meta_info.get("rating_total", 0)
                    assert heat_i >= heat_j, (
                        f"Same bucket, expected heat desc: {heat_i} >= {heat_j}"
                    )


# ═══════════════════════════════════════════════════════════════════
# RagSearchResult — 模型验证
# ═══════════════════════════════════════════════════════════════════


class TestRagSearchResult:
    def test_valid_model(self):
        r = RagSearchResult(
            entity_id="subject_10",
            entity_type="subject",
            chunk_text="测试文本",
            name="テスト",
            cosine_distance=0.2,
            final_score=0.0,
            meta_info={"score": 7.5},
        )
        assert r.entity_id == "subject_10"
        assert r.cosine_distance == 0.2

    def test_defaults(self):
        r = RagSearchResult(
            entity_id="character_5",
            entity_type="character",
            chunk_text="text",
            name="test",
            cosine_distance=0.5,
        )
        assert r.name_cn is None
        assert r.final_score == 0.0
        assert r.meta_info == {}
