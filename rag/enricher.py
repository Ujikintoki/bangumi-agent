"""
RAG 数据富化模块

职责：
  将 Bangumi p1 API 的原始响应富化为 ``ingestion.py`` 可直接消费的格式。
  p1 API 的 character/person detail 端点不返回 casts/works，需额外调用
  ``/characters/{id}/casts`` 和 ``/persons/{id}/works`` 补全关联边数据。

设计原则：
  - **异步优先**：全 async，httpx 并发 + Semaphore 限流。
  - **永不抛异常**：API 失败返回 ``{"_error": "..."}``，遵循项目约定。
  - **数据格式严格匹配**：输出 dict 的 key 和类型与 ``ingestion.py`` 的
    ``ingest_characters()`` / ``ingest_persons()`` 完全一致。

用法::

    from clients.client import BangumiClient
    from rag.enricher import CharacterEnricher, PersonEnricher

    client = BangumiClient()
    char_enricher = CharacterEnricher(client)

    data = await char_enricher.enrich(1)       # 单条
    # → {"character_id": 1, "name": "...", "chunk_text": "...", ...}

    batch = await char_enricher.enrich_batch([1, 4, 47])  # 批量
    # → [{"character_id": 1, ...}, {"character_id": 4, ...}, ...]

    # 直连 ingestion
    from rag.ingestion import RagEntityIngestor
    ingestor.ingest_characters(batch)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from clients.sanitizers import (
    _CHARACTER_INFOBOX_DROP_KEYS,
    _clean_infobox,
)

logger = logging.getLogger("bgm-agent.enricher")

# 并发控制
_DEFAULT_CONCURRENCY = 3
_DEFAULT_RATE_LIMIT = 0.2  # 秒


# ═══════════════════════════════════════════════════════════════════════
# 文本清洗 — 统一入口（委托到 rag._utils）
# ═══════════════════════════════════════════════════════════════════════

from ._utils import _clean_text  # noqa: F401 — 向后兼容 re-export


# ═══════════════════════════════════════════════════════════════════════
# chunk_text 构造 — 纯叙事文本，供 Agent 上下文消费
# ═══════════════════════════════════════════════════════════════════════


def _build_chunk_text(summary: str, info: str | None = None) -> str:
    """从 summary 构造清洗后的叙事文本。

    仅取摘要主体（summary），不拼接 info（info 存入 meta_info）。
    清洗 BBcode 和 HTML 残留，输出干净的纯文本。

    Args:
        summary: 实体摘要文本（来自 API ``summary`` 字段）。
        info: 已废弃——仅保留参数兼容性，不再拼入 chunk_text。

    Returns:
        清洗后的纯叙事文本。summary 为空时返回空字符串。
    """
    if not summary or not summary.strip():
        return ""

    text = _clean_text(summary)
    return text


# ═══════════════════════════════════════════════════════════════════════
# CharacterEnricher
# ═══════════════════════════════════════════════════════════════════════


class CharacterEnricher:
    """角色数据富化器。

    并发调用 ``GET /p1/characters/{id}`` 和 ``GET /p1/characters/{id}/casts``，
    组装为 ``ingest_characters()`` 可直接消费的 dict。

    Attributes:
        client: BangumiClient 实例，复用其 HTTP 会话和重试逻辑。
        concurrency: 批量处理时的最大并发数，默认 3。
        rate_limit: 单次请求后的休眠秒数，默认 0.2。
    """

    def __init__(
        self,
        client,
        concurrency: int = _DEFAULT_CONCURRENCY,
        rate_limit: float = _DEFAULT_RATE_LIMIT,
    ) -> None:
        self.client = client
        self._semaphore = asyncio.Semaphore(concurrency)
        self._rate_limit = rate_limit

    # ── 单条富化 ──────────────────────────────────────────

    async def enrich(self, character_id: int) -> dict[str, Any]:
        """富化单个角色。

        并发获取角色详情和出演作品列表，组装为 ingestion-ready dict。

        Args:
            character_id: Bangumi 角色 ID。

        Returns:
            成功时返回包含所有必填字段的 dict（key 见模块文档）。
            失败时返回 ``{"_error": "...", "character_id": character_id}``。
        """
        try:
            detail_task = asyncio.create_task(
                self.client._get(f"/p1/characters/{character_id}")
            )
            casts_task = asyncio.create_task(
                self.client._get(f"/p1/characters/{character_id}/casts")
            )

            raw = await detail_task
            casts_raw = await casts_task
        except Exception as exc:
            logger.warning("character %d API 调用异常: %s", character_id, exc)
            return {"_error": str(exc), "character_id": character_id}

        # 检查详情 API 失败
        if "_error" in raw:
            logger.warning("character %d 详情获取失败: %s", character_id, raw["_error"])
            return {"_error": raw["_error"], "character_id": character_id}

        # 提取 casts
        casts_list: list[dict] = []
        if "_error" not in casts_raw and isinstance(casts_raw, dict):
            data = casts_raw.get("data", []) or []
            for cast_entry in data:
                subj = (cast_entry.get("subject", {}) or {}) if isinstance(cast_entry, dict) else {}
                sid = subj.get("id", 0)
                if sid:
                    casts_list.append({
                        "subject_id": sid,
                        "subject_name": subj.get("nameCN") or subj.get("name", ""),
                        "person_id": None,
                        "person_name": None,
                        "type": 1,
                    })

        # 推断最知名出处 — 排除广播剧/CD/音声等衍生作品，优先取正片
        _SPINOFF_KEYWORDS = ["广播剧", "ドラマCD", "オーディオ", "ラジオ", "Sound", "角色歌"]
        subject_name = ""
        for c in casts_list:
            name = c.get("subject_name", "")
            if not any(kw in name for kw in _SPINOFF_KEYWORDS):
                subject_name = name
                break
        if not subject_name:
            subject_name = casts_list[0]["subject_name"] if casts_list else ""

        # 构造 chunk_text
        summary = (raw.get("summary") or "").strip()
        info = (raw.get("info") or "").strip()
        chunk_text = _build_chunk_text(summary, info)

        # infobox 清洗
        infobox = _clean_infobox(
            raw.get("infobox", []) or [],
            drop_keys=_CHARACTER_INFOBOX_DROP_KEYS,
        )

        return {
            "character_id": raw.get("id", character_id),
            "name": raw.get("name", ""),
            "name_cn": raw.get("nameCN", ""),
            "summary_text": chunk_text,
            "subject_name": subject_name,
            "role": raw.get("role", 0),
            "collects": raw.get("collects", 0),
            "comment": raw.get("comment", 0),
            "summary": summary or None,
            "info": info or None,
            "infobox": infobox,
            "casts_raw": casts_list,
            "nsfw": raw.get("nsfw", False),
        }

    # ── 批量富化 ──────────────────────────────────────────

    async def enrich_batch(self, character_ids: list[int]) -> list[dict[str, Any]]:
        """批量富化角色，并发 + 限流。

        单个角色失败不影响其他角色，返回 partial results。

        Args:
            character_ids: Bangumi 角色 ID 列表。

        Returns:
            成功和失败混合的 dict 列表（失败项含 ``_error`` 字段）。
            保证与输入顺序相同。
        """
        if not character_ids:
            return []

        async def _enrich_one(cid: int) -> dict[str, Any]:
            async with self._semaphore:
                result = await self.enrich(cid)
                if self._rate_limit > 0:
                    await asyncio.sleep(self._rate_limit)
                return result

        tasks = [_enrich_one(cid) for cid in character_ids]
        results = await asyncio.gather(*tasks)

        succeeded = sum(1 for r in results if "_error" not in r)
        failed = len(results) - succeeded
        if failed:
            logger.warning(
                "Character 批量富化: %d/%d 成功, %d 失败",
                succeeded, len(results), failed,
            )

        return list(results)


# ═══════════════════════════════════════════════════════════════════════
# PersonEnricher
# ═══════════════════════════════════════════════════════════════════════


class PersonEnricher:
    """人物数据富化器。

    并发调用 ``GET /p1/persons/{id}`` 和 ``GET /p1/persons/{id}/works``，
    组装为 ``ingest_persons()`` 可直接消费的 dict。

    Attributes:
        client: BangumiClient 实例。
        concurrency: 批量处理时的最大并发数，默认 3。
        rate_limit: 单次请求后的休眠秒数，默认 0.2。
    """

    def __init__(
        self,
        client,
        concurrency: int = _DEFAULT_CONCURRENCY,
        rate_limit: float = _DEFAULT_RATE_LIMIT,
    ) -> None:
        self.client = client
        self._semaphore = asyncio.Semaphore(concurrency)
        self._rate_limit = rate_limit

    # ── 单条富化 ──────────────────────────────────────────

    async def enrich(self, person_id: int) -> dict[str, Any]:
        """富化单个人物。

        并发获取人物详情和代表作列表，组装为 ingestion-ready dict。

        Args:
            person_id: Bangumi 人物 ID。

        Returns:
            成功时返回包含所有必填字段的 dict。
            失败时返回 ``{"_error": "...", "person_id": person_id}``。
        """
        try:
            detail_task = asyncio.create_task(
                self.client._get(f"/p1/persons/{person_id}")
            )
            works_task = asyncio.create_task(
                self.client._get(f"/p1/persons/{person_id}/works")
            )

            raw = await detail_task
            works_raw = await works_task
        except Exception as exc:
            logger.warning("person %d API 调用异常: %s", person_id, exc)
            return {"_error": str(exc), "person_id": person_id}

        if "_error" in raw:
            logger.warning("person %d 详情获取失败: %s", person_id, raw["_error"])
            return {"_error": raw["_error"], "person_id": person_id}

        # 提取 career
        career = raw.get("career", [])
        if not isinstance(career, list):
            career = [career] if career else []

        # 提取 works
        works_list: list[dict] = []
        if "_error" not in works_raw and isinstance(works_raw, dict):
            data = works_raw.get("data", []) or []
            for work_entry in data:
                subj = (work_entry.get("subject", {}) or {}) if isinstance(work_entry, dict) else {}
                sid = subj.get("id", 0)
                if not sid:
                    continue

                # positions 可能在 work_entry 层级或 subject 层级
                raw_positions = (
                    work_entry.get("positions", [])
                    if isinstance(work_entry, dict)
                    else []
                )
                if not raw_positions and isinstance(subj, dict):
                    raw_positions = subj.get("positions", []) or []

                positions: list[dict] = []
                if isinstance(raw_positions, list):
                    for pos in raw_positions:
                        if not isinstance(pos, dict):
                            continue
                        pos_type = pos.get("type", {}) if isinstance(pos.get("type"), dict) else {}
                        positions.append({
                            "type": {"cn": pos_type.get("cn", "") or pos.get("type_cn", "")},
                            "summary": pos.get("summary", ""),
                            "appearEps": pos.get("appearEps", ""),
                        })

                works_list.append({
                    "subject_id": sid,
                    "subject_name": subj.get("nameCN") or subj.get("name", ""),
                    "positions": positions,
                })

        # 构造 chunk_text — person 只有 summary 有实质内容
        summary = (raw.get("summary") or "").strip()
        info = (raw.get("info") or "").strip()
        chunk_text = _build_chunk_text(summary, info)

        # infobox 清洗
        infobox = _clean_infobox(
            raw.get("infobox", []) or [],
            drop_keys=_CHARACTER_INFOBOX_DROP_KEYS,
        )

        return {
            "person_id": raw.get("id", person_id),
            "name": raw.get("name", ""),
            "name_cn": raw.get("nameCN", ""),
            "summary_text": chunk_text,
            "career": career,
            "type": raw.get("type", 0),
            "collects": raw.get("collects", 0),
            "comment": raw.get("comment", 0),
            "summary": summary or None,
            "info": info or None,
            "infobox": infobox,
            "works_raw": works_list,
            "nsfw": raw.get("nsfw", False),
        }

    # ── 批量富化 ──────────────────────────────────────────

    async def enrich_batch(self, person_ids: list[int]) -> list[dict[str, Any]]:
        """批量富化人物，并发 + 限流。

        单个人物失败不影响其他人物，返回 partial results。

        Args:
            person_ids: Bangumi 人物 ID 列表。

        Returns:
            成功和失败混合的 dict 列表。保证与输入顺序相同。
        """
        if not person_ids:
            return []

        async def _enrich_one(pid: int) -> dict[str, Any]:
            async with self._semaphore:
                result = await self.enrich(pid)
                if self._rate_limit > 0:
                    await asyncio.sleep(self._rate_limit)
                return result

        tasks = [_enrich_one(pid) for pid in person_ids]
        results = await asyncio.gather(*tasks)

        succeeded = sum(1 for r in results if "_error" not in r)
        failed = len(results) - succeeded
        if failed:
            logger.warning(
                "Person 批量富化: %d/%d 成功, %d 失败",
                succeeded, len(results), failed,
            )

        return list(results)


# ═══════════════════════════════════════════════════════════════════════
# SubjectCollector
# ═══════════════════════════════════════════════════════════════════════


class SubjectCollector:
    """作品数据收集器。

    Subject 不需要 multi-hop 富化——单次 ``GET /p1/subjects/{id}`` 即返回
    全量数据（评分、标签、简介、infobox）。本类负责调 API 并映射为
    ``ingest_subjects()`` 可直接消费的格式。

    Attributes:
        client: BangumiClient 实例。
        concurrency: 批量处理时的最大并发数，默认 3。
        rate_limit: 单次请求后的休眠秒数，默认 0.15。
    """

    def __init__(
        self,
        client,
        concurrency: int = _DEFAULT_CONCURRENCY,
        rate_limit: float = 0.15,
    ) -> None:
        self.client = client
        self._semaphore = asyncio.Semaphore(concurrency)
        self._rate_limit = rate_limit

    async def collect(self, subject_id: int) -> dict[str, Any]:
        """收集单个作品数据。返回 ``ingest_subjects()`` 消费格式的 dict。"""
        try:
            raw = await self.client._get(f"/p1/subjects/{subject_id}")
        except Exception as exc:
            logger.warning("subject %d API 调用异常: %s", subject_id, exc)
            return {"_error": str(exc), "subject_id": subject_id}

        if "_error" in raw:
            logger.warning("subject %d 获取失败: %s", subject_id, raw["_error"])
            return {"_error": raw["_error"], "subject_id": subject_id}

        from clients.sanitizers import (
    _CHARACTER_INFOBOX_DROP_KEYS,
    _clean_infobox,
    sanitize_subject_detail,
)
        detail = sanitize_subject_detail(raw)

        summary = (detail.get("summary") or "").strip()
        info_text = (detail.get("info") or "").strip()
        chunk_text = _build_chunk_text(summary, info_text)

        date_str = detail.get("date", "") or ""
        year = None
        if date_str and len(date_str) >= 4:
            try:
                year = int(date_str[:4])
            except ValueError:
                pass

        # sanitize_subject_detail 把 collection key 转成了中文（"想看"/"看过"），
        # 但 SubjectMeta 期望 int key。从 raw 直接取数字 key。
        collection_raw = raw.get("collection", {}) or {}
        collection: dict[int, int] = {}
        for k, v in collection_raw.items():
            try:
                collection[int(k)] = int(v) if isinstance(v, (int, float, str)) else 0
            except (ValueError, TypeError):
                pass

        return {
            "subject_id": detail["id"],
            "name": detail.get("name", ""),
            "name_cn": detail.get("name_cn", ""),
            "info": detail.get("info", ""),
            "summary_text": chunk_text,
            "score": detail.get("score", 0.0),
            "rating_total": detail.get("rating_total", 0),
            "rank": detail.get("rank", 0),
            "rating_count": detail.get("rating_count", []),
            "collection": collection,
            "date": date_str or None,
            "year": year,
            "platform": detail.get("type", ""),
            "eps": detail.get("eps", 0),
            "volumes": detail.get("volumes", 0),
            "series": detail.get("series", False),
            "series_entry": detail.get("series_entry", False),
            "nsfw": detail.get("nsfw", False),
            "infobox": detail.get("infobox", {}),
            "tags": detail.get("tags", []),
        }

    async def collect_batch(self, subject_ids: list[int]) -> list[dict[str, Any]]:
        """批量收集作品数据，并发 + 限流。"""
        if not subject_ids:
            return []

        async def _collect_one(sid: int) -> dict[str, Any]:
            async with self._semaphore:
                result = await self.collect(sid)
                if self._rate_limit > 0:
                    await asyncio.sleep(self._rate_limit)
                return result

        tasks = [_collect_one(sid) for sid in subject_ids]
        results = await asyncio.gather(*tasks)

        succeeded = sum(1 for r in results if "_error" not in r)
        failed = len(results) - succeeded
        if failed:
            logger.warning(
                "Subject 批量收集: %d/%d 成功, %d 失败",
                succeeded, len(results), failed,
            )

        return list(results)
