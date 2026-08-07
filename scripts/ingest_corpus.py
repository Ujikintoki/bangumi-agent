"""
RAG 语料库批量灌入脚本 — Phase 2

从 Phase 1 发现的 ID 列表（``scripts/data/*.json``）读取实体 ID，
经 Bangumi p1 API 富化后向量化灌入 pgvector。

两阶段协作::

    python scripts/discover_corpus.py     # Phase 1: ID 发现
    python scripts/ingest_corpus.py       # Phase 2: 富化 + 灌入

用法::

    source .venv/bin/activate
    python scripts/ingest_corpus.py              # 全部流程
    python scripts/ingest_corpus.py --clear      # 清空 rag_entities 后重新灌入
    python scripts/ingest_corpus.py --subjects-only

数据流::

    scripts/data/subject_ids_type2.json  ─┐  head 800 + tail 100
    scripts/data/subject_ids_type1.json  ─┤  head 100 + tail 30
    scripts/data/character_ids.json      ─┼─ head 300 + tail 50
    scripts/data/person_ids.json         ─┘  head 100 + tail 20
                                               │
                                               ▼
                                        SubjectCollector / CharacterEnricher
                                        / PersonEnricher
                                               │
                                               ▼
                                        RagEntityIngestor
                                               │
                                               ▼
                                        PostgreSQL + pgvector
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(message)s",
)
logger = logging.getLogger("ingest_corpus")

DATA_DIR = Path(__file__).resolve().parent / "data"

# ═══════════════════════════════════════════════════════════════════════
# 头部/尾部采样配置
# ═══════════════════════════════════════════════════════════════════════

# 头部：按收藏数排名确定性选取 Top-N
HEAD_SIZES: dict[str, int] = {
    "subject_type2": 800,
    "subject_type1": 100,
    "character": 300,
    "person": 100,
}

# 短尾随机：从排名截止线后的剩余 ID 中随机采样
TAIL_DISTRIBUTION: dict[str, int] = {
    "subject_type2": 100,
    "subject_type1": 30,
    "character": 50,
    "person": 20,
}

TAIL_SEED = 42  # 固定随机种子，保证可复现

# ═══════════════════════════════════════════════════════════════════════
# Phase 1 产物 → ID 列表加载
# ═══════════════════════════════════════════════════════════════════════

# 映射: CLI flag → JSON 文件名 + entity_type 标签
_DISCOVERY_FILES: dict[str, dict] = {
    "subject_type2": {
        "file": "subject_ids_type2.json",
        "label": "Subject (anime)",
        "entity_type": "subject",
    },
    "subject_type1": {
        "file": "subject_ids_type1.json",
        "label": "Subject (book)",
        "entity_type": "subject",
    },
    "character": {
        "file": "character_ids.json",
        "label": "Character",
        "entity_type": "character",
    },
    "person": {
        "file": "person_ids.json",
        "label": "Person",
        "entity_type": "person",
    },
}


def _load_ids(key: str) -> list[int]:
    """从 Phase 1 JSON 产物中加载实体 ID 列表。"""
    filepath = DATA_DIR / _DISCOVERY_FILES[key]["file"]
    if not filepath.exists():
        logger.error("%s 不存在，请先运行 scripts/discover_corpus.py", filepath)
        return []

    with open(filepath, encoding="utf-8") as f:
        data = json.load(f)

    ids = [item["id"] for item in data if isinstance(item.get("id"), int)]
    logger.info("加载 %s: %d 个 ID (%s)", _DISCOVERY_FILES[key]["label"], len(ids), filepath.name)
    return ids


# ═══════════════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════════════


async def main():
    parser = argparse.ArgumentParser(
        description="RAG 语料库批量灌入 — Phase 2"
    )
    parser.add_argument("--clear", action="store_true", help="清空 rag_entities 后重新灌入")
    parser.add_argument("--subjects-only", action="store_true")
    parser.add_argument("--characters-only", action="store_true")
    parser.add_argument("--persons-only", action="store_true")
    args = parser.parse_args()

    from clients.client import BangumiClient
    from core.config import get_settings

    settings = get_settings()
    client = BangumiClient(access_token=settings.BANGUMI_ACCESS_TOKEN or None)

    run_all = not (args.subjects_only or args.characters_only or args.persons_only)

    # ═══════════════════════════════════════════════════════════════════
    # Phase 1: 加载 ID 列表 + 头部/尾部拆分
    # ═══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 60)
    print("  Phase 1: 加载 ID 列表 + 头部/尾部拆分")
    print("=" * 60)

    random.seed(TAIL_SEED)

    # 分别加载各来源的完整 ID 列表（保持排名顺序）
    all_ids: dict[str, list[int]] = {}
    for key in _DISCOVERY_FILES:
        all_ids[key] = _load_ids(key)

    # 拆分头部（确定性 Top-N）和尾部（随机采样）
    final_ids: dict[str, list[int]] = {}
    tail_summary: list[str] = []

    for key, ids in all_ids.items():
        head_size = HEAD_SIZES.get(key, 0)
        tail_count = TAIL_DISTRIBUTION.get(key, 0)

        head = ids[:head_size]
        tail_pool = ids[head_size:]

        if tail_pool and tail_count > 0:
            actual_tail = min(tail_count, len(tail_pool))
            tail = random.sample(tail_pool, actual_tail)
        else:
            tail = []

        final_ids[key] = head + tail
        label = _DISCOVERY_FILES[key]["label"]
        tail_summary.append(
            f"  {label:20s}: head={len(head):>4d} + tail={len(tail):>3d} = {len(final_ids[key]):>4d}"
        )
        logger.info(
            "%s: head=%d + tail=%d/%d → %d total",
            key, len(head), len(tail), len(tail_pool), len(final_ids[key]),
        )

    print()
    for line in tail_summary:
        print(line)

    subject_ids = final_ids["subject_type2"] + final_ids["subject_type1"]
    character_ids = final_ids["character"]
    person_ids = final_ids["person"]

    total_ids = len(subject_ids) + len(character_ids) + len(person_ids)
    print(f"  {'─' * 45}")
    print(f"  总计: {total_ids} (subject={len(subject_ids)}, character={len(character_ids)}, person={len(person_ids)})")

    # ═══════════════════════════════════════════════════════════════════
    # Phase 2: 清空旧数据（可选）
    # ═══════════════════════════════════════════════════════════════════
    if args.clear:
        print("\n" + "=" * 60)
        print("  Phase 2: 清空旧数据")
        print("=" * 60)
        from database.engine import engine
        from sqlmodel import Session, text
        with Session(engine) as session:
            result = session.exec(text("DELETE FROM rag_entities"))
            session.commit()
            count = getattr(result, 'rowcount', None)
            logger.info("已清空 rag_entities (删除 %s 行)", count if count is not None else "?")

    # ═══════════════════════════════════════════════════════════════════
    # Phase 3: 富化
    # ═══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 60)
    print("  Phase 3: API 富化")
    print("=" * 60)

    subjects_data: list[dict] = []
    characters_data: list[dict] = []
    persons_data: list[dict] = []

    if subject_ids:
        from rag.enricher import SubjectCollector
        logger.info("── Subject (%d IDs) ──", len(subject_ids))
        collector = SubjectCollector(client)
        raw = await collector.collect_batch(sorted(subject_ids))
        valid = [r for r in raw if "_error" not in r]
        subjects_data = valid
        logger.info("  富化: %d/%d 成功", len(valid), len(raw))

    if character_ids:
        from rag.enricher import CharacterEnricher
        logger.info("── Character (%d IDs) ──", len(character_ids))
        enricher = CharacterEnricher(client)
        raw = await enricher.enrich_batch(sorted(character_ids))
        valid = [r for r in raw if "_error" not in r]
        characters_data = valid
        logger.info("  富化: %d/%d 成功", len(valid), len(raw))

    if person_ids:
        from rag.enricher import PersonEnricher
        logger.info("── Person (%d IDs) ──", len(person_ids))
        enricher = PersonEnricher(client)
        raw = await enricher.enrich_batch(sorted(person_ids))
        valid = [r for r in raw if "_error" not in r]
        persons_data = valid
        logger.info("  富化: %d/%d 成功", len(valid), len(raw))

    # ═══════════════════════════════════════════════════════════════════
    # Phase 4: Embedding + 灌入
    # ═══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 60)
    print("  Phase 4: Embedding + 灌入")
    print("=" * 60)

    from database.engine import engine
    from rag.ingestion import RagEntityIngestor

    ingestor = RagEntityIngestor(
        engine=engine,
        zhipu_api_key=settings.ZHIPU_API_KEY,
        zhipu_base_url=settings.ZHIPU_BASE_URL,
    )

    n_subject = n_character = n_person = 0

    if subjects_data:
        logger.info("灌入 %d 条 subject...", len(subjects_data))
        n_subject = ingestor.ingest_subjects(subjects_data)
        logger.info("  ✓ %d subject", n_subject)

    if characters_data:
        logger.info("灌入 %d 条 character...", len(characters_data))
        n_character = ingestor.ingest_characters(characters_data)
        logger.info("  ✓ %d character", n_character)

    if persons_data:
        logger.info("灌入 %d 条 person...", len(persons_data))
        n_person = ingestor.ingest_persons(persons_data)
        logger.info("  ✓ %d person", n_person)

    # ═══════════════════════════════════════════════════════════════════
    # 总结
    # ═══════════════════════════════════════════════════════════════════
    print("\n" + "=" * 60)
    print("  灌入完成")
    print("=" * 60)
    print(f"  Subject:   {n_subject}")
    print(f"  Character: {n_character}")
    print(f"  Person:    {n_person}")
    print(f"  ─────────────────")
    print(f"  总计:      {n_subject + n_character + n_person}")
    print()

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
