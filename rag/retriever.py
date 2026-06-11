"""
RAG 混合检索模块

============================================================================
  架构演进: 多态检索器 (Polymorphic Retriever)
============================================================================
  RagEntityRetriever 面向 ``rag_entities`` 表，支持 Subject / Character /
  Person 三类实体的统一语义检索，核心增强：

  1. **标量前置过滤**：硬编码 entity_type WHERE 子句，仅在特定领域内做向量比对。
  2. **多态阶梯分桶排序**：保留向量距离分桶逻辑，桶内次级排序按实体类型动态路由：
     Subject → rating_total, Character/Person → collects。
============================================================================

检索策略：
  - SQL 硬过滤：entity_type 标量前置 + nsfw 安全护栏 + JSONB tags 交集过滤
  - 向量检索：对 query 做 embedding，用余弦距离召回 limit * 2 候选集
  - 多态降级重排：语义梯队分桶 → 动态热度信号降序
  - 距离阈值：丢弃 cosine_distance > threshold 的结果，防幻觉
"""

from __future__ import annotations

import logging
from typing import Literal, Optional

from pydantic import BaseModel, Field
from sqlalchemy import Engine, type_coerce
from sqlalchemy import cast as sa_cast
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Session, select

from database.rag_tables import BangumiChunk, RagEntity

logger = logging.getLogger("bgm-agent.retriever")

# ============================================================================
# 新架构: RagSearchResult — 多态检索结果
# ============================================================================


class RagSearchResult(BaseModel):
    """单条多态检索结果。

    统一承载 Subject / Character / Person 三类实体的检索命中信息。
    实体特有的结构化字段通过 ``meta_info`` dict 暴露，由调用方按需解析。

    Attributes:
        entity_id: 带前缀的全局唯一 ID，如 ``"subject_10"``。
        entity_type: 实体类型: ``"subject"`` / ``"character"`` / ``"person"``。
        chunk_text: 命中文本块原文。
        name: 实体原文名称。
        name_cn: 实体中文名称。
        cosine_distance: PGVector 余弦距离，范围 [0, 2]。
        final_score: 降级重排后的综合得分（梯队 ID），越小越好。
        meta_info: 反范式化元数据完整内容，包含评分、标签、关联边等。
    """

    entity_id: str = Field(description="带前缀的全局唯一 ID")
    entity_type: str = Field(description="实体类型: subject / character / person")
    chunk_text: str = Field(description="命中文本块原文")
    name: str = Field(default="", description="实体原文名称")
    name_cn: Optional[str] = Field(default=None, description="实体中文名称")
    cosine_distance: float = Field(description="余弦距离，越小越相似")
    final_score: float = Field(
        default=0.0, description="降级重排后的综合得分（梯队 ID）"
    )
    meta_info: dict = Field(default_factory=dict, description="反范式化元数据完整内容")


# ============================================================================
# 新架构: RagEntityRetriever — 多态 RAG 检索器
# ============================================================================


def _extract_heat_signal(meta: dict, entity_type: str) -> int:
    """根据实体类型动态提取次级热度信号，用于桶内降序重排。

    Args:
        meta: RagEntity.meta_info JSONB 内容。
        entity_type: 实体类型。

    Returns:
        热度信号整数值，默认为 0。
    """
    if entity_type == "subject":
        val = meta.get("rating_total", 0)
    elif entity_type in ("character", "person"):
        val = meta.get("collects", 0)
    else:
        val = 0
    return int(val) if isinstance(val, (int, float)) else 0


