"""Alembic 迁移环境配置。

从项目 ``core.config`` 读取数据库 URL，使用 ``SQLModel.metadata``
作为 autogenerate 的目标元数据。

用法（从项目根目录运行）::

    alembic -c database/migrations/alembic.ini upgrade head
    alembic -c database/migrations/alembic.ini revision --autogenerate -m "..."
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# ── Alembic Config 对象 ──────────────────────────────────────
config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ── 从项目配置读取数据库 URL ─────────────────────────────────
from core.config import get_settings

settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

# ── 导入所有 ORM 模型以注册到 SQLModel.metadata ──────────────
# 必须导入所有定义了 table=True 的 SQLModel 子类，autogenerate 才能检测到
from sqlmodel import SQLModel
from database.rag_tables import RagEntity, BangumiChunk  # noqa: F401
from database.memory_tables import SessionMemory, UserProfile, PublicMemory  # noqa: F401

target_metadata = SQLModel.metadata


# ═══════════════════════════════════════════════════════════════
# 迁移执行
# ═══════════════════════════════════════════════════════════════


def run_migrations_offline() -> None:
    """离线模式——只生成 SQL 脚本，不连数据库。"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式——连接数据库并执行迁移。"""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
