"""
pytest 全局夹具 (Fixtures)

提供数据库事务隔离、Zhipu Embedding Mock、Bangumi API Mock、
以及 FastAPI TestClient 等测试基础设施。
"""

from __future__ import annotations

import math
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

from core.config import get_settings
from database.engine import get_session

# ═══════════════════════════════════════════════════════════════
# 向量工具（用于构造可控的 Mock Embedding）
# ═══════════════════════════════════════════════════════════════

EMBEDDING_DIM = 2048


def _make_unit_vector(dim: int = EMBEDDING_DIM, first: float = 1.0) -> list[float]:
    """构造 dim 维单位向量，first 为第一分量，其余分量补零或 sqrt(1-first²)。

    若 |first| ≤ 1，第二分量为 sqrt(1-first²) 以保证单位长度。
    """
    v = [0.0] * dim
    v[0] = first
    if abs(first) < 1.0:
        v[1] = math.sqrt(max(0.0, 1.0 - first * first))
    return v


# 标准 Query 向量：[1, 0, 0, ...]
QUERY_VEC = _make_unit_vector(dim=EMBEDDING_DIM, first=1.0)


def make_doc_vector(distance: float, dim: int = EMBEDDING_DIM) -> list[float]:
    """构造与 QUERY_VEC 余弦距离为 distance 的文档向量。

    cosine_similarity = 1 - distance
    doc[0] = cosine_similarity
    """
    cos_sim = 1.0 - distance
    return _make_unit_vector(dim=dim, first=cos_sim)


# ═══════════════════════════════════════════════════════════════
# 数据库 Fixtures（Transaction Rollback 隔离）
# ═══════════════════════════════════════════════════════════════


@pytest.fixture(scope="session")
def test_engine():
    """会话级 Engine，连接真实 PostgreSQL 但由事务回滚保证隔离。"""
    settings = get_settings()
    eng = create_engine(
        settings.DATABASE_URL,
        pool_size=5,
        max_overflow=10,
        echo=False,
    )
    # 确保 pgvector 扩展和表存在
    with eng.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))
        conn.commit()
    SQLModel.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def db_session(test_engine):
    """函数级数据库会话，直接绑定 Engine 以确保 retriever 可读到已提交数据。

    测试中调用 session.commit() 即可持久化数据。
    清理工作由各测试自行负责（通过 try/finally 或 fixture teardown）。
    """
    session = Session(test_engine)
    yield session
    session.rollback()  # 安全回滚（如果测试未清理）
    session.close()


@pytest.fixture(autouse=True)
def _cleanup_bangumi_chunks(test_engine):
    """每个测试结束后清空 bangumi_chunks 表，保证数据库干净。

    autouse=True 意味着每个测试函数自动调用此 fixture。
    """
    yield
    from sqlmodel import Session, delete

    from database.models import BangumiChunk

    with Session(test_engine) as s:
        s.exec(delete(BangumiChunk))
        s.commit()


# ═══════════════════════════════════════════════════════════════
# Zhipu Embedding Mock Fixture
# ═══════════════════════════════════════════════════════════════


class FakeEmbeddingData:
    """模拟智谱 API 返回的 embedding 数据对象。"""

    def __init__(self, embedding: list[float]) -> None:
        self.embedding = embedding


class FakeEmbeddingResponse:
    """模拟智谱 API 返回的 response 对象。"""

    def __init__(self, embeddings: list[list[float]]) -> None:
        self.data = [FakeEmbeddingData(e) for e in embeddings]


@pytest.fixture
def mock_zhipu_embeddings():
    """Patch 智谱 ZhipuAiClient，拦截所有 embedding API 调用。

    返回一个函数 ``set_embeddings(query_vec, doc_vecs)``：
      - query_vec: 模拟查询向量（list[float]）
      - doc_vecs: 模拟文档向量列表（list[list[float]]）或单个向量

    实际使用示例::

        def test_retrieval(db_session, mock_zhipu_embeddings):
            mock_zhipu_embeddings(query_vec=QUERY_VEC, doc_vecs=[...])
            retriever = BangumiRetriever(test_engine, zhipu_api_key="mock")
            ...
    """

    def _setup(
        query_vec: list[float], doc_vecs: list[list[float]] | list[float]
    ) -> None:
        # 归一化 doc_vecs
        if doc_vecs and isinstance(doc_vecs[0], float):
            doc_vecs = [doc_vecs]  # type: ignore[assignment]

        call_count = [0]  # mutable counter

        def fake_create(*, model: str, input: list[str], **kwargs):
            call_count[0] += 1
            # 第一次调用是 query embedding，后续是 ingestion
            if call_count[0] == 1:
                return FakeEmbeddingResponse([query_vec])
            return FakeEmbeddingResponse(doc_vecs)  # type: ignore[arg-type]

        patcher = patch.object(
            target="zai.ZhipuAiClient.embeddings",
            new=MagicMock(),
        )
        mock_embeddings = patcher.start()
        mock_embeddings.create = fake_create

    return _setup


@pytest.fixture(autouse=True)
def cleanup_zhipu_patches():
    """自动清理所有 zai 相关 patch，防止跨测试污染。"""
    yield
    # 确保所有 patch 都被 stop
    patch.stopall()


# ═══════════════════════════════════════════════════════════════
# FastAPI TestClient Fixture
# ═══════════════════════════════════════════════════════════════


@pytest.fixture
def test_client(db_session):
    """FastAPI TestClient，注入事务回滚的数据库会话。"""
    from fastapi.testclient import TestClient

    from main import app

    def _override_get_session():
        yield db_session

    app.dependency_overrides[get_session] = _override_get_session
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()
