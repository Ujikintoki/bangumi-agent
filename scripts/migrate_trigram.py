"""
pg_trgm 扩展 + trigram 索引迁移

为 name / name_cn 列创建 trigram GIN 索引，用于模糊名称匹配。
pg_trgm 是 PostgreSQL 自带扩展，无需额外安装。

用法::

    python scripts/migrate_trigram.py
"""

from __future__ import annotations

from database.engine import engine
from sqlmodel import Session, text

MIGRATIONS = [
    # Step 1: 启用扩展
    "CREATE EXTENSION IF NOT EXISTS pg_trgm",

    # Step 2: GIN trigram 索引 — name 列（日文名）
    """
    CREATE INDEX IF NOT EXISTS idx_rag_entities_name_trgm
    ON rag_entities USING GIN (name gin_trgm_ops)
    """,

    # Step 3: GIN trigram 索引 — name_cn 列（中文名）
    """
    CREATE INDEX IF NOT EXISTS idx_rag_entities_name_cn_trgm
    ON rag_entities USING GIN (name_cn gin_trgm_ops)
    """,
]


def migrate():
    with Session(engine) as session:
        for i, sql in enumerate(MIGRATIONS):
            print(f"  [{i+1}/{len(MIGRATIONS)}]", end=" ... ")
            try:
                session.exec(text(sql))
                session.commit()
                print("✓")
            except Exception as e:
                print(f"✗ ({e})")
                session.rollback()

    print("\n  验证索引:")
    with Session(engine) as session:
        rows = session.exec(text("""
            SELECT indexname FROM pg_indexes
            WHERE tablename = 'rag_entities'
            AND indexname LIKE '%trgm%'
        """)).all()
        for r in rows:
            print(f"    ✓ {r[0]}")


if __name__ == "__main__":
    migrate()