class RagEntityRetriever:
    """多态 RAG 检索器（新架构）。

    面向 ``rag_entities`` 表，支持对 Subject / Character / Person
    三类实体进行标量前置过滤 + 向量语义检索 + 多态阶梯分桶排序。

    Attributes:
        engine: SQLAlchemy Engine 实例。
        client: 智谱 ZhipuAiClient，用于查询向量化。
    """

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
            logger.warning("RagEntityRetriever: %s", init_error)

    def _check_client(self) -> None:
        if self.client is None:
            raise RuntimeError(
                "智谱客户端未初始化，无法进行查询 embedding。"
                "请确认 zai-sdk 已安装且 API Key 有效。"
            )

    def hybrid_search(
        self,
        query: str,
        entity_type: Literal["subject", "character", "person", "all"] = "all",
        limit: int = 5,
        exclude_nsfw: bool = True,
        distance_threshold: float = 0.65,
        semantic_bucket_size: float = 0.03,
    ) -> list[RagSearchResult]:
        """多态混合检索：标量前置过滤 → 向量召回 → 多态分桶排序 → 阈值防爆。

        检索管道：
          1. **标量前置过滤**：若 entity_type != "all"，硬编码 WHERE 条件
             限定检索域，仅在该领域内做向量比对。
          2. 查询向量化。
          3. 按 PGVector 余弦距离召回 limit * 2 候选集。
          4. **距离阈值防爆**：丢弃 cosine_distance > threshold 的候选。
          5. **多态阶梯分桶排序**：按 entity_type 动态选择次级热度信号：
             - subject → meta_info.rating_total
             - character / person → meta_info.collects
          6. 截取 top ``limit`` 条返回。

        Args:
            query: 自然语言查询。
            entity_type: 限定实体类型，``"all"`` 表示跨域检索。
            limit: 最大返回条数，默认 5。
            exclude_nsfw: 是否排除 R18（仅对 subject 生效）。
            distance_threshold: 余弦距离上限，默认 0.65。
            semantic_bucket_size: 语义梯队步长，默认 0.03。

        Returns:
            按 final_score 升序排列的 RagSearchResult 列表。
        """
        if not query or not query.strip():
            logger.warning("查询为空，返回空列表")
            return []

        self._check_client()

        # ── Step 1: 查询向量化 ────────────────────────────────
        try:
            response = self.client.embeddings.create(
                model="embedding-3",
                input=[query.strip()],
            )
            query_embedding: list[float] = response.data[0].embedding
            logger.debug("查询向量化: '%s' → %d 维", query[:50], len(query_embedding))
        except Exception as exc:
            logger.error("查询 embedding 失败: %s", exc)
            raise RuntimeError(f"查询 embedding 失败: {exc}") from exc

        # ── Step 2: 标量前置过滤 + 向量召回 ──────────────────
        candidate_limit = limit * 2
        # PGVector 同时负责排序和返回余弦距离——Python 层不再用 _compute_cosine_distance
        # 重算（避免 2048 维浮点向量重复计算及精度偏差）
        distance_expr = RagEntity.embedding.cosine_distance(query_embedding).label(
            "cosine_dist"
        )
        try:
            with Session(self.engine) as session:
                stmt = select(RagEntity, distance_expr)

                # ── 标量前置过滤: entity_type ────────────────
                if entity_type != "all":
                    stmt = stmt.where(RagEntity.entity_type == entity_type)
                    logger.debug("实体类型前置过滤: %s", entity_type)

                # ── 安全护栏: nsfw ──────────────────────────
                # subject 实体的 meta_info 中包含 nsfw 布尔字段；
                # character / person 无此字段，`@>` 匹配永远为 false，
                # 因此 NOT false = true 对其无影响（安全兜底正确）。
                if exclude_nsfw and entity_type in ("subject", "all"):
                    stmt = stmt.where(~RagEntity.meta_info.contains({"nsfw": True}))
                    logger.debug("安全护栏: 排除 nsfw=True")

                stmt = stmt.order_by(distance_expr).limit(candidate_limit)
                rows = session.execute(stmt).fetchall()

        except Exception as exc:
            logger.error("数据库查询失败: %s", exc)
            raise RuntimeError(f"多态检索查询失败: {exc}") from exc

        if not rows:
            logger.info(
                "无匹配: query='%s', entity_type=%s",
                query[:50],
                entity_type,
            )
            return []

        # ── Step 3: 组装候选集（距离由 PGVector 直接返回） ──
        raw_results: list[RagSearchResult] = []
        for row in rows:
            entity: RagEntity = row[0]
            distance: float = float(row[1])
            meta = entity.meta_info or {}

            raw_results.append(
                RagSearchResult(
                    entity_id=entity.id,
                    entity_type=entity.entity_type,
                    chunk_text=entity.chunk_text,
                    name=entity.name or "",
                    name_cn=entity.name_cn,
                    cosine_distance=distance,
                    final_score=0.0,
                    meta_info=meta,
                )
            )

        # ── Step 4: 距离阈值防爆 ──────────────────────────────
        within_threshold = [
            r for r in raw_results if r.cosine_distance <= distance_threshold
        ]
        discarded = len(raw_results) - len(within_threshold)
        if discarded > 0:
            logger.debug(
                "阈值预过滤: 丢弃 %d 条, 保留 %d 条", discarded, len(within_threshold)
            )

        if not within_threshold:
            logger.info("阈值过滤后无候选: query='%s'", query[:50])
            return []

        # ── Step 5: 多态阶梯分桶排序 ───────────────────────────
        within_threshold.sort(
            key=lambda r: (
                int(r.cosine_distance / semantic_bucket_size),
                -_extract_heat_signal(r.meta_info, r.entity_type),
            )
        )

        for r in within_threshold:
            r.final_score = float(int(r.cosine_distance / semantic_bucket_size))

        final_results = within_threshold[:limit]

        logger.info(
            "多态检索完成: query='%s', type=%s, 候选=%d, 最终=%d, "
            "top1='%s'(entity=%s, distance=%.4f, bucket=%d)",
            query[:50],
            entity_type,
            len(raw_results),
            len(final_results),
            final_results[0].name if final_results else "N/A",
            final_results[0].entity_type if final_results else "N/A",
            final_results[0].cosine_distance if final_results else 0,
            int(final_results[0].final_score) if final_results else -1,
        )

        return final_results


