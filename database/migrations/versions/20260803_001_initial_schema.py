"""initial schema

Revision ID: 001
Revises: None
Create Date: 2026-08-03

完整的初始 schema：5 张表 + 所有索引。
由 SQLModel.metadata.create_all() 自动生成 DDL，并补充 autogenerate 检测不到的
HNSW 向量索引、GIN trigram 全文索引、部分索引。
"""

from typing import Sequence, Union

from alembic import op

revision: str = "001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 前置扩展：pgvector + pg_trgm（幂等）────────────────
    # 迁移链必须自包含：init_db() 的 CREATE EXTENSION 只覆盖应用启动路径，
    # 独立运行 `alembic upgrade head` 时空库会缺 vector 类型（P0-1 修复）。
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # ── 建表：从 ORM 模型自动生成 ──────────────────────────
    # 相当于旧的 SQLModel.metadata.create_all(engine)
    from sqlmodel import SQLModel
    from database.rag_tables import RagEntity, BangumiChunk  # noqa: F401
    from database.memory_tables import SessionMemory, UserProfile, PublicMemory  # noqa: F401

    SQLModel.metadata.create_all(bind=op.get_bind())

    # ── 自定义索引（create_all 不会创建以下索引）───────────

    # HNSW 向量索引 — rag_entities 语义检索
    op.create_index(
        "ix_rag_entities_embedding",
        "rag_entities",
        ["embedding"],
        unique=False,
        postgresql_ops={"embedding": "vector_cosine_ops"},
        postgresql_using="hnsw",
        if_not_exists=True,
    )

    # GIN trigram — rag_entities name 模糊匹配
    op.create_index(
        "ix_rag_entities_name_trgm",
        "rag_entities",
        ["name"],
        unique=False,
        postgresql_ops={"name": "gin_trgm_ops"},
        postgresql_using="gin",
        if_not_exists=True,
    )

    # NOTE: chunk_text 列已废弃（2026-08 移除，替换为 rag_output + embed_text），
    # 旧版 ix_rag_entities_chunk_text_trgm 索引随之失效——空库 create_all 不会建该列，
    # 在此建索引会导致 alembic upgrade head 崩溃（P0-1 修复）。旧库中的悬空列与索引无害，由 ORM 忽略。

    # B-Tree — rag_entities nsfw 安全护栏
    op.create_index(
        "ix_rag_entities_nsfw",
        "rag_entities",
        ["nsfw"],
        unique=False,
        if_not_exists=True,
    )

    # HNSW 向量索引 — session_memories 语义检索
    op.create_index(
        "ix_session_memories_embedding",
        "session_memories",
        ["embedding"],
        unique=False,
        postgresql_ops={"embedding": "vector_cosine_ops"},
        postgresql_using="hnsw",
        if_not_exists=True,
    )

    # B-Tree 复合索引 — session_memories 按用户+时间检索
    op.create_index(
        "ix_session_memories_user_created",
        "session_memories",
        ["user_id", "created_at"],
        unique=False,
        postgresql_using="btree",
        postgresql_ops={"created_at": "DESC"},
        if_not_exists=True,
    )

    # B-Tree — user_profiles 按 user_id 快速查找
    op.create_index(
        "ix_user_profiles_user_id",
        "user_profiles",
        ["user_id"],
        unique=False,
        if_not_exists=True,
    )

    # B-Tree — user_profiles 按最后活跃时间降序
    op.create_index(
        "ix_user_profiles_last_active",
        "user_profiles",
        ["last_active_at"],
        unique=False,
        postgresql_ops={"last_active_at": "DESC"},
        if_not_exists=True,
    )

    # HNSW 向量索引 — public_memories 语义检索
    op.create_index(
        "ix_public_memories_embedding",
        "public_memories",
        ["embedding"],
        unique=False,
        postgresql_ops={"embedding": "vector_cosine_ops"},
        postgresql_using="hnsw",
        if_not_exists=True,
    )

    # B-Tree 部分索引 — public_memories 活跃条目
    op.create_index(
        "ix_public_memories_active",
        "public_memories",
        ["is_active", "created_at"],
        unique=False,
        postgresql_using="btree",
        postgresql_ops={"created_at": "DESC"},
        postgresql_where="(is_active = TRUE)",
        if_not_exists=True,
    )

    # ── session_memories 去重 + 唯一约束（条件化——P0-1 修复）──
    # 当前 SessionMemory 模型已通过 __table_args__ 声明该约束，
    # create_all 会直接创建；此分支只服务约束尚不存在的旧库。
    from sqlalchemy import inspect

    inspector = inspect(op.get_bind())
    existing = {
        c["name"] for c in inspector.get_unique_constraints("session_memories")
    }
    if "uq_session_memories_user_session" not in existing:
        # 删除重复行：每 (user_id, session_id) 只保留最新一条
        op.execute("""
            DELETE FROM session_memories sm
            USING (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY user_id, session_id
                           ORDER BY created_at DESC
                       ) AS rn
                FROM session_memories
            ) dedup
            WHERE sm.id = dedup.id AND dedup.rn > 1
        """)

        # 添加复合唯一约束
        op.create_unique_constraint(
            "uq_session_memories_user_session",
            "session_memories",
            ["user_id", "session_id"],
        )

    # ── 数据回填：nsfw 标记从 meta_info JSONB 迁移到列 ──
    op.execute("""
        UPDATE rag_entities
            SET nsfw = TRUE
            WHERE meta_info @> '{"nsfw": true}' AND nsfw = FALSE
    """)


def downgrade() -> None:
    """完全回滚：删除所有表和索引。"""
    from sqlmodel import SQLModel
    from database.rag_tables import RagEntity, BangumiChunk  # noqa: F401
    from database.memory_tables import SessionMemory, UserProfile, PublicMemory  # noqa: F401

    SQLModel.metadata.drop_all(bind=op.get_bind())
