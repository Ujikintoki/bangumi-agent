"""add popularity column

Revision ID: 7a94e5c746c4
Revises: 001
Create Date: 2026-08-04 01:17:30

将热度信号从 meta_info JSONB 冗余提取为列级字段：
  - subject     → meta_info.rating_total
  - character   → meta_info.collects
  - person      → meta_info.collects

同时建立 B-Tree 索引，加速检索排序 (ORDER BY popularity DESC)。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "7a94e5c746c4"
down_revision: Union[str, Sequence[str], None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    from sqlalchemy import inspect

    # 1. 加列（条件化——P0-1 修复）
    #    · 空库路径：001 的 create_all 按当前 ORM 建表，popularity 列已存在，跳过。
    #    · 旧库路径：迁移前创建的表无该列，此处补列 + 回填。
    columns = {c["name"] for c in inspect(bind).get_columns("rag_entities")}
    if "popularity" not in columns:
        op.add_column(
            "rag_entities",
            sa.Column("popularity", sa.Integer(), nullable=False, server_default="0"),
        )

    # 2. B-Tree 索引
    op.create_index(
        "ix_rag_entities_popularity",
        "rag_entities",
        ["popularity"],
        unique=False,
        if_not_exists=True,
    )

    # 3. 回填已有数据：从 meta_info JSONB 提取
    #    subject → rating_total, character/person → collects
    op.execute("""
        UPDATE rag_entities
        SET popularity = COALESCE(
            (meta_info->>'rating_total')::int,
            (meta_info->>'collects')::int,
            0
        )
        WHERE popularity = 0
    """)


def downgrade() -> None:
    op.drop_index("ix_rag_entities_popularity", table_name="rag_entities")
    op.drop_column("rag_entities", "popularity")
