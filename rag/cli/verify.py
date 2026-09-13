"""
Phase 1.4 Ingestion 管线测试 + Round-trip 验证

加载 Phase 1.3 收集的真实数据，通过 RagEntityIngestor 写入数据库，
验证写入后数据的完整性。

用法::

    python -m rag.cli.verify                    # 全部类型
    python -m rag.cli.verify --type character   # 仅角色
    python -m rag.cli.verify --dry-run          # 仅验证数据格式，不实际写入
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

# 语料目录从模块位置推导，不写字面相对路径 —— 字面路径在目录调整时会静默失效
_CORPUS_PROCESSED_DIR = Path(__file__).resolve().parent.parent / "corpus" / "processed"

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(message)s",
)
logger = logging.getLogger("eval.ingestion_test")


# ═══════════════════════════════════════════════════════════════════════
# 字段清单 —— 以 rag/ingestion.py 的 ingest_*() 实际读到的键为准
# ═══════════════════════════════════════════════════════════════════════
#
# `required`：缺了就会炸或明显丢数据（ingest 里是 item["xxx"] 硬下标，或正文本身）
# `known`：ingest 会读或至少不会丢的键。落在 known 之外的键会被 ingest 静默忽略，
#          所以那是一个"数据在入库路上消失"的信号，而不是错误。
#
# ⚠️ 正文键叫 summary_text，不叫 chunk_text。chunk_text 是已废弃表 bangumi_chunks
#    的历史列名，2026-08 enricher 统一改名成 summary_text 之后，这里没跟着改，
#    导致 --dry-run 把每一条都判成"缺 chunk_text"（明明数据是全的）。
#    改动 enricher 输出键名时，这三组清单要同步改。

_SUBJECT_REQUIRED = {"subject_id", "name", "summary_text"}
_SUBJECT_KNOWN = _SUBJECT_REQUIRED | {
    "name_cn", "info", "score", "rating_total", "rank", "rating_count",
    "collection", "date", "year", "platform", "eps", "volumes",
    "series", "series_entry", "nsfw", "infobox", "tags",
}

_CHARACTER_REQUIRED = {"character_id", "name", "summary_text"}
_CHARACTER_KNOWN = _CHARACTER_REQUIRED | {
    "name_cn", "subject_name", "role", "collects", "comment",
    "summary", "info", "infobox", "casts_raw", "nsfw",
}

_PERSON_REQUIRED = {"person_id", "name", "summary_text"}
_PERSON_KNOWN = _PERSON_REQUIRED | {
    "name_cn", "career", "type", "collects", "comment",
    "summary", "info", "infobox", "works_raw", "nsfw",
}


def _unknown_field_warnings(label: str, data: list[dict], known: set[str]) -> list[str]:
    """找出 enricher 吐了、但 ingest 不认识的键。

    按"键"聚合而不是按"条"报：一个字段要是系统性多出来，1030 条会刷 1030 行
    警告，把真正要看的那一行淹掉。

    只警告不报错 —— 多字段本身不失败（可能是 enricher 新增了字段而这里没跟上），
    但它是"字段悄悄丢了"的唯一信号，所以不能一句话都不说。
    """
    counts: dict[str, int] = {}
    for item in data:
        for key in set(item) - known:
            counts[key] = counts.get(key, 0) + 1
    return [
        f"{label}: 未知字段 '{key}' 出现在 {n}/{len(data)} 条中（ingest 不认识，会被忽略）"
        for key, n in sorted(counts.items())
    ]


# ═══════════════════════════════════════════════════════════════════════
# 数据格式校验
# ═══════════════════════════════════════════════════════════════════════


def validate_subjects(data: list[dict]) -> dict:
    """校验 subject 数据格式是否匹配 ingest_subjects() 期望。"""
    errors: list[str] = []
    warnings: list[str] = []

    for i, item in enumerate(data):
        for key in _SUBJECT_REQUIRED:
            if not item.get(key):
                errors.append(f"subjects[{i}]: missing required field '{key}'")

        # 用 item.get() 而不是 item['subject_id']：上面那个循环刚说过这个键可能不存在，
        # 这里再硬下标一次，就会把"缺字段"这句能读懂的报错换成质检员自己 KeyError 崩掉
        if not isinstance(item.get("subject_id"), int):
            errors.append(
                f"subjects[{i}]: subject_id must be int, "
                f"got {type(item.get('subject_id')).__name__}"
            )

        if not isinstance(item.get("tags", []), list):
            errors.append(f"subjects[{i}]: tags must be list")

    warnings += _unknown_field_warnings("subjects", data, _SUBJECT_KNOWN)
    return {"valid": len(errors) == 0, "errors": errors, "warnings": warnings, "count": len(data)}


def validate_characters(data: list[dict]) -> dict:
    """校验 character 数据格式。"""
    errors: list[str] = []
    warnings: list[str] = []

    for i, item in enumerate(data):
        for key in _CHARACTER_REQUIRED:
            if not item.get(key):
                errors.append(f"characters[{i}]: missing required field '{key}'")

        if not isinstance(item.get("character_id"), int):
            errors.append(
                f"characters[{i}]: character_id must be int, "
                f"got {type(item.get('character_id')).__name__}"
            )

        if not isinstance(item.get("casts_raw", []), list):
            errors.append(f"characters[{i}]: casts_raw must be list")

        # 检查 casts_raw 内部格式
        for j, cast in enumerate(item.get("casts_raw", [])):
            if not isinstance(cast.get("subject_id"), int):
                errors.append(f"characters[{i}].casts_raw[{j}]: subject_id must be int, got {type(cast.get('subject_id')).__name__}")
            if not cast.get("subject_name"):
                warnings.append(f"characters[{i}].casts_raw[{j}]: empty subject_name")

    warnings += _unknown_field_warnings("characters", data, _CHARACTER_KNOWN)
    return {"valid": len(errors) == 0, "errors": errors, "warnings": warnings, "count": len(data)}


def validate_persons(data: list[dict]) -> dict:
    """校验 person 数据格式。"""
    errors: list[str] = []
    warnings: list[str] = []

    for i, item in enumerate(data):
        for key in _PERSON_REQUIRED:
            if not item.get(key):
                errors.append(f"persons[{i}]: missing required field '{key}'")

        if not isinstance(item.get("person_id"), int):
            errors.append(
                f"persons[{i}]: person_id must be int, "
                f"got {type(item.get('person_id')).__name__}"
            )

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

    warnings += _unknown_field_warnings("persons", data, _PERSON_KNOWN)
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

    # 列名对着 database/rag_tables.py:RagEntity（单表多态）来。
    # 曾经这里查的是 entity_id + chunk_text —— 那是已废弃表 bangumi_chunks 的列，
    # rag_entities 里根本没有，所以非 dry-run 一跑到 round-trip 就 UndefinedColumn。
    # 正文的现存列是 embed_text（送 embedding 的文本）和 rag_output（检索时直接返回的 JSON）。
    with engine.connect() as conn:
        for eid in expected_ids:
            result = conn.execute(
                text("SELECT id, entity_type, name, name_cn, nsfw, rag_output, embed_text, meta_info, embedding FROM rag_entities WHERE id = :id"),
                {"id": eid},
            )
            row = result.first()

            if row is None:
                results["missing"].append(eid)
                continue

            results["found"] += 1

            # 检查各字段
            entity_id, et, name, name_cn, nsfw, rag_output, embed_text, meta_info, embedding = row

            if et != entity_type:
                results["issues"].append(f"{eid}: entity_type mismatch (expected {entity_type}, got {et})")

            if not name:
                results["issues"].append(f"{eid}: name is empty")

            if not embed_text:
                results["issues"].append(f"{eid}: embed_text is empty")

            if not rag_output:
                results["issues"].append(f"{eid}: rag_output is empty")

            if meta_info is None or not meta_info:
                results["issues"].append(f"{eid}: meta_info is empty")

            if embedding is None:
                results["issues"].append(f"{eid}: embedding is null")

    return results


# ═══════════════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════════════


def run_ingestion_test(
    data_dir: str | None = None,
    entity_types: list[str] | None = None,
    dry_run: bool = False,
    limit: int | None = None,
) -> dict:
    """运行 ingestion 管线测试。

    Args:
        data_dir: 处理后数据的目录。None = 默认 rag/corpus/processed。
        entity_types: 要测试的类型列表。None = 全部。
        dry_run: True = 只校验格式，不实际写入。
        limit: 每种类型最多写入几条。None = 全部。
    """
    base = Path(data_dir) if data_dir else _CORPUS_PROCESSED_DIR
    all_results: dict[str, dict] = {}

    # 默认三种全查（函数 docstring 一直是这么写的）。曾经默认只查 character+person，
    # 理由是"subjects 已有 61 条在 DB"—— 那个数字早就过期了，DB 里现在 1030 条 subject，
    # 而 subject 恰恰是唯一一个 collect 产出形状出错、最该被查的类型。
    entity_types = entity_types or ["subject", "character", "person"]

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

        # 警告必须先于错误打印：错误那块末尾会 continue，写在它后面就永远打不出来。
        # 曾经就是这个顺序 —— 数据越坏，"未知字段"这类最有信息量的警告越看不见
        # （汇总里数得到、正文里找不到），而那恰恰是唯一指向根因的一行。
        if validation["warnings"]:
            for w in validation["warnings"]:
                logger.warning("  ⚠ %s", w)

        if validation["errors"]:
            logger.error("格式校验失败:")
            for err in validation["errors"]:
                logger.error("  ✗ %s", err)
            continue

        logger.info("格式校验: ✓ (%d 条)", validation["count"])

        if dry_run:
            logger.info("dry-run 模式，跳过实际写入")
            continue

        # 3. 截断过长文本（embedding API 有 token 上限）
        #    截的是 summary_text —— 它既是 embed_text 的原料，也是 rag_output 的正文
        MAX_CHUNK_CHARS = 2000
        for item in data:
            text_len = len(item.get("summary_text", ""))
            if text_len > MAX_CHUNK_CHARS:
                # 先记长度再截断 —— 截完再取 len() 记的是"截到多少"，不是原来的多长
                item["summary_text"] = item["summary_text"][:MAX_CHUNK_CHARS] + "..."
                logger.debug("  截断 %s_%s summary_text: %d → %d",
                            et, item.get(f"{et}_id"), text_len, MAX_CHUNK_CHARS)

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
        "all": ["subject", "character", "person"],
        "subject": ["subject"],
        "character": ["character"],
        "person": ["person"],
    }[args.type]

    results = run_ingestion_test(
        data_dir=str(_CORPUS_PROCESSED_DIR),
        entity_types=entity_types,
        dry_run=args.dry_run,
        limit=args.limit,
    )

    # 汇总
    print("\n" + "=" * 60)
    print("  Ingestion 管线测试结果")
    print("=" * 60)

    failed: list[str] = []
    for key, result in results.items():
        if key.endswith("_validation"):
            ok = bool(result["valid"])
            mark = "✓" if ok else "✗"
            print(f"  [{mark}] {key}: {result['count']} 条, {len(result['errors'])} 错误, {len(result['warnings'])} 警告")
        elif key.endswith("_round_trip"):
            ok = result["found"] == result["total"] and not result["issues"]
            mark = "✓" if ok else "✗"
            print(f"  [{mark}] {key}: {result['found']}/{result['total']} 找到, {len(result['issues'])} 问题")
        elif key.endswith("_ingestion"):
            ok = "error" not in result
            mark = "✓" if ok else "✗"
            print(f"  [{mark}] {key}: {result.get('error', 'OK')}")
        else:
            ok = True
        if not ok:
            failed.append(key)

    if args.dry_run:
        print("\n  (dry-run 模式，未实际写入数据库)")

    print()

    # 退出码必须跟着结果走。曾经这里只 print()，于是全红也退 0 ——
    # `verify --dry-run && echo ok` 在任何情况下都会 echo ok，CI 会静默放行一份全红的报告。
    # （同一类缺陷在轴 2 的 `--check` 上出现过一次，方向相反：断言全绿却退 1。）
    if not results:
        logger.error(
            "没有可校验的数据：%s 下没找到任何 {subject,character,person}s.json",
            _CORPUS_PROCESSED_DIR,
        )
        sys.exit(1)
    if failed:
        logger.error("未通过: %s", "、".join(failed))
        sys.exit(1)


if __name__ == "__main__":
    main()