# ============================================================================
# 旧架构兼容: SearchResult / BangumiRetriever（已弃用）
# ============================================================================


class SearchResult(BaseModel):
    """单条混合检索结果。

    Attributes:
        entity_id: Bangumi 条目 ID，可链接到条目详情页。
        chunk_text: 命中文本块的原文内容。
        name: 条目名称（来自 meta_info）。
        score: 条目评分（来自 meta_info），0.0 表示无评分。
        rating_total: 评分人数（热度信号），0 表示无数据。
        tags: 条目标签列表（来自 meta_info）。
        subject_type: 条目类型（1=书籍, 2=动画, 3=音乐, 4=游戏, 6=三次元）。
        nsfw: 是否为 R18 内容（来自 meta_info）。
        core_staff: 核心制作人员列表（来自 meta_info）。
        main_cv: 主役声优列表（来自 meta_info）。
        cosine_distance: PGVector 余弦距离，范围 [0, 2]，越小越相似。
        final_score: 降级重排后的综合得分（越小越好），仅在 re-rank 后填充。
    """

    entity_id: int = Field(description="Bangumi 条目 ID")
    chunk_text: str = Field(description="命中文本块原文")
    name: str = Field(default="", description="条目名称")
    score: float = Field(default=0.0, description="条目评分")
    rating_total: int = Field(default=0, description="评分人数（热度信号）")
    tags: list[str] = Field(default_factory=list, description="条目标签列表")
    subject_type: int = Field(default=0, description="条目类型")
    nsfw: bool = Field(default=False, description="是否为 R18 内容")
    core_staff: list[str] = Field(default_factory=list, description="核心制作人员")
    main_cv: list[str] = Field(default_factory=list, description="主役声优")
    cosine_distance: float = Field(description="余弦距离，越小越相似")
    final_score: float = Field(
        default=0.0, description="降级重排后的综合得分，越小越好"
    )


