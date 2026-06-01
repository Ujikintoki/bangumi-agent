"""
RAG 核心逻辑测试：精准过滤、防爆拦截、语义阶梯分桶排序

严格隔离 —— 绝不发起真实网络请求，所有 Embedding 均通过 Mock 注入。
数据库操作使用 transaction rollback，测试结束后自动回滚。
"""

from __future__ import annotations

from unittest.mock import MagicMock

from sqlmodel import Session

from database.models import BangumiChunk
from rag.retriever import BangumiRetriever
from test.conftest import QUERY_VEC, make_doc_vector

# ═══════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════


def _insert_chunk(
    session: Session,
    entity_id: int,
    name: str,
    text: str,
    embedding: list[float],
    tags: list[str] | None = None,
    score: float = 7.0,
    rating_total: int = 0,
    nsfw: bool = False,
    subject_type: int = 2,
) -> BangumiChunk:
    """插入一条测试用 BangumiChunk。"""
    chunk = BangumiChunk(
        entity_type="subject",
        entity_id=entity_id,
        chunk_text=text,
        embedding=embedding,
        meta_info={
            "name": name,
            "tags": tags or [],
            "score": score,
            "rating_total": rating_total,
            "nsfw": nsfw,
            "subject_type": subject_type,
        },
    )
    session.add(chunk)
    session.commit()
    session.refresh(chunk)
    return chunk


def _make_retriever(engine, zhipu_api_key: str = "mock-key") -> BangumiRetriever:
    """创建检索器实例。"""
    return BangumiRetriever(engine=engine, zhipu_api_key=zhipu_api_key)


# ═══════════════════════════════════════════════════════════════
# 精准过滤测试：NSFW + Tags 硬过滤
# ═══════════════════════════════════════════════════════════════


class TestPreciseFiltering:
    """验证 SQL 层 JSONB 硬过滤（tags, nsfw）是否生效。"""

    def test_exclude_nsfw_blocks_adult_content(self, db_session: Session, test_engine):
        """插入两条数据（一条 nsfw=True，一条 nsfw=False），断言 nsfw 被排除。"""
        safe_vec = make_doc_vector(0.3)
        nsfw_vec = make_doc_vector(0.25)

        _insert_chunk(
            db_session,
            1001,
            "Safe Anime",
            "safe content",
            safe_vec,
            tags=["日常"],
            nsfw=False,
        )
        _insert_chunk(
            db_session,
            1002,
            "NSFW Anime",
            "adult content",
            nsfw_vec,
            tags=["日常"],
            nsfw=True,
        )

        # 替换 retriever.client 为 MagicMock，拦截 embedding API 调用
        mock_client = MagicMock()
        mock_embed_response = MagicMock()
        mock_embed_response.data = [MagicMock(embedding=QUERY_VEC)]
        mock_client.embeddings.create.return_value = mock_embed_response

        retriever = _make_retriever(test_engine)
        retriever.client = mock_client

        results = retriever.hybrid_search(
            query="日常动画",
            exclude_nsfw=True,
            top_k=5,
        )

        # 应仅返回安全条目
        entity_ids = {r.entity_id for r in results}
        assert 1001 in entity_ids
        assert 1002 not in entity_ids, "NSFW 内容应被 exclude_nsfw=True 拦截"

    def test_tags_intersection_filter_works(self, db_session: Session, test_engine):
        """插入三条：['百合','校园']、['机战','原创']、['日常']，过滤 ['百合']。"""
        vec = make_doc_vector(0.3)

        _insert_chunk(
            db_session, 2001, "Yuri A", "yuri text", vec, tags=["百合", "校园"]
        )
        _insert_chunk(
            db_session, 2002, "Mecha B", "mecha text", vec, tags=["机战", "原创"]
        )
        _insert_chunk(db_session, 2003, "SoL C", "sol text", vec, tags=["日常"])

        mock_client = MagicMock()
        mock_embed_response = MagicMock()
        mock_embed_response.data = [MagicMock(embedding=QUERY_VEC)]
        mock_client.embeddings.create.return_value = mock_embed_response

        retriever = _make_retriever(test_engine)
        retriever.client = mock_client

        results = retriever.hybrid_search(
            query="百合动画",
            required_tags=["百合"],
            top_k=5,
        )

        entity_ids = {r.entity_id for r in results}
        assert 2001 in entity_ids
        assert 2002 not in entity_ids
        assert 2003 not in entity_ids

    def test_multi_tags_all_required(self, db_session: Session, test_engine):
        """required_tags=["百合", "校园"] — 必须同时具备两个标签。"""
        vec = make_doc_vector(0.3)

        _insert_chunk(
            db_session, 3001, "Both Tags", "text", vec, tags=["百合", "校园", "恋爱"]
        )
        _insert_chunk(db_session, 3002, "One Tag", "text", vec, tags=["百合"])

        mock_client = MagicMock()
        mock_embed_response = MagicMock()
        mock_embed_response.data = [MagicMock(embedding=QUERY_VEC)]
        mock_client.embeddings.create.return_value = mock_embed_response

        retriever = _make_retriever(test_engine)
        retriever.client = mock_client

        results = retriever.hybrid_search(
            query="百合校园",
            required_tags=["百合", "校园"],
            top_k=5,
        )

        entity_ids = {r.entity_id for r in results}
        assert 3001 in entity_ids
        assert 3002 not in entity_ids, "@> 语义要求同时具备所有标签"


