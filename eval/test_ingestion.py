"""
Phase 1.4 Ingestion 管线测试 + Round-trip 验证

加载 Phase 1.3 收集的真实数据，通过 RagEntityIngestor 写入数据库，
验证写入后数据的完整性。

用法::

    python eval/test_ingestion.py                    # 全部类型
    python eval/test_ingestion.py --type character   # 仅角色
    python eval/test_ingestion.py --dry-run           # 仅验证数据格式，不实际写入
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(message)s",
)
logger = logging.getLogger("eval.ingestion_test")


# ═══════════════════════════════════════════════════════════════════════
# 数据格式校验
# ═══════════════════════════════════════════════════════════════════════


def validate_subjects(data: list[dict]) -> dict:
    """校验 subject 数据格式是否匹配 ingest_subjects() 期望。"""
    required = {"subject_id", "name", "chunk_text"}
    optional = {"name_cn", "score", "rank", "rating_total", "rating_count",
                "collection", "date", "year", "platform", "eps", "nsfw", "tags"}
    errors: list[str] = []
    warnings: list[str] = []

    for i, item in enumerate(data):
        for key in required:
            if key not in item or not item[key]:
                errors.append(f"subjects[{i}]: missing required field '{key}'")

        # 检查类型
        if not isinstance(item.get("subject_id"), int):
            errors.append(f"subjects[{i}]: subject_id must be int, got {type(item['subject_id']).__name__}")

        if not item.get("chunk_text"):
            warnings.append(f"subjects[{i}]: empty chunk_text")

        if not isinstance(item.get("tags", []), list):
            errors.append(f"subjects[{i}]: tags must be list")

    return {"valid": len(errors) == 0, "errors": errors, "warnings": warnings, "count": len(data)}


def validate_characters(data: list[dict]) -> dict:
    """校验 character 数据格式。"""
    required = {"character_id", "name", "chunk_text"}
    errors: list[str] = []
    warnings: list[str] = []

    for i, item in enumerate(data):
        for key in required:
            if key not in item or not item[key]:
                errors.append(f"characters[{i}]: missing required field '{key}'")

        if not isinstance(item.get("character_id"), int):
            errors.append(f"characters[{i}]: character_id must be int")

        if not isinstance(item.get("casts_raw", []), list):
            errors.append(f"characters[{i}]: casts_raw must be list")

        # 检查 casts_raw 内部格式
        for j, cast in enumerate(item.get("casts_raw", [])):
            if not isinstance(cast.get("subject_id"), int):
                errors.append(f"characters[{i}].casts_raw[{j}]: subject_id must be int, got {type(cast.get('subject_id')).__name__}")
            if not cast.get("subject_name"):
                warnings.append(f"characters[{i}].casts_raw[{j}]: empty subject_name")

    return {"valid": len(errors) == 0, "errors": errors, "warnings": warnings, "count": len(data)}


def validate_persons(data: list[dict]) -> dict:
    """校验 person 数据格式。"""
    required = {"person_id", "name", "chunk_text"}
    errors: list[str] = []
    warnings: list[str] = []

    for i, item in enumerate(data):
        for key in required:
            if key not in item or not item[key]:
                errors.append(f"persons[{i}]: missing required field '{key}'")

        if not isinstance(item.get("person_id"), int):
            errors.append(f"persons[{i}]: person_id must be int")

        if not isinstance(item.get("career", []), list):
            errors.append(f"persons[{i}]: career must be list")

        if not isinstance(item.get("works_raw", []), list):
            errors.append(f"persons[{i}]: works_raw must be list")

        # 检查 works_raw 内部格式
        for j, work in enumerate(item.get("works_raw", [])):
            if not isinstance(work.get("subject_id"), int):
                errors.append(f"persons[{i}].works_raw[{j}]: subject_id must be int")
            if not work.get("subject_name"):
                warnings.append(f"persons[{i}].works_raw[{j}]: empty subject_name")

    return {"valid": len(errors) == 0, "errors": errors, "warnings": warnings, "count": len(data)}


# ═══════════════════════════════════════════════════════════════════════
# Round-trip 验证
# ═══════════════════════════════════════════════════════════════════════


def verify_round_trip(entity_type: str, expected_ids: list[str]) -> dict:
    """验证写入后的数据完整性。

    Args:
        entity_type: "subject" | "character" | "person"
        expected_ids: 预期的 entity IDs (带前缀), e.g. ["character_1", "character_4"]

    Returns:
        验证结果 dict。
    """
    from database.engine import engine
    from sqlalchemy import text

    results: dict = {"total": len(expected_ids), "found": 0, "missing": [], "issues": []}

    with engine.connect() as conn:
        for eid in expected_ids:
            result = conn.execute(
                text("SELECT id, entity_type, name, name_cn, nsfw, chunk_text, meta_info, embedding FROM rag_entities WHERE id = :id"),
                {"id": eid},
            )
            row = result.first()

            if row is None:
                results["missing"].append(eid)
                continue

            results["found"] += 1

            # 检查各字段
            entity_id, et, name, name_cn, nsfw, chunk_text, meta_info, embedding = row

            if et != entity_type:
                results["issues"].append(f"{eid}: entity_type mismatch (expected {entity_type}, got {et})")

            if not name:
                results["issues"].append(f"{eid}: name is empty")

            if not chunk_text:
                results["issues"].append(f"{eid}: chunk_text is empty")

            if meta_info is None or not meta_info:
                results["issues"].append(f"{eid}: meta_info is empty")

            if embedding is None:
                results["issues"].append(f"{eid}: embedding is null")

    return results


# ═══════════════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════════════


def run_ingestion_test(
    data_dir: str = "eval/data/processed",
    entity_types: list[str] | None = None,
    dry_run: bool = False,
    limit: int | None = None,
) -> dict:
    """运行 ingestion 管线测试。

    Args:
        data_dir: 处理后数据的目录。
        entity_types: 要测试的类型列表。None = 全部。
        dry_run: True = 只校验格式，不实际写入。
        limit: 每种类型最多写入几条。None = 全部。
    """
    base = Path(data_dir)
    all_results: dict[str, dict] = {}

    entity_types = entity_types or ["character", "person"]

    for et in entity_types:
        logger.info("══ %s ingestion 测试 ══", et)

        # 1. 加载数据
        data_file = base / f"{et}s.json"
        if not data_file.exists():
            logger.warning("数据文件不存在: %s", data_file)
            continue

        data: list[dict] = json.loads(data_file.read_text(encoding="utf-8"))
        logger.info("加载 %d 条 %s 数据", len(data), et)

        if limit:
            data = data[:limit]
            logger.info("  截断至 %d 条", limit)

        # 2. 格式校验
        validators = {
            "subject": validate_subjects,
            "character": validate_characters,
            "person": validate_persons,
        }
        validation = validators[et](data)
        all_results[f"{et}_validation"] = validation

        if validation["errors"]:
            logger.error("格式校验失败:")
            for err in validation["errors"]:
                logger.error("  ✗ %s", err)
            continue

        if validation["warnings"]:
            for w in validation["warnings"]:
                logger.warning("  ⚠ %s", w)

        logger.info("格式校验: ✓ (%d 条)", validation["count"])

        if dry_run:
            logger.info("dry-run 模式，跳过实际写入")
            continue

        # 3. 截断过长文本（embedding API 有 token 上限）
        MAX_CHUNK_CHARS = 2000
        for item in data:
            if len(item.get("chunk_text", "")) > MAX_CHUNK_CHARS:
                item["chunk_text"] = item["chunk_text"][:MAX_CHUNK_CHARS] + "..."
                logger.debug("  截断 %s_%d chunk_text: %d → %d",
                            et, item.get(f"{et}_id"), len(item.get("chunk_text", "")), MAX_CHUNK_CHARS)

        # 4. Ingestion — 小 batch 避免 embedding API 超限
        BATCH_SIZE = 5
        from core.config import get_settings
        from database.engine import engine
        from rag.ingestion import RagEntityIngestor

        settings = get_settings()
        ingestor = RagEntityIngestor(
            engine=engine,
            zhipu_api_key=settings.ZHIPU_API_KEY,
            zhipu_base_url=settings.ZHIPU_BASE_URL,
        )

        total_inserted = 0
        try:
            for start in range(0, len(data), BATCH_SIZE):
                batch = data[start:start + BATCH_SIZE]
                if et == "subject":
                    n = ingestor.ingest_subjects(batch)
                elif et == "character":
                    n = ingestor.ingest_characters(batch)
                elif et == "person":
                    n = ingestor.ingest_persons(batch)
                else:
                    raise ValueError(f"Unknown entity type: {et}")
                total_inserted += n
                logger.info("  batch %d-%d: %d 条", start + 1, start + len(batch), n)

            logger.info("写入: %d 条", total_inserted)

        except Exception as e:
            logger.error("ingestion 失败: %s", e)
            all_results[f"{et}_ingestion"] = {"error": str(e), "inserted": total_inserted}
            continue

        # 4. Round-trip 验证
        prefix = f"{et}_"
        expected_ids = [f"{prefix}{item[f'{et}_id']}" for item in data]
        verify_result = verify_round_trip(et, expected_ids)
        all_results[f"{et}_round_trip"] = verify_result

        logger.info("Round-trip: %d/%d 找到", verify_result["found"], verify_result["total"])
        if verify_result["missing"]:
            logger.error("  缺失: %s", verify_result["missing"])
        if verify_result["issues"]:
            for issue in verify_result["issues"]:
                logger.warning("  ⚠ %s", issue)
        if not verify_result["missing"] and not verify_result["issues"]:
            logger.info("  ✓ 全部通过")

    return all_results


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(description="Phase 1.4 Ingestion 管线测试")
    parser.add_argument("--type", default="all", choices=["all", "subject", "character", "person"])
    parser.add_argument("--dry-run", action="store_true", help="仅校验数据格式，不实际写入")
    parser.add_argument("--limit", type=int, default=None, help="每种类型最多写入 N 条（方便快速测试）")
    args = parser.parse_args()

    entity_types = {
        "all": ["character", "person"],  # subjects 已有 61 条在 DB，默认跳过
        "subject": ["subject"],
        "character": ["character"],
        "person": ["person"],
    }[args.type]

    results = run_ingestion_test(
        data_dir="eval/data/processed",
        entity_types=entity_types,
        dry_run=args.dry_run,
        limit=args.limit,
    )

    # 汇总
    print("\n" + "=" * 60)
    print("  Ingestion 管线测试结果")
    print("=" * 60)

    for key, result in results.items():
        if key.endswith("_validation"):
            status = "✓" if result["valid"] else "✗"
            print(f"  [{status}] {key}: {result['count']} 条, {len(result['errors'])} 错误, {len(result['warnings'])} 警告")
        elif key.endswith("_round_trip"):
            ok = result["found"] == result["total"] and not result["issues"]
            status = "✓" if ok else "✗"
            print(f"  [{status}] {key}: {result['found']}/{result['total']} 找到, {len(result['issues'])} 问题")
        elif key.endswith("_ingestion"):
            has_error = "error" in result
            status = "✗" if has_error else "✓"
            print(f"  [{status}] {key}: {result.get('error', 'OK')}")

    if args.dry_run:
        print("\n  (dry-run 模式，未实际写入数据库)")

    print()


if __name__ == "__main__":
    main()