class BangumiRetriever:
    """Bangumi 混合检索器。

    结合 PGVector 向量检索与 JSONB 元数据硬过滤，实现"语义召回
    + 精确筛选"的双擎查询管道。

    典型检索流程::

        retriever = BangumiRetriever(engine, zhipu_api_key="...")
        results = retriever.hybrid_search(
            query="有哪些高分科幻动画",
            required_tags=["科幻", "原创"],
            min_score=7.5,
            top_k=5,
        )
        for r in results:
            print(r.name, r.cosine_distance)

    Attributes:
        engine: SQLAlchemy Engine 实例。
        client: 智谱 ZhipuAiClient，用于将查询文本向量化。
    """

    def __init__(
        self,
        engine: Engine,
        zhipu_api_key: str = "",
        zhipu_base_url: str = "https://open.bigmodel.cn/api/paas/v4",
    ) -> None:
        """初始化混合检索器。

        Args:
            engine: SQLAlchemy Engine 实例。
            zhipu_api_key: 智谱 API 密钥，默认空字符串以支持尚未缴费的开发阶段。
            zhipu_base_url: 智谱 API 基础 URL。
        """
        from clients.zhipu_client import init_zhipu_client

        self.engine = engine
        self.client, init_error = init_zhipu_client(zhipu_api_key, zhipu_base_url)
        if init_error:
            logger.warning("BangumiRetriever: %s", init_error)

    # ── 公开方法 ──────────────────────────────────────────────

    def hybrid_search(
        self,
        query: str,
        required_tags: Optional[list[str]] = None,
        exclude_nsfw: bool = True,
        top_k: int = 5,
        distance_threshold: float = 0.65,
        semantic_bucket_size: float = 0.03,
    ) -> list[SearchResult]:
        """执行混合检索：SQL 硬过滤 → 向量召回 → 语义阶梯分桶排序 → 阈值防爆。

        检索管道：
          1. **SQL 硬过滤**：对 tags（JSONB @> 交集）和 nsfw 做精确 WHERE 裁剪，
             缩减候选集规模。仅过滤无效候选，不做准入门槛拦截。
          2. 将 query 向量化为 embedding。
          3. 在硬过滤后的子集中，按 PGVector 余弦距离召回 top_k * 2。
          4. **距离阈值防爆**：丢弃 cosine_distance > threshold 的候选。
          5. **语义阶梯分桶排序**：将 cosine_distance 按 ``semantic_bucket_size``
             分入语义梯队。梯队 ID 越小，语义越近。同梯队内按 rating_total 降序，
             热度仅用于同 IP 衍生作内部消歧，不破坏全局语义匹配度::

                 梯队 ID = int(cosine_distance / semantic_bucket_size)
                 排序键 = (梯队 ID 升序, -rating_total 降序)

          6. 截取 top_k 条返回。

        Args:
            query: 用户自然语言查询，如 ``"高分科幻动画"``。
                若为空字符串，返回空列表。
            required_tags: 强约束标签列表，要求命中条目必须**同时具备**
                所有标签（``@>`` 语义）。如 ``["百合", "科幻"]`` 表示
                条目 tags 中必须同时包含"百合"和"科幻"。
                为 ``None`` 或空列表时跳过标签过滤。
            exclude_nsfw: 是否排除 R18 内容，默认 ``True``（安全护栏）。
                当用户明确要求 NSFW 内容时传 ``False``。
            top_k: 最大返回条数，默认 5。
            distance_threshold: 余弦距离上限，范围 [0, 2]。超过此阈值的
                结果视为语义不相关，将被丢弃。默认 0.65。
            semantic_bucket_size: 语义梯队的步长，默认 0.03。值越小，
                梯队划分越精细，热度信号的影响范围越窄。

        Returns:
            按 final_score 升序排列的 ``SearchResult`` 列表。
            若无匹配结果或 query 为空，返回空列表。

        Example:
            >>> results = retriever.hybrid_search(
            ...     query="机战类原创动画",
            ...     required_tags=["原创", "机战"],
            ...     exclude_nsfw=True,
            ...     top_k=5,
            ... )
            >>> results[0].name
            '天元突破グレンラガン'
        """
        # ── 空查询防御 ────────────────────────────────────────
        if not query or not query.strip():
            logger.warning("查询为空，返回空列表")
            return []

        if self.client is None:
            raise RuntimeError(
                "智谱客户端未初始化，无法进行查询 embedding。"
                "请确认 zai-sdk 已安装且 API Key 有效。"
            )

        # ── Step 1: 查询向量化 ────────────────────────────────
        try:
            response = self.client.embeddings.create(
                model="embedding-3",
                input=[query.strip()],
            )
            query_embedding: list[float] = response.data[0].embedding
            logger.debug(
                "查询向量化完成: '%s' → %d 维", query[:50], len(query_embedding)
            )
        except Exception as exc:
            logger.error("查询 embedding 失败: %s", exc)
            raise RuntimeError(f"查询 embedding 失败: {exc}") from exc

        # ── Step 2: SQL 硬过滤 + 向量召回 top_k * 2 ────────────
        # 先在 SQL 层用 JSONB @> 对 tags 做交集过滤，
        # 以及 nsfw 布尔过滤，缩减候选集后再做向量排序。
        candidate_limit = top_k * 2
        distance_expr = BangumiChunk.embedding.cosine_distance(query_embedding).label(
            "cosine_dist"
        )
        try:
            with Session(self.engine) as session:
                stmt = select(BangumiChunk, distance_expr)

                # ── JSONB 硬过滤: tags ────────────────────────
                if required_tags:
                    # CAST(meta_info -> 'tags' AS JSONB) @> '["百合","科幻"]'::jsonb
                    stmt = stmt.where(
                        type_coerce(
                            BangumiChunk.meta_info["tags"],
                            JSONB,
                        ).contains(sa_cast(required_tags, JSONB))
                    )
                    logger.debug("标签硬过滤: %s", required_tags)

                # ── JSONB 硬过滤: nsfw ─────────────────────────
                if exclude_nsfw:
                    # 排除 meta_info 中包含 {"nsfw": true} 的条目
                    # ~contains → NOT (meta_info @> '{"nsfw": true}')
                    stmt = stmt.where(~BangumiChunk.meta_info.contains({"nsfw": True}))
                    logger.debug("安全护栏: 排除 nsfw=True")

                stmt = stmt.order_by(distance_expr).limit(candidate_limit)
                rows = session.execute(stmt).fetchall()

        except Exception as exc:
            logger.error("数据库查询失败: %s", exc)
            raise RuntimeError(f"混合检索查询失败: {exc}") from exc

        if not rows:
            logger.info(
                "无匹配结果: query='%s', tags=%s, exclude_nsfw=%s",
                query[:50],
                required_tags,
                exclude_nsfw,
            )
            return []

        # ── Step 3: 组装候选集（距离由 PGVector 直接返回） ──
        raw_results: list[SearchResult] = []

        for row in rows:
            chunk: BangumiChunk = row[0]
            distance: float = float(row[1])
            meta = chunk.meta_info or {}

            tags_raw = meta.get("tags", [])
            if isinstance(tags_raw, list):
                tags = [str(t) for t in tags_raw]
            else:
                tags = []

            rating_total = meta.get("rating_total", 0)
            if isinstance(rating_total, (int, float)):
                rating_total = int(rating_total)
            else:
                rating_total = 0

            raw_results.append(
                SearchResult(
                    entity_id=chunk.entity_id,
                    chunk_text=chunk.chunk_text,
                    name=meta.get("name", ""),
                    score=meta.get("score", 0.0),
                    rating_total=rating_total,
                    tags=tags,
                    subject_type=meta.get("subject_type", 0),
                    nsfw=meta.get("nsfw", False),
                    core_staff=meta.get("core_staff", []),
                    main_cv=meta.get("main_cv", []),
                    cosine_distance=distance,
                    final_score=0.0,
                )
            )

        if not raw_results:
            logger.info(
                "无候选: query='%s', tags=%s",
                query[:50],
                required_tags,
            )
            return []

        # ── Step 4: 距离阈值防爆 ──────────────────────────────
        # 先丢弃完全不相关的候选，再进入分桶排序
        within_threshold = [
            r for r in raw_results if r.cosine_distance <= distance_threshold
        ]
        discarded = len(raw_results) - len(within_threshold)
        if discarded > 0:
            logger.debug(
                "距离阈值预过滤: 丢弃 %d 条 (threshold=%.2f), 保留 %d 条",
                discarded,
                distance_threshold,
                len(within_threshold),
            )

        if not within_threshold:
            logger.info(
                "阈值过滤后无候选: query='%s', threshold=%.2f",
                query[:50],
                distance_threshold,
            )
            return []

        # ── Step 5: 语义阶梯分桶排序 ───────────────────────────
        # 梯队 ID = int(cosine_distance / bucket_size)
        # 第一主键：梯队 ID 升序（语义越近越靠前）
        # 第二主键：rating_total 降序（同梯队内热度高的优先）
        within_threshold.sort(
            key=lambda r: (
                int(r.cosine_distance / semantic_bucket_size),
                -r.rating_total,
            )
        )

        # 填充 final_score 为梯队 ID
        for r in within_threshold:
            r.final_score = float(int(r.cosine_distance / semantic_bucket_size))

        # 截取 top_k
        final_results = within_threshold[:top_k]

        logger.info(
            "分桶排序完成: query='%s', 候选=%d, 阈值保留=%d, 最终=%d, "
            "top1='%s'(distance=%.4f, bucket=%d, heat=%d)",
            query[:50],
            len(raw_results),
            len(within_threshold),
            len(final_results),
            final_results[0].name if final_results else "N/A",
            final_results[0].cosine_distance if final_results else 0,
            int(final_results[0].final_score) if final_results else -1,
            final_results[0].rating_total if final_results else 0,
        )

        return final_results


# ── 辅助函数 ──────────────────────────────────────────────────


def _compute_cosine_distance(
    vec_a: list[float],
    vec_b: list[float],
) -> float:
    """计算两个向量的余弦距离。

    余弦距离 = 1 - 余弦相似度，范围 [0, 2]。
    当任一向量为零向量或维度不匹配时返回 2.0（最大距离，视为不相似）。

    Args:
        vec_a: 向量 A。
        vec_b: 向量 B。

    Returns:
        余弦距离，0 表示完全相同，2 表示完全相反。
    """
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 2.0

    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    norm_a = sum(a * a for a in vec_a) ** 0.5
    norm_b = sum(b * b for b in vec_b) ** 0.5

    if norm_a == 0.0 or norm_b == 0.0:
        return 2.0

    # 限制在 [-1, 1] 内以防浮点溢出
    similarity = max(-1.0, min(1.0, dot / (norm_a * norm_b)))
    return 1.0 - similarity
