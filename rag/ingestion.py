"""
RAG 数据摄入模块

负责将预处理后的实体文本块批量向量化并写入 PostgreSQL + pgvector。

============================================================================
  架构演进: 单表多态摄入 (Single Table Polymorphism Ingestion)
============================================================================
  RagEntityIngestor 面向 ``rag_entities`` 表，支持 Subject / Character /
  Person 三类实体的统一摄入，核心增强：

  1. **防稀释语义前缀 (Semantic Prefixing)**：在 embedding 前拼接极简
     自然语言定调前缀，防止机械键值对模板词稀释大模型 Embedding 语义质心。
  2. **关联边内存洗牌与重排 (In-Memory Re-sorting & Pruning)**：
     对照本地 RagEntity 中关联作品的热度 (rating_total)，在 Python 内存中
     按热度降序重排 casts / works 列表，强力截断至 Top 10 代表作。
============================================================================
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select

from database.models import (
    BangumiChunk,
    CharacterCast,
    CharacterMeta,
    PersonMeta,
    PersonWork,
    RagEntity,
    SubjectMeta,
)

logger = logging.getLogger("bgm-agent.ingestion")

# ============================================================================
# 语义前缀构造器 — 防稀释 Embedding 语义质心
# ============================================================================


def _build_subject_chunk_text(name_cn: str, chunk_text: str) -> str:
    """为作品文本块拼接语义定调前缀。

    Args:
        name_cn: 条目中文名称，为空时省略。
        chunk_text: 原始文本块内容。

    Returns:
        带 ``[作品名]`` 前缀的完整文本，供 embedding 向量化。
    """
    name_part = f"{name_cn}。" if name_cn else ""
    return f"[作品名] {name_part}{chunk_text}"


def _build_character_chunk_text(
    name_cn: str,
    subject_name: str,
    chunk_text: str,
) -> str:
    """为角色文本块拼接语义定调前缀。

    Args:
        name_cn: 角色中文名称。
        subject_name: 角色所属的（最知名）作品名称。
        chunk_text: 原始文本块内容。

    Returns:
        带 ``[角色]`` 及作品出处前缀的完整文本。
    """
    name_part = f"{name_cn}" if name_cn else ""
    work_part = f"，出自《{subject_name}》" if subject_name else ""
    return f"[角色] {name_part}{work_part}。{chunk_text}"


def _build_person_chunk_text(name_cn: str, chunk_text: str) -> str:
    """为人物文本块拼接语义定调前缀。

    Args:
        name_cn: 人物中文名称，为空时省略。
        chunk_text: 原始文本块内容。

    Returns:
        带 ``[人物]`` 前缀的完整文本。
    """
    name_part = f"{name_cn}。" if name_cn else ""
    return f"[人物] {name_part}{chunk_text}"


# ============================================================================
# 实体 ID 前缀化工具
# ============================================================================


def _prefixed_subject_id(raw_id: int) -> str:
    """将原始数字 ID 转为带前缀的全局唯一标识。"""
    return f"subject_{raw_id}"


def _prefixed_character_id(raw_id: int) -> str:
    """将原始数字 ID 转为带前缀的全局唯一标识。"""
    return f"character_{raw_id}"


def _prefixed_person_id(raw_id: int) -> str:
    """将原始数字 ID 转为带前缀的全局唯一标识。"""
    return f"person_{raw_id}"


class RagEntityIngestor:
    """单表多态 RAG 实体摄入器（新架构）。

    面向 ``rag_entities`` 表，支持 Subject / Character / Person 三类实体
    的统一批量摄入。核心增强：
      - **防稀释语义前缀**：在 embedding 前拼接自然语言定调前缀。
      - **关联边内存重排与剪枝**：对照本地热度数据，按 rating_total 降序
        重排 casts / works 列表，截断至 Top 10。
    """

    def __init__(
        self,
        engine: Engine,
        zhipu_api_key: str = "",
        zhipu_base_url: str = "https://open.bigmodel.cn/api/paas/v4",
    ) -> None:
        self.engine = engine
        try:
            from zai import ZhipuAiClient

            self.client = ZhipuAiClient(api_key=zhipu_api_key)
            logger.info("RagEntityIngestor: ZhipuAiClient 初始化成功")
        except ImportError:
            self.client = None
            logger.warning("zai-sdk 未安装，embedding 功能不可用")
        except Exception as exc:
            self.client = None
            logger.error("智谱客户端初始化失败: %s", exc)

    # ── 内部工具方法 ──────────────────────────────────────────

    def _check_client(self) -> None:
        if self.client is None:
            raise RuntimeError(
                "智谱客户端未初始化，请确认 zai-sdk 已安装且 API Key 有效"
            )

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        self._check_client()
        if not texts:
            return []
        try:
            response = self.client.embeddings.create(model="embedding-3", input=texts)
            return [item.embedding for item in response.data]
        except Exception as exc:
            logger.error("embedding API 调用失败: %s", exc)
            raise RuntimeError(f"embedding API 调用失败: {exc}") from exc

    def _lookup_subject_rating_map(
        self, session: Session, subject_ids: set[int]
    ) -> dict[int, int]:
        """查询本地 RagEntity 中 Subject 的 rating_total 热度映射。"""
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
        """关联边内存洗牌与重排：按本地热度降序重排角色出演列表，截断至 Top 10。"""
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
                continue  # 同一作品不同版本（TV/总集篇）去重
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
        """关联边内存洗牌与重排：按本地热度降序重排人物代表作列表，截断至 Top 10。"""
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
                continue  # 同一作品不同版本（TV/总集篇）去重
            try:
                works.append(
                    PersonWork(
                        subject_id=prefixed,
                        subject_name=str(w.get("subject_name", "")),
                        character_id=(
                            _prefixed_character_id(w["character_id"])
                            if w.get("character_id")
                            else None
                        ),
                        character_name=w.get("character_name"),
                        role_type=w.get("type", 0),
                    )
                )
                seen_subjects.add(prefixed)
            except ValidationError as exc:
                logger.warning("PersonWork 校验失败，跳过: %s", exc)
            if len(works) >= 10:
                break
        return works

    # ── 公开摄入方法 ──────────────────────────────────────────

    def ingest_subjects(self, subjects_data: list[dict[str, Any]]) -> int:
        """摄入番剧 (Subject) 实体。

        对每个文本块拼接 ``[作品名] {name_cn}。`` 语义前缀后向量化，
        meta_info 经 SubjectMeta 契约校验后存入 JSONB。

        Args:
            subjects_data: 列表，每个字典包含::
                {
                    "subject_id": int, "name": str, "name_cn": str,
                    "chunk_text": str, "score": float, "rating_total": int,
                    "date": str | None, "eps": int, "nsfw": bool,
                    "tags": [{"name": str, "count": int}, ...],
                }
        Returns:
            成功写入数量。
        """
        if not subjects_data:
            raise ValueError("subjects_data 不能为空列表")
        self._check_client()

        prefixed_texts = [
            _build_subject_chunk_text(item.get("name_cn", "") or "", item["chunk_text"])
            for item in subjects_data
        ]
        embeddings = self._embed_batch(prefixed_texts)

        if len(embeddings) != len(subjects_data):
            raise ValueError("embedding 数量与输入不匹配")

        inserted = 0
        try:
            with Session(self.engine) as session:
                for item, vector in zip(subjects_data, embeddings):
                    meta = SubjectMeta(
                        score=item.get("score", 0.0),
                        rating_total=item.get("rating_total", 0),
                        date=item.get("date"),
                        eps=item.get("eps", 0),
                        nsfw=item.get("nsfw", False),
                        tags=item.get("tags", []),
                    )
                    entity = RagEntity(
                        id=_prefixed_subject_id(item["subject_id"]),
                        entity_type="subject",
                        name=item.get("name", ""),
                        name_cn=item.get("name_cn"),
                        chunk_text=_build_subject_chunk_text(
                            item.get("name_cn", "") or "", item["chunk_text"]
                        ),
                        embedding=vector,
                        meta_info=meta.model_dump(),
                    )
                    session.add(entity)
                    inserted += 1
                session.commit()
                logger.info("摄入 %d 条 Subject 到 rag_entities", inserted)
        except SQLAlchemyError as exc:
            logger.error("Subject 写入失败: %s", exc)
            raise RuntimeError(f"数据库写入失败: {exc}") from exc

        return inserted

    def ingest_characters(self, characters_data: list[dict[str, Any]]) -> int:
        """摄入角色 (Character) 实体。

        拼接 ``[角色] {name_cn}，出自《{subject_name}》。`` 前缀，
        casts 列表经内存重排（按关联作品 rating_total 降序，截断 Top 10）后
        经 CharacterMeta 契约校验存入 meta_info。

        Args:
            characters_data: 列表，每个字典包含::
                {
                    "character_id": int, "name": str, "name_cn": str,
                    "chunk_text": str, "subject_name": str,
                    "role": int, "collects": int,
                    "casts_raw": [
                        {"subject_id": int, "subject_name": str,
                         "person_id": int|None, "person_name": str|None,
                         "type": int}, ...
                    ],
                }
        Returns:
            成功写入数量。
        """
        if not characters_data:
            raise ValueError("characters_data 不能为空列表")
        self._check_client()

        prefixed_texts = [
            _build_character_chunk_text(
                item.get("name_cn", "") or "",
                item.get("subject_name", "") or "",
                item["chunk_text"],
            )
            for item in characters_data
        ]
        embeddings = self._embed_batch(prefixed_texts)

        if len(embeddings) != len(characters_data):
            raise ValueError("embedding 数量与输入不匹配")

        inserted = 0
        try:
            with Session(self.engine) as session:
                for item, vector in zip(characters_data, embeddings):
                    casts = self._rerank_casts(session, item.get("casts_raw", []))
                    meta = CharacterMeta(
                        role=item.get("role", 0),
                        collects=item.get("collects", 0),
                        casts=casts,
                    )
                    entity = RagEntity(
                        id=_prefixed_character_id(item["character_id"]),
                        entity_type="character",
                        name=item.get("name", ""),
                        name_cn=item.get("name_cn"),
                        chunk_text=_build_character_chunk_text(
                            item.get("name_cn", "") or "",
                            item.get("subject_name", "") or "",
                            item["chunk_text"],
                        ),
                        embedding=vector,
                        meta_info=meta.model_dump(),
                    )
                    session.add(entity)
                    inserted += 1
                session.commit()
                logger.info("摄入 %d 条 Character 到 rag_entities", inserted)
        except SQLAlchemyError as exc:
            logger.error("Character 写入失败: %s", exc)
            raise RuntimeError(f"数据库写入失败: {exc}") from exc

        return inserted

    def ingest_persons(self, persons_data: list[dict[str, Any]]) -> int:
        """摄入人物 (Person) 实体。

        拼接 ``[人物] {name_cn}。`` 前缀，
        works 列表经内存重排（按关联作品 rating_total 降序，截断 Top 10）后
        经 PersonMeta 契约校验存入 meta_info。

        Args:
            persons_data: 列表，每个字典包含::
                {
                    "person_id": int, "name": str, "name_cn": str,
                    "chunk_text": str, "career": str, "type": int,
                    "collects": int,
                    "works_raw": [
                        {"subject_id": int, "subject_name": str,
                         "character_id": int|None, "character_name": str|None,
                         "type": int}, ...
                    ],
                }
        Returns:
            成功写入数量。
        """
        if not persons_data:
            raise ValueError("persons_data 不能为空列表")
        self._check_client()

        prefixed_texts = [
            _build_person_chunk_text(item.get("name_cn", "") or "", item["chunk_text"])
            for item in persons_data
        ]
        embeddings = self._embed_batch(prefixed_texts)

        if len(embeddings) != len(persons_data):
            raise ValueError("embedding 数量与输入不匹配")

        inserted = 0
        try:
            with Session(self.engine) as session:
                for item, vector in zip(persons_data, embeddings):
                    works = self._rerank_works(session, item.get("works_raw", []))
                    meta = PersonMeta(
                        career=item.get("career", ""),
                        type=item.get("type", 0),
                        collects=item.get("collects", 0),
                        works=works,
                    )
                    entity = RagEntity(
                        id=_prefixed_person_id(item["person_id"]),
                        entity_type="person",
                        name=item.get("name", ""),
                        name_cn=item.get("name_cn"),
                        chunk_text=_build_person_chunk_text(
                            item.get("name_cn", "") or "", item["chunk_text"]
                        ),
                        embedding=vector,
                        meta_info=meta.model_dump(),
                    )
                    session.add(entity)
                    inserted += 1
                session.commit()
                logger.info("摄入 %d 条 Person 到 rag_entities", inserted)
        except SQLAlchemyError as exc:
            logger.error("Person 写入失败: %s", exc)
            raise RuntimeError(f"数据库写入失败: {exc}") from exc

        return inserted


class BangumiIngestor:
    """[DEPRECATED] Bangumi 数据摄入器。

    .. deprecated::
        此摄入器将在后续 Phase 中移除，请迁移至 ``RagEntityIngestor``。

    将经过 ``BangumiTextProcessor`` 预处理后的文本块批量向量化，
    并写入数据库中的 ``bangumi_chunks`` 表。

    核心设计原则：
      - **正文与 Metadata 分离**：Embedding 仅基于 chunk_text 纯文本，
        tags、评分等结构化字段存入 meta_info JSON 列，供后续 SQL 硬过滤。
      - **批量处理**：一次性对一批文本调用 embedding API，减少网络开销。
      - **防御性编程**：API 异常和数据库异常均被捕获并记录，不中断整体流程。

    Attributes:
        engine: SQLAlchemy Engine 实例，用于创建数据库会话。
        client: 智谱 ZhipuAiClient 实例，用于调用 embedding-3 模型。
    """

    def __init__(
        self,
        engine: Engine,
        zhipu_api_key: str = "",
        zhipu_base_url: str = "https://open.bigmodel.cn/api/paas/v4",
    ) -> None:
        """初始化数据摄入器。

        Args:
            engine: SQLAlchemy Engine 实例，通常由 ``database.engine.engine`` 提供。
            zhipu_api_key: 智谱 API 密钥。通过环境变量 ``ZHIPU_API_KEY`` 注入。
            zhipu_base_url: 智谱 API 基础 URL，默认使用官方地址。
        """
        self.engine = engine

        # ── 智谱客户端初始化 ──────────────────────────────────
        # 延迟导入以避免 zai-sdk 未安装时阻塞整个模块的加载
        try:
            from zai import ZhipuAiClient

            self.client: ZhipuAiClient = ZhipuAiClient(api_key=zhipu_api_key)
            logger.info("智谱 ZhipuAiClient 初始化成功")
        except ImportError:
            self.client = None  # type: ignore[assignment]
            logger.warning(
                "zai-sdk 未安装，embedding 功能不可用。请执行: pip install zai-sdk"
            )
        except Exception as exc:
            self.client = None  # type: ignore[assignment]
            logger.error("智谱客户端初始化失败: %s", exc)

    def ingest_chunks(self, chunks_data: list[dict[str, Any]]) -> int:
        """将预处理后的文本块批量向量化并写入数据库。

        严格遵循正文与 Metadata 分离策略：
          1. 提取所有条目的 ``text``（纯摘要正文）组成列表。
          2. 调用智谱 embedding-3 API 批量获取向量。
          3. 遍历数据与向量，构造 ``BangumiChunk`` 对象——
             ``chunk_text`` 存储正文，``meta_info`` 存储 tags、评分等结构化字段。
          4. 批量写入数据库并提交事务。

        Args:
            chunks_data: 预处理后的文本块列表，每个字典包含::

                {
                    "chunk_id": int,          # 分块序号（仅用于日志追踪）
                    "subject_id": int,        # Bangumi 条目 ID
                    "name": str,              # 条目名称
                    "type": int,              # 条目类型 (1=书籍, 2=动画, ...)
                    "score": float,           # 评分
                    "rating_total": int,      # 评分人数（热度信号，用于降级排序）
                    "nsfw": bool,             # 安全护栏：是否为 R18 内容
                    "core_staff": list[str],  # 知识图谱：核心制作人员（导演/原作等）
                    "main_cv": list[str],     # 知识图谱：主役声优
                    "tags": list[str],        # 前10个社区标签
                    "text": str,              # 切分后的纯文本正文
                }

        Returns:
            成功写入数据库的条目数。

        Raises:
            ValueError: 若 chunks_data 为空列表。
            RuntimeError: 若智谱客户端未初始化（zai-sdk 未安装或配置错误）。
        """
        if not chunks_data:
            raise ValueError("chunks_data 不能为空列表")

        if self.client is None:
            raise RuntimeError(
                "智谱客户端未初始化，无法进行 embedding。"
                "请确认 zai-sdk 已安装且 API Key 有效。"
            )

        # ── Step 1: 纯正文提取 ────────────────────────────────
        # 仅提取 text 字段，tags 等元数据绝不参与向量化
        raw_texts: list[str] = [item["text"] for item in chunks_data]

        logger.info(
            "准备批量 embedding: %d 条文本, 前3条预览: %s",
            len(raw_texts),
            [t[:50] + "..." if len(t) > 50 else t for t in raw_texts[:3]],
        )

        # ── Step 2: 批量 Embedding ─────────────────────────────
        try:
            response = self.client.embeddings.create(
                model="embedding-3",
                input=raw_texts,
            )
            embeddings: list[list[float]] = [item.embedding for item in response.data]
            logger.info("embedding 完成: 获取 %d 条向量", len(embeddings))
        except Exception as exc:
            logger.error("智谱 embedding API 调用失败: %s", exc)
            raise RuntimeError(f"embedding API 调用失败: {exc}") from exc

        # ── 安全校验：向量数量与输入数量必须一致 ──────────────
        if len(embeddings) != len(raw_texts):
            raise ValueError(
                f"embedding 返回数量 ({len(embeddings)}) "
                f"与输入数量 ({len(raw_texts)}) 不匹配"
            )

        # ── Step 3 & 4: 组装 BangumiChunk 并批量写入 ──────────
        inserted_count = 0

        try:
            with Session(self.engine) as session:
                for item, vector in zip(chunks_data, embeddings):
                    chunk = BangumiChunk(
                        entity_type="subject",
                        entity_id=item["subject_id"],
                        chunk_text=item["text"],
                        embedding=vector,
                        meta_info={
                            # ── 核心元数据 ──────────────────────────
                            "name": item.get("name", ""),
                            "subject_type": item.get("type", 0),
                            "score": item.get("score", 0.0),
                            "tags": item.get("tags", []),
                            # ── 热度信号（降级排序用） ─────────────
                            "rating_total": item.get("rating_total", 0),
                            # ── 安全护栏 ───────────────────────────
                            "nsfw": item.get("nsfw", False),
                            # ── 知识图谱 ───────────────────────────
                            "core_staff": item.get("core_staff", []),
                            "main_cv": item.get("main_cv", []),
                        },
                    )
                    session.add(chunk)
                    inserted_count += 1

                session.commit()
                logger.info("成功写入 %d 条 chunk 到 bangumi_chunks 表", inserted_count)

        except SQLAlchemyError as exc:
            logger.error("数据库写入失败: %s", exc)
            raise RuntimeError(f"数据库写入失败: {exc}") from exc
        except Exception as exc:
            logger.error("未知异常: %s", exc)
            raise RuntimeError(f"摄入过程异常: {exc}") from exc

        return inserted_count
