"""
数据库连接层

负责 SQLAlchemy Engine 的初始化、pgvector/pg_trgm 扩展的自动启用、
以及 SQLModel Session 的生命周期管理。

Schema 变更由 Alembic 管理，init_db() 仅负责启用扩展并调用
``alembic upgrade head`` 将数据库同步到最新版本。

用法::

    from database.engine import engine, init_db, get_session

    init_db()                          # app 启动时调用一次
    session = next(get_session())      # FastAPI 依赖注入
"""

from collections.abc import Generator
from pathlib import Path
import logging

from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlmodel import Session, SQLModel, create_engine

from core.config import get_settings

# 注册 ORM 模型到 SQLModel.metadata（create_all / Alembic autogenerate 通过
# SQLModel 元类自动发现 table=True 的子类，import 即注册）
from database.memory_tables import PublicMemory, SessionMemory, UserProfile  # noqa: F401

logger = logging.getLogger("bgm-agent.database")

# ── Engine 初始化 ──────────────────────────────────────────────

settings = get_settings()
database_url: str = settings.DATABASE_URL

engine = create_engine(
    database_url,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    echo=(settings.ENVIRONMENT == "development"),
)
"""SQLAlchemy Engine 实例，全局复用。"""


# ── 数据库初始化 ──────────────────────────────────────────────


def init_db() -> None:
    """初始化数据库。

    执行顺序：
    1. 启用 pgvector + pg_trgm 扩展（幂等）。
    2. 运行 Alembic 迁移，将 schema 同步到最新版本。

    所有表结构、索引、数据迁移均由 Alembic 迁移文件管理，
    此函数不再包含任何手动 DDL。

    Raises:
        OperationalError: 数据库连接失败。
    """
    # 启用必要扩展
    try:
        with engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
            conn.commit()
    except OperationalError:
        logger.error("数据库连接失败，请检查 DATABASE_URL 和数据库状态")
        raise
    except ProgrammingError as e:
        logger.error("扩展创建失败（可能权限不足）: %s", e)
        raise

    # 运行 Alembic 迁移
    try:
        from alembic.config import Config
        from alembic import command

        alembic_ini = Path(__file__).resolve().parent / "migrations" / "alembic.ini"
        alembic_cfg = Config(str(alembic_ini))
        command.upgrade(alembic_cfg, "head")
        logger.info("数据库迁移完成")
    except Exception as e:
        logger.error("Alembic 迁移失败: %s", e)
        raise


# ── Session 管理 ──────────────────────────────────────────────


def get_session() -> Generator[Session, None, None]:
    """FastAPI 依赖注入用的 Session 生成器。

    每次调用 yield 一个全新的数据库会话实例，请求结束后自动关闭，
    确保连接归还到连接池。

    Yields:
        Session: SQLModel 数据库会话实例。
    """
    with Session(engine) as session:
        try:
            yield session
        finally:
            session.close()
