"""
数据库连接层

负责 SQLAlchemy Engine 的初始化、pgvector 扩展的自动启用，
以及 SQLModel Session 的生命周期管理。
"""

from collections.abc import Generator

from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlmodel import Session, SQLModel, create_engine

from core.config import get_settings

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
    """初始化数据库表结构及必要扩展。

    执行顺序：
    1. 启用 pgvector 扩展（幂等操作，重复执行无副作用）。
    2. 根据所有注册的 SQLModel 子类自动建表（仅创建不存在的表）。

    Raises:
        OperationalError: 数据库连接失败时抛出，调用方应捕获并决定是否重试。
        ProgrammingError: SQL 执行错误（如权限不足无法创建扩展）时抛出。
    """
    try:
        with engine.connect() as conn:
            # 开启 pgvector 扩展以支持 Vector 列类型
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))
            conn.commit()
    except OperationalError:
        raise
    except ProgrammingError:
        raise

    try:
        SQLModel.metadata.create_all(engine)
    except OperationalError:
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
