"""
数据库迁移: 为 rag_entities 添加 subject_type 列 + 回填已有数据

用法::

    source .venv/bin/activate
    python scripts/migrate_subject_type.py

步骤:
  1. ALTER TABLE 添加 subject_type INTEGER 列（如不存在）
  2. CREATE INDEX（如不存在）
  3. 根据 discover 产物回填已有数据:
     - subject_ids_type2.json 中的 ID → subject_type = 2（动画）
     - subject_ids_type1.json 中的 ID → subject_type = 1（书籍）

状态（2026-09-13 核）:
  迁移已执行完毕，DB 现状 1030 条 subject = 130 书籍(type1) + 900 动画(type2)，
  全表 1500 条（+350 character +120 person）。脚本保留为可重入的运维工具——
  ALTER/CREATE 都带 IF NOT EXISTS，回填是幂等 UPDATE，重跑无副作用。
  Step 3 自报的数字取自 rowcount（实际匹配行数），可直接引用。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATA_DIR = Path(__file__).resolve().parent / "data"


def main():
    from database.engine import engine
    from sqlmodel import Session, text

    with Session(engine) as session:
        # ── Step 1: 添加列 ──
        print("Step 1: 添加 subject_type 列...")
        try:
            session.exec(text(
                "ALTER TABLE rag_entities ADD COLUMN IF NOT EXISTS subject_type INTEGER"
            ))
            session.commit()
            print("  ✓ subject_type 列已就绪")
        except Exception as e:
            session.rollback()
            print(f"  ⚠ 添加列失败: {e}")
            return

        # ── Step 2: 创建索引 ──
        print("Step 2: 创建索引...")
        try:
            session.exec(text(
                "CREATE INDEX IF NOT EXISTS ix_rag_entities_subject_type "
                "ON rag_entities (subject_type)"
            ))
            session.commit()
            print("  ✓ 索引已就绪")
        except Exception as e:
            session.rollback()
            print(f"  ⚠ 创建索引失败: {e}")

        # ── Step 3: 回填 subject_type ──
        print("Step 3: 回填已有数据...")

        # 加载 ID 列表
        type2_ids: set[int] = set()
        type1_ids: set[int] = set()

        for fname, id_set, label in [
            ("subject_ids_type2.json", type2_ids, "anime"),
            ("subject_ids_type1.json", type1_ids, "book"),
        ]:
            filepath = DATA_DIR / fname
            if filepath.exists():
                with open(filepath, encoding="utf-8") as f:
                    data = json.load(f)
                for item in data:
                    if isinstance(item.get("id"), int):
                        id_set.add(item["id"])
                print(f"  加载 {label} IDs: {len(id_set)}")
            else:
                print(f"  ⚠ {fname} 不存在，跳过")

        # 回填
        for label, ids, stype in [
            ("anime (type=2)", type2_ids, 2),
            ("book (type=1)", type1_ids, 1),
        ]:
            if not ids:
                print(f"  跳过 {label}: 无 ID")
                continue
            # 批量更新（按 500 分批）
            id_list = sorted(ids)
            matched = 0    # 真的改到的行数（来自 rowcount）
            targeted = 0   # 打算改的行数（来自 ID 列表）
            for i in range(0, len(id_list), 500):
                batch = id_list[i : i + 500]
                prefixed = [f"subject_{sid}" for sid in batch]
                placeholders = ", ".join([f"'{p}'" for p in prefixed])
                sql = (
                    f"UPDATE rag_entities SET subject_type = {stype} "
                    f"WHERE entity_type = 'subject' AND id IN ({placeholders})"
                )
                result = session.exec(text(sql))
                # total 必须累加 rowcount，不能累加 len(batch)。
                # len(batch) 是"打算改多少"：WHERE 少匹配时脚本照样报满数，而迁移
                # 脚本自报的数字是会被引用的。rowcount 要在 commit 之前读。
                matched += getattr(result, "rowcount", 0) or 0
                session.commit()
                targeted += len(batch)
            if matched == targeted:
                print(f"  ✓ {label}: {matched} 条已设置为 subject_type={stype}")
            else:
                # 对不上是常态而非故障：ID 列表来自 discover 的热门榜，库里未必
                # 每条都灌过。两个数都印出来，避免"匹配 N 条"被读成"覆盖了 N 条"。
                print(f"  ⚠ {label}: 实际匹配 {matched} 条（ID 列表里给了 {targeted} 条，"
                      f"其余不在 DB 中）")

        # ── 验证 ──
        print("\nStep 4: 验证...")
        counts = session.exec(text(
            "SELECT subject_type, COUNT(*) FROM rag_entities "
            "WHERE entity_type = 'subject' GROUP BY subject_type ORDER BY subject_type"
        )).all()
        for st, cnt in counts:
            label = {1: "书籍", 2: "动画", None: "NULL"}.get(st, str(st))
            print(f"  subject_type={st} ({label}): {cnt} 条")

        null_count = session.exec(text(
            "SELECT COUNT(*) FROM rag_entities "
            "WHERE entity_type = 'subject' AND subject_type IS NULL"
        )).scalar()
        if null_count:
            print(f"  ⚠ 仍有 {null_count} 条 subject 的 subject_type 为 NULL（不在 ID 列表中）")

        print("\n迁移完成。")


if __name__ == "__main__":
    main()
