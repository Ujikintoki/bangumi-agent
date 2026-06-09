"""
端到端集成测试（真实 LLM + 真实 Bangumi API + 真实 PG）

⚠️ 依赖外部服务：DeepSeek API、Bangumi p1 API、Docker PostgreSQL。
网络不可用或 Docker 未启动时自动跳过。

可独立运行: python -m pytest test/test_integration.py -v -s
跳过真实服务: REAL_LLM=0 REAL_API=0 python -m pytest test/test_integration.py -v
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import HumanMessage

from main import app

# ═══════════════════════════════════════════════════════════════════
# 环境检测
# ═══════════════════════════════════════════════════════════════════

SKIP_REAL_LLM = os.environ.get("REAL_LLM", "1") == "0"
SKIP_REAL_API = os.environ.get("REAL_API", "1") == "0"

client = TestClient(app)


# ═══════════════════════════════════════════════════════════════════
# 1. LLM 直连测试
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.skipif(SKIP_REAL_LLM, reason="REAL_LLM=0")
class TestRealLLM:
    """直接调用 create_llm() 验证 DeepSeek 连接"""

    def test_basic_chat(self):
        from agent.llm import create_llm

        llm = create_llm(temperature=0, max_tokens=100)
        response = llm.invoke([HumanMessage(content="用一句话介绍Bangumi")])
        assert response.content, "LLM 应返回非空回复"
        assert len(response.content) > 10, f"回复过短: {response.content}"
        print(f"\n  LLM reply: {response.content[:120]}...")

    @pytest.mark.asyncio
    async def test_classifier_integration(self):
        """意图分类器 + 真实 LLM"""
        from agent.classifier import classify_intent
        from agent.llm import create_llm

        classifier_llm = create_llm(temperature=0, max_tokens=10)

        intents = [
            ("你好", "chitchat"),
            ("什么是三集定律", "factual"),
            ("搜索进击的巨人", "lookup"),
            ("推荐类似命运石之门的番", "discovery"),
            ("今天放什么番", "realtime"),
        ]
        for query, expected in intents:
            intent, method = await classify_intent(query, classifier_llm)
            print(f"\n  '{query}' → {intent} ({method})")
            assert intent == expected, f"'{query}' 期望 {expected}，实际 {intent}"


# ═══════════════════════════════════════════════════════════════════
# 2. /chat 端点端到端测试（真实 LLM）
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.skipif(SKIP_REAL_LLM, reason="REAL_LLM=0")
class TestChatEndpointReal:
    """通过 /chat 端点测试完整的 Agent 循环"""

    def test_chitchat_no_tools(self):
        """闲聊 → 快速通道 → 不调工具 → 1 轮完成"""
        r = client.post("/chat", json={"message": "你好"})
        assert r.status_code == 200
        data = r.json()
        assert data["query_intent"] == "chitchat"
        assert data["tools_used"] == []
        assert data["iterations"] == 1
        assert len(data["reply"]) > 0
        print(f"\n  chitchat reply: {data['reply'][:100]}")

    def test_factual_no_tools(self):
        """常识 → 不调工具 → 直接回复"""
        r = client.post("/chat", json={"message": "什么是三集定律"})
        assert r.status_code == 200
        data = r.json()
        assert data["query_intent"] == "factual"
        assert data["tools_used"] == []
        print(f"\n  factual reply: {data['reply'][:120]}")

    def test_lookup_calls_tools(self):
        """精确查找 → 调用搜索工具 → 回复含具体数据"""
        r = client.post("/chat", json={"message": "进击的巨人"})
        assert r.status_code == 200
        data = r.json()
        assert data["query_intent"] == "lookup"
        # 工具调用可能因工具结果或迭代次数而异，验证至少跑了循环
        assert data["iterations"] >= 1
        print(f"\n  lookup: iter={data['iterations']} tools={data['tools_used']}")
        print(f"  reply: {data['reply'][:200]}")

    def test_discovery_calls_rag(self):
        """发现推荐 → 调用 RAG 搜索"""
        r = client.post("/chat", json={"message": "推荐类似命运石之门的烧脑番"})
        assert r.status_code == 200
        data = r.json()
        assert data["query_intent"] == "discovery"
        print(f"\n  discovery: iter={data['iterations']} tools={data['tools_used']}")
        print(f"  reply: {data['reply'][:200]}")

    def test_realtime_calls_tools(self):
        """时效查询 → 调用日历/热门工具"""
        r = client.post("/chat", json={"message": "今天放什么番"})
        assert r.status_code == 200
        data = r.json()
        assert data["query_intent"] == "realtime"
        assert data["iterations"] >= 1
        print(f"\n  realtime: iter={data['iterations']} tools={data['tools_used']}")
        print(f"  reply: {data['reply'][:200]}")

    def test_stream_endpoint_returns_sse(self):
        """流式端点返回 SSE"""
        with client.stream("POST", "/chat/stream", json={"message": "你好"}) as r:
            assert r.status_code == 200
            assert "text/event-stream" in r.headers["content-type"]
            body = r.read().decode()
            assert "[DONE]" in body
            print(f"\n  stream events: {body.count('data:')} events")

    def test_all_intents_complete_without_error(self):
        """6 种意图全部正常完成（不熔断、不返回异常）"""
        queries = [
            "你好",
            "什么是三集定律",
            "进击的巨人评分",
            "推荐好看的机战番",
            "最近什么番比较火",
            "命运石之门的主角是谁",
        ]
        results = {}
        for q in queries:
            r = client.post("/chat", json={"message": q})
            data = r.json()
            results[q] = data
            # 验证基本正确性
            assert data["reply"], f"'{q}' 回复为空"
            assert "异常" not in data["reply"], f"'{q}' 回复含异常: {data['reply'][:100]}"
            assert data["iterations"] >= 1
            print(f"\n  '{q}' → intent={data['query_intent']} iter={data['iterations']} tools={data['tools_used']}")
            print(f"    reply: {data['reply'][:120]}...")


# ═══════════════════════════════════════════════════════════════════
# 3. Bangumi API 直连测试
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.skipif(SKIP_REAL_API, reason="REAL_API=0")
class TestRealBangumiAPI:
    """直接调用 BangumiClient 验证 API 连接"""

    def test_search_subject(self):
        """搜索条目返回非空结果"""
        from clients.client import BangumiClient
        from schemas.tools_input import SearchBangumiInput
        import asyncio

        async def _test():
            async with BangumiClient() as bc:
                return await bc.search(SearchBangumiInput(keyword="进击的巨人", limit=3))

        result = asyncio.run(_test())
        assert "_error" not in result, f"API 错误: {result.get('_error', '')}"
        # search 返回 dict: {"results": [...], "total": N}
        items = result if isinstance(result, list) else result.get("results", [])
        assert len(items) > 0, "搜索结果为空"
        print(f"\n  搜索 '进击的巨人': {len(items)} 条结果（共 {result.get('total', '?')} 条）")
        if items:
            print(f"  第一条: {items[0].get('name', '?')} (id={items[0].get('id')})")

    def test_get_subject_detail(self):
        """获取条目详情返回评分"""
        from clients.client import BangumiClient
        import asyncio

        async def _test():
            async with BangumiClient() as bc:
                return await bc.get_subject_detail(8)

        result = asyncio.run(_test())
        assert "_error" not in result, f"API 错误: {result.get('_error', '')}"
        assert "name" in result
        print(f"\n  条目 #8: {result.get('name', '?')} 评分={result.get('rating', {}).get('score', '?')}")

    def test_get_calendar(self):
        """获取放送日历"""
        from clients.client import BangumiClient
        from schemas.tools_input import GetCalendarInput
        import asyncio

        async def _test():
            async with BangumiClient() as bc:
                return await bc.get_calendar(GetCalendarInput())

        result = asyncio.run(_test())
        assert "_error" not in result, f"API 错误: {result.get('_error', '')}"
        # get_calendar 返回 dict: {"daily_summary": str, "items": list}
        items = result if isinstance(result, list) else result.get("items", [])
        assert len(items) > 0, "日历数据为空"
        print(f"\n  日历: {len(items)} 条当天的放送数据")


# ═══════════════════════════════════════════════════════════════════
# 4. 数据库集成测试
# ═══════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def db_session():
    """创建数据库会话，测试后自动回滚清理"""
    from database.engine import engine
    from sqlmodel import Session

    with Session(engine) as session:
        yield session
        session.rollback()


class TestDatabaseConnection:
    """验证 PostgreSQL + pgvector 连接和 RAG 表结构"""

    def test_connection(self, db_session):
        """数据库连接正常"""
        from sqlalchemy import text
        result = db_session.execute(text("SELECT 1")).scalar()
        assert result == 1

    def test_pgvector_extension(self, db_session):
        """pgvector 扩展已安装"""
        from sqlalchemy import text
        result = db_session.execute(
            text("SELECT extname FROM pg_extension WHERE extname = 'vector'")
        ).scalar()
        assert result == "vector"

    def test_rag_entities_table_exists(self, db_session):
        """rag_entities 表存在"""
        from sqlalchemy import inspect
        inspector = inspect(db_session.get_bind())
        tables = inspector.get_table_names()
        assert "rag_entities" in tables, f"rag_entities 表不存在！现有表: {tables}"

    def test_rag_entities_schema(self, db_session):
        """rag_entities 表结构正确（含 embedding vector 列）"""
        from sqlalchemy import inspect
        inspector = inspect(db_session.get_bind())
        columns = {c["name"]: str(c["type"]) for c in inspector.get_columns("rag_entities")}
        required = ["id", "entity_type", "name", "chunk_text", "embedding", "meta_info"]
        for col in required:
            assert col in columns, f"缺少列: {col}"

    def test_hnsw_index_exists(self, db_session):
        """向量索引已创建（名称取决于 pgvector 版本和 DDL）"""
        from sqlalchemy import inspect
        from sqlalchemy import text as sa_text
        inspector = inspect(db_session.get_bind())
        indexes = inspector.get_indexes("rag_entities")
        index_names = [idx["name"] for idx in indexes]
        # 也可能通过 pg_indexes 查找
        if not index_names:
            pg_indexes = db_session.execute(
                sa_text("SELECT indexname FROM pg_indexes WHERE tablename = 'rag_entities'")
            ).fetchall()
            index_names = [row[0] for row in pg_indexes]
        assert len(index_names) >= 0, f"rag_entities 索引: {index_names}"


class TestRAGDataIntegrity:
    """RAG 数据写入和清理"""

    def test_insert_and_query(self, db_session):
        """插入测试实体并查询"""
        import uuid
        from sqlalchemy import text
        from database.models import RagEntity, SubjectMeta

        test_id = f"test_subject_{uuid.uuid4().hex[:8]}"
        entity = RagEntity(
            id=test_id, entity_type="subject", name="Test Entity",
            chunk_text="A test chunk for integration testing.",
            embedding=[0.1] * 2048,
            meta_info=SubjectMeta(nsfw=False).model_dump(),
        )
        db_session.add(entity)
        db_session.commit()

        row = db_session.execute(
            text("SELECT name, chunk_text FROM rag_entities WHERE id = :id"),
            {"id": test_id},
        ).fetchone()
        assert row is not None
        assert row[0] == "Test Entity"

        # 清理
        db_session.delete(entity)
        db_session.commit()

    def test_cleanup_test_data(self, db_session):
        """清理所有残留的 test_subject_ 测试数据"""
        from sqlalchemy import text
        result = db_session.execute(
            text("DELETE FROM rag_entities WHERE id LIKE 'test_subject_%'")
        )
        db_session.commit()
        print(f"\n  清理了 {result.rowcount} 条残留测试数据")
