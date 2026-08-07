"""
RAG 语料库 ID 发现脚本 — Phase 1

按 collection 收藏数排名，采集 Top-N 实体 ID 及基础元数据。
输出 JSON 文件供 ``ingest_corpus.py`` Phase 2 消费。

数据源:
  - Subject (anime/book): p1 API ``GET /p1/subjects?type=N&sort=collects&page=N``
  - Character: HTML 解析 ``https://bangumi.tv/character?orderby=collects&page=N``
  - Person:   HTML 解析 ``https://bangumi.tv/person?orderby=collects&page=N``

用法::

    source .venv/bin/activate
    python scripts/discover_corpus.py              # 全量采集
    python scripts/discover_corpus.py --dry-run    # 仅前 2 页试跑
    python scripts/discover_corpus.py --subjects-only  # 仅采集条目

输出::

    scripts/data/
    ├── subject_ids_type2.json   # 动画 Top 1000
    ├── subject_ids_type1.json   # 书籍 Top 200
    ├── character_ids.json       # 角色 Top 400
    └── person_ids.json          # 人物 Top 150

每条记录::

    {"id": 328609, "name": "ぼっち・ざ・ろっく！", "name_cn": "孤独摇滚！",
     "rank": 1, "rating_total": 40377, "score": 8.37}
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Optional

import httpx

# ═══════════════════════════════════════════════════════════════════════
# 项目路径
# ═══════════════════════════════════════════════════════════════════════

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(message)s",
)
logger = logging.getLogger("discover_corpus")

DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ═══════════════════════════════════════════════════════════════════════
# 目标规模
# ═══════════════════════════════════════════════════════════════════════

TARGETS: dict[str, dict] = {
    "subject_type2": {
        "total": 1207, "target": 1000, "per_page": 24,
        "file": "subject_ids_type2.json",
    },
    "subject_type1": {
        "total": 16320, "target": 200, "per_page": 24,
        "file": "subject_ids_type1.json",
    },
    "character": {
        "target": 400, "per_page": 20,
        "file": "character_ids.json",
    },
    "person": {
        "target": 150, "per_page": 20,
        "file": "person_ids.json",
    },
}

# ═══════════════════════════════════════════════════════════════════════
# HTTP 基础设施
# ═══════════════════════════════════════════════════════════════════════

P1_BASE = "https://next.bgm.tv"
BGM_BASE = "https://bangumi.tv"
USER_AGENT = "BGM-Agent-Dev/1.0 (RAG corpus ingestion; bangumi.tv contact: see GitHub)"

# 请求间隔（秒）
API_DELAY = 0.3       # p1 API
HTML_DELAY = 0.5      # 浏览器页面抓取

# 重试配置
MAX_RETRIES = 3


async def _fetch_json(
    client: httpx.AsyncClient,
    url: str,
    label: str = "",
) -> Optional[dict]:
    """GET JSON API，带指数退避重试。"""
    for attempt in range(MAX_RETRIES):
        try:
            resp = await client.get(url)
            if resp.status_code == 429:
                wait = 2 ** (attempt + 1)
                logger.warning("%s 429 限流，%ds 后重试", label, wait)
                await asyncio.sleep(wait)
                continue
            if resp.status_code in (502, 503) and attempt < MAX_RETRIES - 1:
                wait = 2 ** (attempt + 1)
                logger.warning("%s %d，%ds 后重试", label, resp.status_code, wait)
                await asyncio.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except (httpx.TimeoutException, httpx.HTTPStatusError) as exc:
            if attempt < MAX_RETRIES - 1:
                wait = 2 ** (attempt + 1)
                logger.warning("%s 失败 (%s)，%ds 后重试", label, exc, wait)
                await asyncio.sleep(wait)
                continue
            logger.error("%s 最终失败: %s", label, exc)
            return None
    return None


async def _fetch_html(
    client: httpx.AsyncClient,
    url: str,
    label: str = "",
) -> Optional[str]:
    """GET HTML 页面，带指数退避重试。"""
    for attempt in range(MAX_RETRIES):
        try:
            resp = await client.get(url)
            if resp.status_code == 429:
                wait = 2 ** (attempt + 1)
                logger.warning("%s 429 限流，%ds 后重试", label, wait)
                await asyncio.sleep(wait)
                continue
            if resp.status_code in (502, 503) and attempt < MAX_RETRIES - 1:
                wait = 2 ** (attempt + 1)
                logger.warning("%s %d，%ds 后重试", label, resp.status_code, wait)
                await asyncio.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.text
        except (httpx.TimeoutException, httpx.HTTPStatusError) as exc:
            if attempt < MAX_RETRIES - 1:
                wait = 2 ** (attempt + 1)
                logger.warning("%s 失败 (%s)，%ds 后重试", label, exc, wait)
                await asyncio.sleep(wait)
                continue
            logger.error("%s 最终失败: %s", label, exc)
            return None
    return None


# ═══════════════════════════════════════════════════════════════════════
# 断点续跑
# ═══════════════════════════════════════════════════════════════════════


def _load_existing(filepath: Path) -> list[dict]:
    """加载已有 JSON 数据，用于断点续跑判断。"""
    if not filepath.exists():
        return []
    try:
        with open(filepath, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return []


def _save_results(filepath: Path, results: list[dict]) -> None:
    """写入 JSON 文件。先写临时文件再 rename，保证原子性。"""
    tmp = filepath.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    os.replace(tmp, filepath)


# ═══════════════════════════════════════════════════════════════════════
# Subject 采集 — p1 API
# ═══════════════════════════════════════════════════════════════════════


async def collect_subjects(
    client: httpx.AsyncClient,
    subject_type: int,
    target: int,
    per_page: int,
    filepath: Path,
    dry_run: bool = False,
) -> list[dict]:
    """从 p1 API 分页采集 Subject ID 和基础元数据。"""
    type_name = {1: "book", 2: "anime"}.get(subject_type, str(subject_type))
    logger.info("── Subject type=%d (%s) — 目标 %d ──", subject_type, type_name, target)

    existing = _load_existing(filepath)
    if len(existing) >= target:
        logger.info("已有 %d 条 ≥ 目标 %d，跳过", len(existing), target)
        return existing[:target]

    # 计算从第几页继续
    start_page = len(existing) // per_page + 1
    results = list(existing)

    max_pages = (target - 1) // per_page + 1
    if dry_run:
        max_pages = min(max_pages, 2)

    for page in range(start_page, max_pages + 1):
        needed = target - len(results)
        if needed <= 0:
            break

        url = f"{P1_BASE}/p1/subjects?type={subject_type}&sort=collects&page={page}&limit={per_page}"
        label = f"subject type={subject_type} page={page}"
        data = await _fetch_json(client, url, label)

        if data is None:
            logger.error("%s 获取失败，停止采集", label)
            break

        items = data.get("data", [])
        if not items:
            logger.info("%s 返回 0 条，已达末尾", label)
            break

        api_total = data.get("total", 0)
        for i, item in enumerate(items):
            rank = (page - 1) * per_page + i + 1
            record = {
                "id": item.get("id"),
                "name": item.get("name", ""),
                "name_cn": item.get("nameCN", ""),
                "type": item.get("type", subject_type),
                "rank": rank,
                "rating_total": (item.get("rating") or {}).get("total", 0),
                "score": (item.get("rating") or {}).get("score", 0.0),
                "date": item.get("date", ""),
                "meta_tags": item.get("metaTags", []),
            }
            results.append(record)

        _save_results(filepath, results)
        logger.info(
            "  page=%d/%d: +%d 条, 累计 %d/%d (api_total=%s)",
            page, max_pages, len(items), len(results), target,
            api_total,
        )

        if len(results) >= target:
            break

        await asyncio.sleep(API_DELAY)

    # 截断到目标数量
    final = results[:target]
    if len(final) > len(existing):
        _save_results(filepath, final)
    logger.info("Subject type=%d 完成: %d 条", subject_type, len(final))
    return final


# ═══════════════════════════════════════════════════════════════════════
# Character / Person 采集 — HTML 解析
# ═══════════════════════════════════════════════════════════════════════


# 匹配 /character/12345 或 /person/67890 链接 + 名称
# 每页 HTML 中每个实体出现多次（图片、名称链接、"讨论"链接），需去重取唯一名称
_CHAR_LINK_RE = re.compile(r'/character/(\d+)"[^>]*>([^<]+)</a>')
_PERSON_LINK_RE = re.compile(r'/person/(\d+)"[^>]*>([^<]+)</a>')
_NON_NAME_LABELS = {"讨论", ""}


def _parse_browser_html(html: str, entity_type: str) -> list[dict]:
    """从浏览器页面 HTML 中提取实体 ID 和名称。"""
    pattern = _CHAR_LINK_RE if entity_type == "character" else _PERSON_LINK_RE
    matches = pattern.findall(html)

    seen: dict[str, str] = {}
    for eid, name in matches:
        name = name.strip()
        if name in _NON_NAME_LABELS:
            continue
        if eid not in seen:
            seen[eid] = name

    return [{"id": int(eid), "name": name} for eid, name in seen.items()]


async def collect_characters_or_persons(
    client: httpx.AsyncClient,
    entity_type: str,
    target: int,
    per_page: int,
    filepath: Path,
    dry_run: bool = False,
) -> list[dict]:
    """从浏览器页面分页采集 Character 或 Person ID 和名称。"""
    label_cn = "角色" if entity_type == "character" else "人物"
    logger.info("── %s (%s) — 目标 %d ──", label_cn, entity_type, target)

    existing = _load_existing(filepath)
    if len(existing) >= target:
        logger.info("已有 %d 条 ≥ 目标 %d，跳过", len(existing), target)
        return existing[:target]

    start_page = len(existing) // per_page + 1
    results = list(existing)

    max_pages = (target - 1) // per_page + 1
    if dry_run:
        max_pages = min(max_pages, 2)

    for page in range(start_page, max_pages + 1):
        needed = target - len(results)
        if needed <= 0:
            break

        url = f"{BGM_BASE}/{entity_type}?orderby=collects&page={page}"
        label = f"{entity_type} page={page}"
        html = await _fetch_html(client, url, label)

        if html is None:
            logger.error("%s 获取失败，停止采集", label)
            break

        items = _parse_browser_html(html, entity_type)
        if not items:
            logger.info("%s 解析到 0 条，已达末尾", label)
            break

        for i, item in enumerate(items):
            rank = (page - 1) * per_page + i + 1
            record = {
                "id": item["id"],
                "name": item["name"],
                "name_cn": "",
                "rank": rank,
            }
            results.append(record)

        _save_results(filepath, results)
        logger.info(
            "  page=%d/%d: +%d 条, 累计 %d/%d",
            page, max_pages, len(items), len(results), target,
        )

        if len(results) >= target:
            break

        await asyncio.sleep(HTML_DELAY)

    final = results[:target]
    if len(final) > len(existing):
        _save_results(filepath, final)
    logger.info("%s 完成: %d 条", label_cn, len(final))
    return final


# ═══════════════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════════════


async def main():
    parser = argparse.ArgumentParser(
        description="RAG 语料库 ID 发现 — Phase 1"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="仅试跑前 2 页，验证格式",
    )
    parser.add_argument(
        "--subjects-only", action="store_true",
        help="仅采集条目",
    )
    parser.add_argument(
        "--characters-only", action="store_true",
        help="仅采集角色",
    )
    parser.add_argument(
        "--persons-only", action="store_true",
        help="仅采集人物",
    )
    args = parser.parse_args()

    run_all = not (args.subjects_only or args.characters_only or args.persons_only)

    # ── 客户端 ──
    api_client = httpx.AsyncClient(
        base_url=P1_BASE,
        headers={"User-Agent": USER_AGENT},
        timeout=httpx.Timeout(30.0, connect=10.0),
    )
    html_client = httpx.AsyncClient(
        base_url=BGM_BASE,
        headers={"User-Agent": USER_AGENT},
        timeout=httpx.Timeout(30.0, connect=10.0),
    )

    try:
        print("\n" + "=" * 60)
        print("  Phase 1: RAG 语料库 ID 发现")
        print("=" * 60)
        if args.dry_run:
            print("  [DRY RUN — 仅采集前 2 页]")
        print()

        # ── Subject type=2 (动画) ──
        if run_all or args.subjects_only:
            t = TARGETS["subject_type2"]
            await collect_subjects(
                api_client,
                subject_type=2,
                target=t["target"],
                per_page=t["per_page"],
                filepath=DATA_DIR / t["file"],
                dry_run=args.dry_run,
            )

        # ── Subject type=1 (书籍) ──
        if run_all or args.subjects_only:
            t = TARGETS["subject_type1"]
            await collect_subjects(
                api_client,
                subject_type=1,
                target=t["target"],
                per_page=t["per_page"],
                filepath=DATA_DIR / t["file"],
                dry_run=args.dry_run,
            )

        # ── Character ──
        if run_all or args.characters_only:
            t = TARGETS["character"]
            await collect_characters_or_persons(
                html_client,
                entity_type="character",
                target=t["target"],
                per_page=t["per_page"],
                filepath=DATA_DIR / t["file"],
                dry_run=args.dry_run,
            )

        # ── Person ──
        if run_all or args.persons_only:
            t = TARGETS["person"]
            await collect_characters_or_persons(
                html_client,
                entity_type="person",
                target=t["target"],
                per_page=t["per_page"],
                filepath=DATA_DIR / t["file"],
                dry_run=args.dry_run,
            )

        # ── 总结 ──
        print("\n" + "=" * 60)
        print("  发现完成")
        print("=" * 60)
        for key, t in TARGETS.items():
            filepath = DATA_DIR / t["file"]
            count = len(_load_existing(filepath))
            status = "✓" if count >= t["target"] else f"({count}/{t['target']})"
            print(f"  {key:20s}: {status}  → {filepath.name}")
        print()

    finally:
        await api_client.aclose()
        await html_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
