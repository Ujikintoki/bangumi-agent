"""
数据库管理命令行工具

用法::

    python database/cli.py init      # 初始化数据库（扩展 + 迁移）
    python database/cli.py seed      # 灌入种子数据（subject/character/person）
    python database/cli.py status    # 显示各表行数 + 迁移版本
    python database/cli.py reset     # 重建数据库（危险！仅开发用）
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(message)s",
)
logger = logging.getLogger("database.cli")


def cmd_init() -> None:
    """初始化数据库。"""
    print("[init] 启用扩展 + 执行迁移...")
    from database.engine import init_db

    init_db()
    print("[init] ✓ 完成")


def cmd_seed() -> None:
    """灌入种子数据。"""
    print("[seed] 启动种子数据注入...")
    from database.seed.seed_data import run_seed

    asyncio.run(run_seed())
    print("[seed] ✓ 完成")


def cmd_status() -> None:
    """显示当前数据库状态。"""
    from sqlalchemy import text
    from database.engine import engine

    with engine.connect() as conn:
        # 表行数
        tables = [
            "rag_entities",
            "session_memories",
            "public_memories",
            "user_profiles",
            "bangumi_chunks",
        ]
        print("\n══ 表行数 ══")
        for t in tables:
            try:
                result = conn.execute(text(f"SELECT COUNT(*) FROM {t}"))
                count = result.scalar()
                print(f"  {t:<25} {count:>6}")
            except Exception:
                print(f"  {t:<25}  (不存在)")

        # rag_entities 分类
        try:
            print("\n══ rag_entities 分布 ══")
            result = conn.execute(
                text(
                    "SELECT entity_type, COUNT(*) FROM rag_entities "
                    "GROUP BY entity_type ORDER BY COUNT(*) DESC"
                )
            )
            for row in result:
                print(f"  {row[0]:<12} {row[1]:>6}")
        except Exception:
            pass

        # Alembic 版本
        try:
            print("\n══ 迁移版本 ══")
            from alembic.config import Config
            from alembic import command

            alembic_ini = (
                Path(__file__).resolve().parent / "migrations" / "alembic.ini"
            )
            alembic_cfg = Config(str(alembic_ini))
            # alembic current 输出到 stderr，我们直接读数据库
            result = conn.execute(
                text("SELECT version_num FROM alembic_version")
            )
            version = result.scalar()
            print(f"  Current: {version}")
        except Exception:
            print("  (alembic_version 表不存在)")

        conn.commit()

    # 数据库连接信息
    print("\n══ 连接信息 ══")
    from core.config import get_settings
    from urllib.parse import urlparse

    s = get_settings()
    url = urlparse(
        s.DATABASE_URL.replace("postgresql+psycopg2://", "postgresql://")
    )
    print(f"  Host: {url.hostname}:{url.port}")
    print(f"  Database: {url.path.lstrip('/')}")
    print(f"  User: {url.username}")
    print(f"  pgvector: ✓ (已启用)\n")


def cmd_reset() -> None:
    """重建数据库（危险！仅开发用）。"""
    confirm = input(
        "⚠ 这将删除所有数据并重建数据库。确定吗？[y/N] "
    )
    if confirm.lower() != "y":
        print("已取消")
        return

    from sqlalchemy import text
    from database.engine import engine

    print("[reset] 删除所有表...")
    with engine.connect() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version CASCADE"))
        conn.execute(text("DROP TABLE IF EXISTS session_memories CASCADE"))
        conn.execute(text("DROP TABLE IF EXISTS user_profiles CASCADE"))
        conn.execute(text("DROP TABLE IF EXISTS public_memories CASCADE"))
        conn.execute(text("DROP TABLE IF EXISTS rag_entities CASCADE"))
        conn.execute(text("DROP TABLE IF EXISTS bangumi_chunks CASCADE"))
        conn.commit()

    print("[reset] 重新初始化...")
    cmd_init()
    print("[reset] ✓ 完成（可以运行 seed 灌入数据）")


# ═══════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(description="BGM Agent 数据库管理")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init", help="初始化数据库（扩展 + 迁移）")
    sub.add_parser("seed", help="灌入种子数据")
    sub.add_parser("status", help="显示数据库状态")
    sub.add_parser("reset", help="重建数据库（危险！）")

    args = parser.parse_args()

    if args.command == "init":
        cmd_init()
    elif args.command == "seed":
        cmd_seed()
    elif args.command == "status":
        cmd_status()
    elif args.command == "reset":
        cmd_reset()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
