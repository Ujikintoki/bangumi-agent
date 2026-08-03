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

logger = logging.getLogger("bgm-agent.enricher")

# 并发控制
_DEFAULT_CONCURRENCY = 3
_DEFAULT_RATE_LIMIT = 0.2  # 秒

# chunk_text 安全上限（超出截断，防止 embedding API token 溢出）
_MAX_CHUNK_CHARS = 3000


# ═══════════════════════════════════════════════════════════════════════
# chunk_text 构造
# ═══════════════════════════════════════════════════════════════════════


def _build_chunk_text(summary: str, info: str | None = None) -> str:
    """从 summary 和 info 构造 chunk_text。

    只取摘要主体（summary）和一行简介（info），不拼接 infobox kv 对。
    硬截断在 ``_MAX_CHUNK_CHARS`` 以内。

    Args:
        summary: 实体摘要文本（来自 API ``summary`` 字段）。
        info: 实体一行简介（来自 API ``info`` 字段），可选。

    Returns:
        拼接并截断后的纯文本。
    """
    parts: list[str] = []
    if summary:
        parts.append(summary.strip())
    if info and info.strip():
        # 避免 info 与 summary 开头重复
        if not summary or not summary.strip().startswith(info.strip()[:10]):
            parts.append(info.strip())

    text = "。".join(parts) if parts else ""
    if len(text) > _MAX_CHUNK_CHARS:
        # 尽量在句号处断开
        cut = text.rfind("。", 0, _MAX_CHUNK_CHARS)
        if cut > _MAX_CHUNK_CHARS // 2:
            text = text[:cut] + "。"
        else:
            text = text[:_MAX_CHUNK_CHARS] + "..."
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

        # 推断最知名出处
        subject_name = casts_list[0]["subject_name"] if casts_list else ""

        # 构造 chunk_text
        summary = (raw.get("summary") or "").strip()
        info = (raw.get("info") or "").strip()
        chunk_text = _build_chunk_text(summary, info)

        return {
            "character_id": raw.get("id", character_id),
            "name": raw.get("name", ""),
            "name_cn": raw.get("nameCN", ""),
            "chunk_text": chunk_text,
            "subject_name": subject_name,
            "role": raw.get("role", 0),
            "collects": raw.get("collects", 0),
            "summary": summary or None,
            "info": info or None,
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

        return {
            "person_id": raw.get("id", person_id),
            "name": raw.get("name", ""),
            "name_cn": raw.get("nameCN", ""),
            "chunk_text": chunk_text,
            "career": career,
            "type": raw.get("type", 0),
            "collects": raw.get("collects", 0),
            "summary": summary or None,
            "info": info or None,
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
