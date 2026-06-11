"""
数据库连接层

负责 SQLAlchemy Engine 的初始化、pgvector/pg_trgm 扩展的自动启用、
rag_entities 表高性能索引的创建，以及 SQLModel Session 的生命周期管理。
"""

from collections.abc import Generator

from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlmodel import Session, SQLModel, create_engine

from core.config import get_settings

# 注册记忆系统 ORM 模型到 SQLModel.metadata
# （create_all 通过元类自动发现，import 即注册）
from database.memory_tables import PublicMemory, SessionMemory, UserProfile  # noqa: F401

# ── Engine 初始化 ──────────────────────────────────────────────
# 拉取配置中的数据库 URL，允许通过环境变量覆盖
settings = get_settings()
database_url: str = settings.DATABASE_URL

engine = create_engine(
    database_url,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,  # 每次从池中取出连接前先 ping，防止使用已断开的连接
    echo=(settings.ENVIRONMENT == "development"),
)
"""SQLAlchemy Engine 实例，全局复用。"""


def init_db() -> None:
    """初始化数据库表结构、必要扩展及高性能索引。

    执行顺序：
    1. 启用 pgvector 扩展（幂等）。
    2. 启用 pg_trgm 扩展，用于 GIN 三元组全文索引。
    3. 根据所有注册的 SQLModel 子类自动建表。
    4. 创建 HNSW 向量索引和 GIN trigram 全文索引（幂等）。

    Raises:
        OperationalError: 数据库连接失败时抛出。
        ProgrammingError: SQL 执行错误（如权限不足）时抛出。
    """
    try:
        with engine.connect() as conn:
            # 开启 pgvector 扩展以支持 Vector 列类型
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))
            # 开启 pg_trgm 扩展以支持 GIN 三元组全文索引
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm;"))
            conn.commit()
    except OperationalError:
        raise
    except ProgrammingError:
        raise

    try:
        SQLModel.metadata.create_all(engine)
    except OperationalError:
        raise

    # ── 高性能索引创建（幂等 DDL）──────────────────────────────
    _INDEX_DDL_STATEMENTS = [
        # HNSW 向量余弦距离索引 — 加速语义检索的向量最近邻查询
        """
        CREATE INDEX IF NOT EXISTS ix_rag_entities_embedding
            ON rag_entities USING hnsw (embedding vector_cosine_ops);
        """,
        # GIN trigram 索引 — 加速 name 列的模糊匹配与 LIKE 查询
        """
        CREATE INDEX IF NOT EXISTS ix_rag_entities_name_trgm
            ON rag_entities USING gin (name gin_trgm_ops);
        """,
        # GIN trigram 索引 — 加速 chunk_text 列的模糊匹配与 LIKE 查询
        """
        CREATE INDEX IF NOT EXISTS ix_rag_entities_chunk_text_trgm
            ON rag_entities USING gin (chunk_text gin_trgm_ops);
        """,
        # ── Phase 5 记忆系统索引 ──────────────────────────
        # HNSW 向量索引 — session_memories 语义检索
        """
        CREATE INDEX IF NOT EXISTS ix_session_memories_embedding
            ON session_memories USING hnsw (embedding vector_cosine_ops);
        """,
        # B-tree 复合索引 — 按用户 ID + 创建时间降序检索最近 session
        """
        CREATE INDEX IF NOT EXISTS ix_session_memories_user_created
            ON session_memories (user_id, created_at DESC);
        """,
        # B-tree 索引 — user_profiles 按 user_id 快速查找
        """
        CREATE INDEX IF NOT EXISTS ix_user_profiles_user_id
            ON user_profiles (user_id);
        """,
        # B-tree 部分索引 — user_profiles 按最后活跃时间降序
        """
        CREATE INDEX IF NOT EXISTS ix_user_profiles_last_active
            ON user_profiles (last_active_at DESC);
        """,
        # HNSW 向量索引 — public_memories 语义检索（Phase 6 用）
        """
        CREATE INDEX IF NOT EXISTS ix_public_memories_embedding
            ON public_memories USING hnsw (embedding vector_cosine_ops);
        """,
        # B-tree 部分索引 — public_memories 活跃条目按时间降序
        """
        CREATE INDEX IF NOT EXISTS ix_public_memories_active
            ON public_memories (is_active, created_at DESC)
            WHERE is_active = TRUE;
        """,
    ]

    try:
        with engine.connect() as conn:
            for idx_ddl in _INDEX_DDL_STATEMENTS:
                conn.execute(text(idx_ddl))
            conn.commit()
    except (OperationalError, ProgrammingError):
        # 索引创建失败不阻塞启动（如 pgvector/hnsw 不可用时），
        # 实际生产环境应由运维确保扩展已安装
        raise


def get_session() -> Generator[Session, None, None]:
    """FastAPI 依赖注入用的 Session 生成器。

    每次调用 yield 一个全新的数据库会话实例，请求结束后自动关闭，
    确保连接归还到连接池，避免连接泄漏。

    Yields:
        Session: SQLModel 数据库会话实例。

    Example:
        >>> from fastapi import Depends
        >>> @app.get("/chunks")
        >>> def list_chunks(session: Session = Depends(get_session)):
        ...     return session.exec(select(BangumiChunk)).all()
    """
    with Session(engine) as session:
        try:
            yield session
        finally:
            session.close()
