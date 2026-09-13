"""
Phase 1 数据收集脚本

通过 BangumiClient + rag.enricher 获取 subject/character/person 的真实数据，
保存为 ingestion-ready 的结构化 JSON。

用法::

    python -m rag.cli.collect                    # 全部 3 种类型
    python -m rag.cli.collect --type character   # 仅角色
    python -m rag.cli.collect --limit 10         # 每种各 10 个（默认 20）

产出:
    rag/corpus/processed/    — enricher 输出的 ingestion-ready 数据（verify / ingest 消费）
    rag/corpus/raw/          — 历史遗留目录：旧版 collect 的原始响应存档。
                               当前没有任何代码写它或读它（subject 分支改为走
                               SubjectCollector 后就不再落盘了），留着仅供人工比对。

三种类型都经由各自 enricher 产出同一形状：键名带前缀（subject_id /
character_id / person_id），正文放 summary_text。verify.py 的校验器按这个形状查。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(message)s",
)
logger = logging.getLogger("eval.collect")


# ═══════════════════════════════════════════════════════════════════════
# 搜索词库 — 多样化的角色/人物名，覆盖不同年代和类型
# ═══════════════════════════════════════════════════════════════════════

_CHARACTER_SEARCH_TERMS: list[str] = [
    "綾波レイ", "惣流・アスカ・ラングレー", "草薙素子",
    "スパイク・スピーゲル", "綾崎ハヤテ",
    "セイバー", "ルルーシュ・ランペルージ", "キョン",
    "涼宮ハルヒ", "エドワード・エルリック", "岡部倫太郎",
    "暁美ほむら", "千反田える", "折木奉太郎",
    "エレン・イェーガー", "リヴァイ", "レム",
    "ヴァイオレット・エヴァーガーデン", "竈門炭治郎",
    "ヴィクトリカ", "戦場ヶ原ひたぎ", "キノ",
    "クラフト・ロレンス", "ホロ",
    "lain", "シエル・ファントムハイヴ",
    "セルティ・ストゥルルソン", "銀古",
]

_PERSON_SEARCH_TERMS: list[str] = [
    "花澤香菜", "杉田智和", "釘宮理恵", "神谷浩史",
    "悠木碧", "宮野真守", "沢城みゆき", "中村悠一",
    "早見沙織", "石田彰", "林原めぐみ", "坂本真綾",
    "新房昭之", "今敏", "押井守", "庵野秀明",
    "渡辺信一郎", "湯浅政明", "細田守", "新海誠",
    "虚淵玄", "奈須きのこ", "榎戸洋司",
    "梶浦由記", "菅野よう子", "澤野弘之",
    "富樫義博", "荒木飛呂彦",
]

_SUBJECT_IDS_FOR_CHARACTERS: list[int] = [
    253, 265, 1, 876, 1887, 971, 266,
]


# ═══════════════════════════════════════════════════════════════════════
# Subject 收集（ID 发现走 trending/calendar，数据走 SubjectCollector 富化）
# ═══════════════════════════════════════════════════════════════════════


async def collect_subjects(client, limit: int) -> list[dict]:
    """收集 Subject 数据——trending/calendar 发现 ID → SubjectCollector 富化。

    产出形状必须是 ``ingest_subjects()`` 消费的那一种。曾经这里直接返回
    ``sanitize_subject_detail(raw)``（键是 ``id``/``summary``/``type``，API 层形状），
    而 ingest 要的是 ``subject_id``/``summary_text``/``platform``（入库层形状）——
    两者之间差着 SubjectCollector 做的富化（summary+info 合成 chunk_text、
    collection 的中文键还原成数字键、year 从 date 里切）。
    直接灌 API 形状 = verify 全红 + 入库字段大面积丢失。
    """
    from rag.enricher import SubjectCollector

    logger.info("── 收集 Subject ──")

    subject_ids: set[int] = set()

    # trending
    try:
        trending = await client._get("/p1/trending/subjects", params={"limit": min(limit, 30)})
        if "_error" not in trending:
            for entry in (trending.get("data", []) or []):
                subj = entry.get("subject", {}) or {}
                sid = subj.get("id", 0)
                if sid:
                    subject_ids.add(sid)
            logger.info("trending: %d ids", len(subject_ids))
    except Exception as e:
        logger.warning("trending 失败: %s", e)

    # calendar
    try:
        cal_raw = await client._get("/p1/calendar")
        if "_error" not in cal_raw:
            today = datetime.now().isoweekday()
            for day_key in [str(today), today]:
                day_data = cal_raw.get(day_key, []) or []
                for entry in day_data:
                    subj = entry.get("subject", {}) or {}
                    sid = subj.get("id", 0)
                    if sid:
                        subject_ids.add(sid)
            logger.info("calendar: %d ids total", len(subject_ids))
    except Exception as e:
        logger.warning("calendar 失败: %s", e)

    # sorted 再截断：set 的迭代顺序不稳定，取"前 limit 个"会变成每次跑都不一样
    target_ids = sorted(subject_ids)[:limit]

    enricher = SubjectCollector(client)
    results = await enricher.collect_batch(target_ids)
    valid = [r for r in results if "_error" not in r]
    failed = len(results) - len(valid)
    if failed:
        logger.warning("%d 条富化失败", failed)

    logger.info("Subject: 成功 %d/%d", len(valid), len(target_ids))
    return valid


# ═══════════════════════════════════════════════════════════════════════
# Character / Person 收集（通过 enricher 一步获取 ingestion-ready 数据）
# ═══════════════════════════════════════════════════════════════════════


async def collect_characters(client, limit: int) -> list[dict]:
    """收集 Character 数据——搜索发现 ID → enricher 富化。"""
    from rag.enricher import CharacterEnricher

    logger.info("── 收集 Character ──")

    character_ids: set[int] = set()

    # 搜索
    for term in _CHARACTER_SEARCH_TERMS:
        if len(character_ids) >= limit:
            break
        try:
            raw = await client._post("/p1/search/characters", json={"keyword": term, "limit": 3})
            await asyncio.sleep(0.15)
            if "_error" in raw:
                continue
            data = raw if isinstance(raw, list) else (raw.get("results") or raw.get("data") or [])
            for item in data[:2]:
                cid = item.get("id", 0)
                if cid:
                    character_ids.add(cid)
        except Exception as e:
            logger.warning("搜索角色 '%s' 失败: %s", term, e)

    logger.info("搜索获得 %d 个角色 ID", len(character_ids))

    # 从已知 subject 补角色
    for sid in _SUBJECT_IDS_FOR_CHARACTERS[:5]:
        if len(character_ids) >= limit * 2:
            break
        try:
            raw = await client._get(f"/p1/subjects/{sid}/characters")
            await asyncio.sleep(0.15)
            if "_error" in raw:
                continue
            data = raw if isinstance(raw, list) else (raw.get("data") or [])
            for item in data[:8]:
                ch = item.get("character") or {}
                cid = ch.get("id", 0)
                if cid:
                    character_ids.add(cid)
        except Exception as e:
            logger.warning("subject %d characters 失败: %s", sid, e)

    logger.info("总共 %d 个候选角色 ID", len(character_ids))
    target_ids = sorted(character_ids)[:limit]

    # enricher 一步获取 ingestion-ready 数据
    enricher = CharacterEnricher(client)
    results = await enricher.enrich_batch(target_ids)
    valid = [r for r in results if "_error" not in r]
    failed = len(results) - len(valid)
    if failed:
        logger.warning("%d 条富化失败", failed)

    logger.info("Character: 成功 %d/%d", len(valid), len(target_ids))
    return valid


async def collect_persons(client, limit: int) -> list[dict]:
    """收集 Person 数据——搜索发现 ID → enricher 富化。"""
    from rag.enricher import PersonEnricher

    logger.info("── 收集 Person ──")

    person_ids: set[int] = set()

    for term in _PERSON_SEARCH_TERMS:
        if len(person_ids) >= limit:
            break
        try:
            raw = await client._post("/p1/search/persons", json={"keyword": term, "limit": 3})
            await asyncio.sleep(0.15)
            if "_error" in raw:
                continue
            data = raw if isinstance(raw, list) else (raw.get("results") or raw.get("data") or [])
            for item in data[:1]:
                pid = item.get("id", 0)
                if pid:
                    person_ids.add(pid)
        except Exception as e:
            logger.warning("搜索人物 '%s' 失败: %s", term, e)

    logger.info("搜索获得 %d 个人物 ID", len(person_ids))
    target_ids = sorted(person_ids)[:limit]

    enricher = PersonEnricher(client)
    results = await enricher.enrich_batch(target_ids)
    valid = [r for r in results if "_error" not in r]
    failed = len(results) - len(valid)
    if failed:
        logger.warning("%d 条富化失败", failed)

    logger.info("Person: 成功 %d/%d", len(valid), len(target_ids))
    return valid


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════


async def main():
    parser = argparse.ArgumentParser(description="Phase 1 数据收集")
    parser.add_argument("--type", default="all", choices=["all", "subject", "character", "person"])
    parser.add_argument("--limit", type=int, default=20, help="每种类型的数量上限")
    args = parser.parse_args()

    from clients.client import BangumiClient
    from core.config import get_settings

    settings = get_settings()
    client = BangumiClient(access_token=settings.BANGUMI_ACCESS_TOKEN or None)

    base_dir = Path(__file__).resolve().parent.parent / "corpus"
    # raw/ 不再创建：三种类型现在都只产出 processed/（见模块 docstring）
    processed_dir = base_dir / "processed"
    processed_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if args.type in ("all", "subject"):
        subjects = await collect_subjects(client, args.limit)
        (processed_dir / "subjects.json").write_text(
            json.dumps(subjects, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("保存 %d 个 Subject 到 subjects.json", len(subjects))

    if args.type in ("all", "character"):
        characters = await collect_characters(client, args.limit)
        (processed_dir / "characters.json").write_text(
            json.dumps(characters, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("保存 %d 个 Character 到 characters.json", len(characters))

    if args.type in ("all", "person"):
        persons = await collect_persons(client, args.limit)
        (processed_dir / "persons.json").write_text(
            json.dumps(persons, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("保存 %d 个 Person 到 persons.json", len(persons))

    # 摘要
    summary = {
        "collected_at": timestamp,
        "method": "Bangumi p1 API via BangumiClient + rag.enricher",
    }
    (base_dir / "collection_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    logger.info("\n══ 数据收集完成 ══")

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