# ═══════════════════════════════════════════════════════════════
# 防爆拦截测试：距离阈值过滤
# ═══════════════════════════════════════════════════════════════


class TestDistanceThreshold:
    """验证 distance_threshold 参数正确拦截语义不相关的候选。"""

    def test_high_distance_docs_are_discarded(self, db_session: Session, test_engine):
        """距离 0.70 > 阈值 0.65，应被丢弃。"""
        far_vec = make_doc_vector(0.70)
        near_vec = make_doc_vector(0.30)

        _insert_chunk(db_session, 4001, "Far Doc", "irrelevant", far_vec)
        _insert_chunk(db_session, 4002, "Near Doc", "relevant", near_vec)

        mock_client = MagicMock()
        mock_embed_response = MagicMock()
        mock_embed_response.data = [MagicMock(embedding=QUERY_VEC)]
        mock_client.embeddings.create.return_value = mock_embed_response

        retriever = _make_retriever(test_engine)
        retriever.client = mock_client

        results = retriever.hybrid_search(
            query="相关查询",
            distance_threshold=0.65,
            top_k=5,
        )

        entity_ids = {r.entity_id for r in results}
        assert 4001 not in entity_ids, "距离 0.70 > threshold 0.65 应被丢弃"
        assert 4002 in entity_ids

    def test_all_docs_above_threshold_returns_empty(
        self, db_session: Session, test_engine
    ):
        """所有文档距离均 > 阈值 → 返回空列表。"""
        far_vec = make_doc_vector(0.80)

        _insert_chunk(db_session, 5001, "Very Far", "text", far_vec)

        mock_client = MagicMock()
        mock_embed_response = MagicMock()
        mock_embed_response.data = [MagicMock(embedding=QUERY_VEC)]
        mock_client.embeddings.create.return_value = mock_embed_response

        retriever = _make_retriever(test_engine)
        retriever.client = mock_client

        results = retriever.hybrid_search(
            query="anything",
            distance_threshold=0.65,
            top_k=5,
        )

        assert results == [], "所有文档超阈值时应返回空列表"


# ═══════════════════════════════════════════════════════════════
# 语义阶梯分桶排序测试
# ═══════════════════════════════════════════════════════════════


