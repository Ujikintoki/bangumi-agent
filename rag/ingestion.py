"""
RAG 数据摄入模块

负责将预处理后的实体文本批量向量化并写入 PostgreSQL + pgvector。

============================================================================
  架构演进: embed_text / rag_output 分离
============================================================================
  RagEntityIngestor 面向 ``rag_entities`` 表，支持 Subject / Character /
  Person 三类实体的统一摄入。

  核心设计：
    1. **embed_text**：关键词密集合成文本，仅供 embedding 向量化。
    2. **rag_output**：预构建 JSON dict（对齐 API detail 工具 schema），检索时零转换。
    3. **meta_info**（JSONB）：结构化元数据，Pydantic v2 契约校验。
    4. **关联边内存重排**：按 rating_total 降序重排 casts / works，截断至 Top 10。
============================================================================
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select

from database.rag_tables import (
    CharacterCast,
    CharacterMeta,
    PersonMeta,
    PersonWork,
    RagEntity,
    SubjectMeta,
)
from ._utils import _clean_text, _first_sentence

logger = logging.getLogger("bgm-agent.ingestion")

# ============================================================================
# embed_text 构造 — 关键词密集，按区分度降序，无模板标记
# ============================================================================

# 安全上限：embedding-2 单条 ≤512 tokens，CJK ~1.5–2 tokens/char
_MAX_EMBED_CHARS = 400


def _build_subject_embed_text(item: dict[str, Any], subject_type: int = 2) -> str:
    """构建 Subject 的 embed_text。

    格式: {name_cn} {name} {medium_label} {top tags} {year} {platform} {score}分 {summary首句}
    """
    parts: list[str] = []

    # 名称（最高优先级）
    name_cn = (item.get("name_cn") or "").strip()
    name = (item.get("name") or "").strip()
    if name_cn:
        parts.append(name_cn)
    if name and name != name_cn:
        parts.append(name)

    # 媒介类型标签
    if subject_type == 2:
        parts.append("动画")
    elif subject_type == 1:
        parts.append("书籍")

    # 标签（top 10，按 count 降序）
    tags = item.get("tags", []) or []
    if tags:
        tag_names = [t["name"] for t in tags[:10] if isinstance(t, dict) and t.get("name")]
        if tag_names:
            parts.extend(tag_names)

    # 年代 + 平台
    year = item.get("year")
    if year:
        parts.append(str(year))
    platform = (item.get("platform") or "").strip()
    if platform:
        parts.append(platform)

    # 评分
    score = item.get("score", 0)
    if score > 0:
        parts.append(f"{score:.1f}分")

    # 简介首句（语义兜底，放在最后）
    summary_text = (item.get("summary_text") or "").strip()
    if summary_text:
        first = _first_sentence(summary_text)
        if first and first not in " ".join(parts):
            parts.append(first)

    text = " ".join(parts)
    if len(text) > _MAX_EMBED_CHARS:
        # 从尾部截断，优先保留开头的名称和标签
        text = text[:_MAX_EMBED_CHARS].rsplit(" ", 1)[0]
    return text


def _build_character_embed_text(item: dict[str, Any]) -> str:
    """构建 Character 的 embed_text。

    格式: {name_cn} {name} {subject_name} {summary首句}
    """
    parts: list[str] = []

    name_cn = (item.get("name_cn") or "").strip()
    name = (item.get("name") or "").strip()
    if name_cn:
        parts.append(name_cn)
    if name and name != name_cn:
        parts.append(name)

    subject_name = (item.get("subject_name") or "").strip()
    if subject_name:
        parts.append(subject_name)

    summary_text = (item.get("summary_text") or "").strip()
    if summary_text:
        first = _first_sentence(summary_text)
        if first:
            parts.append(first)

    text = " ".join(parts)
    if len(text) > _MAX_EMBED_CHARS:
        text = text[:_MAX_EMBED_CHARS].rsplit(" ", 1)[0]
    return text


def _build_person_embed_text(item: dict[str, Any]) -> str:
    """构建 Person 的 embed_text。

    格式: {name_cn} {name} {career} {top_3_works_names} {summary首句}
    """
    parts: list[str] = []

    name_cn = (item.get("name_cn") or "").strip()
    name = (item.get("name") or "").strip()
    if name_cn:
        parts.append(name_cn)
    if name and name != name_cn:
        parts.append(name)

    career = item.get("career", []) or []
    if career:
        parts.extend([c for c in career if isinstance(c, str)])

    works = item.get("works_raw", []) or []
    if works:
        work_names = [
            w["subject_name"]
            for w in works[:3]
            if isinstance(w, dict) and w.get("subject_name")
        ]
        parts.extend(work_names)

    summary_text = (item.get("summary_text") or "").strip()
    if summary_text:
        first = _first_sentence(summary_text)
        if first:
            parts.append(first)

    text = " ".join(parts)
    if len(text) > _MAX_EMBED_CHARS:
        text = text[:_MAX_EMBED_CHARS].rsplit(" ", 1)[0]
    return text


# ============================================================================
# rag_output 构造 — 预构建最终返回 dict（JSON string），检索时直接 json.loads 返回
# ============================================================================

import json

# 映射常量
_CHARACTER_ROLES = {1: "主角", 2: "配角", 3: "客串"}
_PERSON_TYPES = {1: "个人", 2: "公司", 3: "组合"}
_COLLECTION_LABELS = {1: "想看", 2: "看过", 3: "在看", 4: "搁置", 5: "抛弃"}


def _build_subject_rag_dict(item: dict[str, Any], raw_id: int, subject_type: int = 2) -> str:
    """构建 Subject 返回 dict，严格对齐 sanitize_subject_detail 格式。

    追加 _source / _next / subject_type 三个 RAG 专用字段。
    """
    collection = {}
    raw_col = item.get("collection", {}) or {}
    if isinstance(raw_col, dict):
        for k, v in raw_col.items():
            label = _COLLECTION_LABELS.get(int(k), str(k))
            collection[label] = v

    tags = item.get("tags", []) or []
    if isinstance(tags, list):
        tags = [
            {"name": t["name"], "count": t["count"]}
            for t in tags if isinstance(t, dict) and t.get("name")
        ]

    result = {
        # ── 核心标识（对齐 sanitize_subject_detail）──
        "id": raw_id,
        "name": item.get("name", ""),
        "name_cn": item.get("name_cn") or "",
        "type": (item.get("platform") or ""),
        "info": item.get("info") or "",
        "date": item.get("date") or "",
        "eps": item.get("eps", 0),
        "volumes": item.get("volumes", 0),
        "series": item.get("series", False),
        "series_entry": item.get("series_entry", False),
        "nsfw": item.get("nsfw", False),
        # ── 文本 ──
        "summary": (item.get("summary_text") or "").strip(),
        # ── 评分 ──
        "score": item.get("score", 0.0),
        "rank": item.get("rank", 0),
        "rating_total": item.get("rating_total", 0),
        "rating_count": item.get("rating_count", []),
        # ── 收藏 ──
        "collection": collection,
        # ── 标签 ──
        "tags": tags,
        # ── Infobox ──
        "infobox": item.get("infobox", {}),
        # ── RAG 标记 ──
        "_source": "rag",
        "subject_type": subject_type,
        "_next": f"如需口碑数据调 get_subject_opinions({raw_id})；"
                 f"如需角色列表调 get_subject_characters({raw_id})",
    }
    return json.dumps(result, ensure_ascii=False)


def _build_character_rag_dict(item: dict[str, Any], raw_id: int) -> str:
    """构建 Character 返回 dict，严格对齐 sanitize_character_detail 格式。

    追加 works / _source 两个 RAG 专用字段。
    """
    role_int = item.get("role", 0)
    works = []
    casts = item.get("casts_raw", []) or []
    if isinstance(casts, list):
        works = [
            {"subject_name": c.get("subject_name", "")}
            for c in casts[:3] if isinstance(c, dict)
        ]

    result = {
        # ── 核心标识（对齐 sanitize_character_detail）──
        "id": raw_id,
        "name": item.get("name", ""),
        "name_cn": item.get("name_cn") or "",
        "role": _CHARACTER_ROLES.get(role_int, "未知"),
        "info": item.get("info") or "",
        "summary": (item.get("summary_text") or "").strip(),
        "infobox": item.get("infobox", {}),
        "comment": item.get("comment", 0),
        "collects": item.get("collects", 0),
        "nsfw": item.get("nsfw", False),
        # ── RAG 专用 ──
        "works": works,
        "_source": "rag",
    }
    return json.dumps(result, ensure_ascii=False)


def _build_person_rag_dict(item: dict[str, Any], raw_id: int) -> str:
    """构建 Person 返回 dict，严格对齐 sanitize_person_detail 格式。

    追加 works / _source 两个 RAG 专用字段。
    """
    type_int = item.get("type", 0)
    works = []
    raw_works = item.get("works_raw", []) or []
    if isinstance(raw_works, list):
        for w in raw_works[:5]:
            if not isinstance(w, dict):
                continue
            positions = w.get("positions", []) or []
            role = ""
            if positions and isinstance(positions[0], dict):
                role = positions[0].get("type_cn", "")
            works.append({
                "subject_name": w.get("subject_name", ""),
                "role": role,
            })

    result = {
        # ── 核心标识（对齐 sanitize_person_detail）──
        "id": raw_id,
        "name": item.get("name", ""),
        "name_cn": item.get("name_cn") or "",
        "type": _PERSON_TYPES.get(type_int, "未知"),
        "career": item.get("career", []),
        "info": item.get("info") or "",
        "summary": (item.get("summary_text") or "").strip(),
        "infobox": item.get("infobox", {}),
        "comment": item.get("comment", 0),
        "collects": item.get("collects", 0),
        "nsfw": item.get("nsfw", False),
        # ── RAG 专用 ──
        "works": works,
        "_source": "rag",
    }
    return json.dumps(result, ensure_ascii=False)


# ============================================================================
# 实体 ID 前缀化工具
# ============================================================================


def _prefixed_subject_id(raw_id: int) -> str:
    return f"subject_{raw_id}"


def _prefixed_character_id(raw_id: int) -> str:
    return f"character_{raw_id}"


def _prefixed_person_id(raw_id: int) -> str:
    return f"person_{raw_id}"


# ============================================================================
# RagEntityIngestor
# ============================================================================


class RagEntityIngestor:
    """单表多态 RAG 实体摄入器。

    面向 ``rag_entities`` 表，支持 Subject / Character / Person 三类实体
    的统一批量摄入。

    核心设计：
      - **embed_text 分离**：关键词密集合成文本 → embedding，chunk_text → Agent 上下文
      - **关联边内存重排**：按关联作品 rating_total 降序，截断至 Top 10
    """

    _EMBED_BATCH_SIZE = 32

    def __init__(
        self,
        engine: Engine,
        zhipu_api_key: str = "",
        zhipu_base_url: str = "https://open.bigmodel.cn/api/paas/v4",
    ) -> None:
        from clients.zhipu_client import init_zhipu_client

        self.engine = engine
        self.client, init_error = init_zhipu_client(zhipu_api_key, zhipu_base_url)
        if init_error:
            logger.warning("RagEntityIngestor: %s", init_error)

    def _check_client(self) -> None:
        if self.client is None:
            raise RuntimeError(
                "智谱客户端未初始化，请确认 zai-sdk 已安装且 API Key 有效"
            )

    # ── Embedding ─────────────────────────────────────────────

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        """对 embed_text 列表做批量向量化。"""
        self._check_client()
        if not texts:
            return []

        # 清洗 + 过滤空文本
        cleaned: list[str] = []
        skipped = 0
        for t in texts:
            c = _clean_text(t)
            if c:
                cleaned.append(c)
            else:
                cleaned.append(" ")
                skipped += 1
        if skipped:
            logger.warning("_embed_batch: %d 条 embed_text 为空，已替换为占位符", skipped)

        from core.config import get_settings as _gs
        model = _gs().EMBEDDING_MODEL
        all_embeddings: list[list[float]] = []
        for i in range(0, len(cleaned), self._EMBED_BATCH_SIZE):
            chunk = cleaned[i : i + self._EMBED_BATCH_SIZE]
            try:
                response = self.client.embeddings.create(model=model, input=chunk)
                all_embeddings.extend(item.embedding for item in response.data)
            except Exception as exc:
                logger.error(
                    "embedding API 调用失败 (batch %d-%d): %s",
                    i, i + len(chunk), exc,
                )
                raise RuntimeError(f"embedding API 调用失败: {exc}") from exc
        return all_embeddings

    # ── 热度查表 + 关联边重排 ──────────────────────────────────

    def _lookup_subject_rating_map(
        self, session: Session, subject_ids: set[int]
    ) -> dict[int, int]:
        if not subject_ids:
            return {}
        prefixed = [_prefixed_subject_id(sid) for sid in subject_ids]
        stmt = select(RagEntity.id, RagEntity.meta_info).where(
            RagEntity.id.in_(prefixed),
            RagEntity.entity_type == "subject",
        )
        rows = session.exec(stmt).all()
        rating_map: dict[int, int] = {}
        for row in rows:
            raw_id_str = row.id.replace("subject_", "")
            try:
                raw_id = int(raw_id_str)
            except ValueError:
                continue
            rating_map[raw_id] = (row.meta_info or {}).get("rating_total", 0)
        return rating_map

    def _rerank_casts(
        self, session: Session, raw_casts: list[dict[str, Any]]
    ) -> list[CharacterCast]:
        if not raw_casts:
            return []
        subject_ids = {
            c["subject_id"] for c in raw_casts if isinstance(c.get("subject_id"), int)
        }
        rating_map = self._lookup_subject_rating_map(session, subject_ids)
        sorted_casts = sorted(
            raw_casts,
            key=lambda c: rating_map.get(c.get("subject_id", 0), 0),
            reverse=True,
        )
        casts: list[CharacterCast] = []
        seen_subjects: set[str] = set()
        for c in sorted_casts:
            prefixed = _prefixed_subject_id(c["subject_id"])
            if prefixed in seen_subjects:
                continue
            try:
                casts.append(
                    CharacterCast(
                        subject_id=prefixed,
                        subject_name=str(c.get("subject_name", "")),
                        person_id=(
                            _prefixed_person_id(c["person_id"])
                            if c.get("person_id")
                            else None
                        ),
                        person_name=c.get("person_name"),
                        role_type=c.get("type", 0),
                    )
                )
                seen_subjects.add(prefixed)
            except ValidationError as exc:
                logger.warning("CharacterCast 校验失败，跳过: %s", exc)
            if len(casts) >= 10:
                break
        return casts

    def _rerank_works(
        self, session: Session, raw_works: list[dict[str, Any]]
    ) -> list[PersonWork]:
        if not raw_works:
            return []
        subject_ids = {
            w["subject_id"] for w in raw_works if isinstance(w.get("subject_id"), int)
        }
        rating_map = self._lookup_subject_rating_map(session, subject_ids)
        sorted_works = sorted(
            raw_works,
            key=lambda w: rating_map.get(w.get("subject_id", 0), 0),
            reverse=True,
        )
        works: list[PersonWork] = []
        seen_subjects: set[str] = set()
        for w in sorted_works:
            prefixed = _prefixed_subject_id(w["subject_id"])
            if prefixed in seen_subjects:
                continue
            try:
                positions: list[dict] = []
                raw_positions = w.get("positions", [])
                if raw_positions:
                    for pos in raw_positions:
                        pos_type = pos.get("type", {}) if isinstance(pos.get("type"), dict) else {}
                        positions.append({
                            "type_cn": pos_type.get("cn", "") or pos.get("type_cn", ""),
                            "summary": pos.get("summary", ""),
                            "appear_eps": pos.get("appearEps", ""),
                        })
                works.append(
                    PersonWork(
                        subject_id=prefixed,
                        subject_name=str(w.get("subject_name", "")),
                        positions=positions,
                    )
                )
                seen_subjects.add(prefixed)
            except ValidationError as exc:
                logger.warning("PersonWork 校验失败，跳过: %s", exc)
            if len(works) >= 10:
                break
        return works

    # ── 公开摄入方法 ──────────────────────────────────────────

    def ingest_subjects(
        self,
        subjects_data: list[dict[str, Any]],
        subject_type: int = 2,
    ) -> int:
        """摄入 Subject 实体。

        embed_text ← 关键词密集合成文本 → embedding
        rag_output ← 预构建 JSON dict → 检索时直接返回

        Args:
            subjects_data: 富化后的 subject 数据列表。
            subject_type: Bangumi subject 类型，1=书籍, 2=动画。
        """
        if not subjects_data:
            raise ValueError("subjects_data 不能为空列表")
        self._check_client()

        embed_texts = [
            _build_subject_embed_text(item, subject_type) for item in subjects_data
        ]
        embeddings = self._embed_batch(embed_texts)

        if len(embeddings) != len(subjects_data):
            raise ValueError("embedding 数量与输入不匹配")

        inserted = 0
        try:
            with Session(self.engine) as session:
                for item, vector, et in zip(subjects_data, embeddings, embed_texts):
                    meta = SubjectMeta(
                        score=item.get("score", 0.0),
                        rank=item.get("rank", 0),
                        rating_total=item.get("rating_total", 0),
                        rating_count=item.get("rating_count", []),
                        collection=item.get("collection", {}),
                        date=item.get("date"),
                        year=item.get("year"),
                        platform=item.get("platform", ""),
                        eps=item.get("eps", 0),
                        tags=item.get("tags", []),
                    )
                    entity = RagEntity(
                        id=_prefixed_subject_id(item["subject_id"]),
                        entity_type="subject",
                        subject_type=subject_type,
                        name=item.get("name", ""),
                        name_cn=item.get("name_cn"),
                        nsfw=item.get("nsfw", False),
                        popularity=item.get("rating_total", 0),
                        embed_text=et,
                        rag_output=_build_subject_rag_dict(item, item["subject_id"], subject_type),
                        embedding=vector,
                        meta_info=meta.model_dump(),
                    )
                    session.merge(entity)
                    inserted += 1
                session.commit()
                logger.info("摄入 %d 条 Subject 到 rag_entities", inserted)
        except SQLAlchemyError as exc:
            logger.error("Subject 写入失败: %s", exc)
            raise RuntimeError(f"数据库写入失败: {exc}") from exc

        return inserted

    def ingest_characters(self, characters_data: list[dict[str, Any]]) -> int:
        """摄入 Character 实体。"""
        if not characters_data:
            raise ValueError("characters_data 不能为空列表")
        self._check_client()

        embed_texts = [_build_character_embed_text(item) for item in characters_data]
        embeddings = self._embed_batch(embed_texts)

        if len(embeddings) != len(characters_data):
            raise ValueError("embedding 数量与输入不匹配")

        inserted = 0
        try:
            with Session(self.engine) as session:
                for item, vector, et in zip(characters_data, embeddings, embed_texts):
                    casts = self._rerank_casts(session, item.get("casts_raw", []))
                    meta = CharacterMeta(
                        role=item.get("role", 0),
                        collects=item.get("collects", 0),
                        summary=item.get("summary"),
                        info=item.get("info"),
                        casts=casts,
                    )
                    entity = RagEntity(
                        id=_prefixed_character_id(item["character_id"]),
                        entity_type="character",
                        name=item.get("name", ""),
                        name_cn=item.get("name_cn"),
                        nsfw=item.get("nsfw", False),
                        popularity=item.get("collects", 0),
                        embed_text=et,
                        rag_output=_build_character_rag_dict(item, item["character_id"]),
                        embedding=vector,
                        meta_info=meta.model_dump(),
                    )
                    session.merge(entity)
                    inserted += 1
                session.commit()
                logger.info("摄入 %d 条 Character 到 rag_entities", inserted)
        except SQLAlchemyError as exc:
            logger.error("Character 写入失败: %s", exc)
            raise RuntimeError(f"数据库写入失败: {exc}") from exc

        return inserted

    def ingest_persons(self, persons_data: list[dict[str, Any]]) -> int:
        """摄入 Person 实体。"""
        if not persons_data:
            raise ValueError("persons_data 不能为空列表")
        self._check_client()

        embed_texts = [_build_person_embed_text(item) for item in persons_data]
        embeddings = self._embed_batch(embed_texts)

        if len(embeddings) != len(persons_data):
            raise ValueError("embedding 数量与输入不匹配")

        inserted = 0
        try:
            with Session(self.engine) as session:
                for item, vector, et in zip(persons_data, embeddings, embed_texts):
                    works = self._rerank_works(session, item.get("works_raw", []))
                    meta = PersonMeta(
                        career=item.get("career", []),
                        type=item.get("type", 0),
                        collects=item.get("collects", 0),
                        summary=item.get("summary"),
                        info=item.get("info"),
                        works=works,
                    )
                    entity = RagEntity(
                        id=_prefixed_person_id(item["person_id"]),
                        entity_type="person",
                        name=item.get("name", ""),
                        name_cn=item.get("name_cn"),
                        nsfw=item.get("nsfw", False),
                        popularity=item.get("collects", 0),
                        embed_text=et,
                        rag_output=_build_person_rag_dict(item, item["person_id"]),
                        embedding=vector,
                        meta_info=meta.model_dump(),
                    )
                    session.merge(entity)
                    inserted += 1
                session.commit()
                logger.info("摄入 %d 条 Person 到 rag_entities", inserted)
        except SQLAlchemyError as exc:
            logger.error("Person 写入失败: %s", exc)
            raise RuntimeError(f"数据库写入失败: {exc}") from exc

        return inserted