class TestSemanticBucketSort:
    """验证语义阶梯分桶 + rating_total 降级排序逻辑。

    三个文档落在同一语义梯队内（distances 差 < bucket_size），
    排序应由 rating_total 降序主导。
    """

    def test_same_bucket_sorted_by_rating_total(self, db_session: Session, test_engine):
        """三文档距离 0.10/0.11/0.12，bucket_size=0.05 → 同在 bucket 2，
        排序应为 rating_total 降序：45000 → 8500 → 1200。"""
        vec_low = make_doc_vector(0.10)  # distance 0.10, rating_total 8500
        vec_mid = make_doc_vector(0.11)  # distance 0.11, rating_total 1200
        vec_high = make_doc_vector(0.12)  # distance 0.12, rating_total 45000

        _insert_chunk(
            db_session,
            6001,
            "Mid Pop",
            "text",
            vec_low,
            rating_total=8500,
            tags=["科幻"],
        )
        _insert_chunk(
            db_session,
            6002,
            "Low Pop",
            "text",
            vec_mid,
            rating_total=1200,
            tags=["科幻"],
        )
        _insert_chunk(
            db_session,
            6003,
            "High Pop",
            "text",
            vec_high,
            rating_total=45000,
            tags=["科幻"],
        )

        mock_client = MagicMock()
        mock_embed_response = MagicMock()
        mock_embed_response.data = [MagicMock(embedding=QUERY_VEC)]
        mock_client.embeddings.create.return_value = mock_embed_response

        retriever = _make_retriever(test_engine)
        retriever.client = mock_client

        results = retriever.hybrid_search(
            query="科幻动画",
            semantic_bucket_size=0.05,
            distance_threshold=0.65,
            top_k=5,
        )

        assert len(results) == 3
        # 同梯队内 rating_total 降序
        assert results[0].entity_id == 6003, (
            f"rating_total=45000 应在第一位，实际 entity_id={results[0].entity_id}"
        )
        assert results[1].entity_id == 6001, (
            f"rating_total=8500 应在第二位，实际 entity_id={results[1].entity_id}"
        )
        assert results[2].entity_id == 6002, (
            f"rating_total=1200 应在第三位，实际 entity_id={results[2].entity_id}"
        )

    def test_different_buckets_primary_sort_by_distance(
        self, db_session: Session, test_engine
    ):
        """不同梯队间，距离优先于热度。"""
        # bucket_size=0.03: dist 0.05→bucket 1, dist 0.10→bucket 3
        vec_near = make_doc_vector(0.05)  # bucket 1, low heat
        vec_far = make_doc_vector(0.10)  # bucket 3, high heat

        _insert_chunk(
            db_session,
            7001,
            "Near Low Heat",
            "text",
            vec_near,
            rating_total=100,
            tags=["动画"],
        )
        _insert_chunk(
            db_session,
            7002,
            "Far High Heat",
            "text",
            vec_far,
            rating_total=99999,
            tags=["动画"],
        )

        mock_client = MagicMock()
        mock_embed_response = MagicMock()
        mock_embed_response.data = [MagicMock(embedding=QUERY_VEC)]
        mock_client.embeddings.create.return_value = mock_embed_response

        retriever = _make_retriever(test_engine)
        retriever.client = mock_client

        results = retriever.hybrid_search(
            query="动画",
            semantic_bucket_size=0.03,
            top_k=5,
        )

        # 距离近的（bucket 1）应排在距离远的（bucket 3）前面，
        # 即使后者热度更高
        assert results[0].entity_id == 7001, (
            f"更近的文档应排第一，实际 entity_id={results[0].entity_id}"
        )
        assert results[1].entity_id == 7002

    def test_heat_only_breaks_ties_within_same_bucket(
        self, db_session: Session, test_engine
    ):
        """验证「热度不破坏全局语义匹配度」原则。"""
        vec_a = make_doc_vector(0.20)  # bucket 6 (with size=0.03)
        vec_b = make_doc_vector(0.22)  # bucket 7

        _insert_chunk(
            db_session,
            8001,
            "Bucket6 LowHeat",
            "text",
            vec_a,
            rating_total=10,
            tags=["动画"],
        )
        _insert_chunk(
            db_session,
            8002,
            "Bucket7 HighHeat",
            "text",
            vec_b,
            rating_total=100000,
            tags=["动画"],
        )

        mock_client = MagicMock()
        mock_embed_response = MagicMock()
        mock_embed_response.data = [MagicMock(embedding=QUERY_VEC)]
        mock_client.embeddings.create.return_value = mock_embed_response

        retriever = _make_retriever(test_engine)
        retriever.client = mock_client

        results = retriever.hybrid_search(
            query="动画",
            semantic_bucket_size=0.03,
            top_k=5,
        )

        # bucket 6 的文档应排在 bucket 7 之前（距离优先）
        assert results[0].entity_id == 8001, (
            "语义更近的梯队应排在前，热度不能跨梯队超越"
        )
