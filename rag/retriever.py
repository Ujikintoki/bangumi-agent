"""
RAG 混合检索模块

实现 Vector Similarity + JSONB Metadata Hard Filtering 的双擎检索，
严格遵循"正文向量检索 → Metadata SQL 硬过滤 → 重排序"的查询管道。

检索策略：
  - 向量检索：对 query 做 embedding，在子文档池中用余弦距离召回候选集
  - 硬过滤：利用 PostgreSQL JSONB ``@>`` 操作符对 tags / score 做精确筛选
  - 绝不在 embedding 阶段混入 tags，保证向量空间纯净
"""

from __future__ import annotations

import logging
from typing import Optional

from pydantic import BaseModel, Field
from sqlalchemy import Engine, Float, type_coerce
from sqlalchemy import cast as sa_cast
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Session, select

from database.models import BangumiChunk

logger = logging.getLogger("bgm-agent.retriever")


class SearchResult(BaseModel):
    """单条混合检索结果。

    Attributes:
        entity_id: Bangumi 条目 ID，可链接到条目详情页。
        chunk_text: 命中文本块的原文内容。
        name: 条目名称（来自 meta_info）。
        score: 条目评分（来自 meta_info），0.0 表示无评分。
        tags: 条目标签列表（来自 meta_info）。
        subject_type: 条目类型（1=书籍, 2=动画, 3=音乐, 4=游戏, 6=三次元）。
        cosine_distance: PGVector 余弦距离，范围 [0, 2]，越小越相似。
    """

    entity_id: int = Field(description="Bangumi 条目 ID")
    chunk_text: str = Field(description="命中文本块原文")
    name: str = Field(default="", description="条目名称")
    score: float = Field(default=0.0, description="条目评分")
    tags: list[str] = Field(default_factory=list, description="条目标签列表")
    subject_type: int = Field(default=0, description="条目类型")
    cosine_distance: float = Field(description="余弦距离，越小越相似")


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
    ) -> None:
        """初始化混合检索器。

        Args:
            engine: SQLAlchemy Engine 实例。
            zhipu_api_key: 智谱 API 密钥，默认空字符串以支持尚未缴费的开发阶段。
        """
        self.engine = engine

        try:
            from zai import ZhipuAiClient

            self.client: ZhipuAiClient = ZhipuAiClient(api_key=zhipu_api_key)
            logger.info("检索器 ZhipuAiClient 初始化成功")
        except ImportError:
            self.client = None  # type: ignore[assignment]
            logger.warning(
                "zai-sdk 未安装，检索器 embedding 功能不可用。"
                "请执行: pip install zai-sdk"
            )
        except Exception as exc:
            self.client = None  # type: ignore[assignment]
            logger.error("检索器客户端初始化失败: %s", exc)

    # ── 公开方法 ──────────────────────────────────────────────

    def hybrid_search(
        self,
        query: str,
        required_tags: Optional[list[str]] = None,
        min_score: Optional[float] = None,
        top_k: int = 5,
    ) -> list[SearchResult]:
        """执行混合检索：向量召回 + JSONB 硬过滤。

        检索管道：
          1. 将 query 向量化为 embedding。
          2. 构建 WHERE 子句，对 tags 和 score 做 JSONB 硬过滤。
          3. 按 PGVector 余弦距离升序排列（越小越相似）。
          4. 截取 top_k 条结果并返回。

        Args:
            query: 用户自然语言查询，如 ``"高分科幻动画"``。
                若为空字符串，返回空列表。
            required_tags: 强约束标签列表，要求命中条目必须**同时具备**
                所有标签（``@>`` 语义）。如 ``["百合", "科幻"]`` 表示
                条目 tags 中必须同时包含"百合"和"科幻"。
                为 ``None`` 或空列表时跳过标签过滤。
            min_score: 最低评分阈值，仅返回评分 ≥ 此值的条目。
                为 ``None`` 时跳过评分过滤。
            top_k: 最大返回条数，默认 5。

        Returns:
            按余弦距离升序排列的 ``SearchResult`` 列表。
            若无匹配结果或 query 为空，返回空列表。

        Example:
            >>> retriever = BangumiRetriever(engine, zhipu_api_key="...")
            >>> results = retriever.hybrid_search(
            ...     query="机战类原创动画",
            ...     required_tags=["原创", "机战"],
            ...     min_score=7.0,
            ... )
            >>> len(results) <= 5
            True
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

        # ── Step 2 & 3: 构建 SQL 查询 ─────────────────────────
        try:
            with Session(self.engine) as session:
                stmt = select(BangumiChunk)

                # ── JSONB 硬过滤: tags ────────────────────────
                if required_tags:
                    # CAST(meta_info -> 'tags' AS JSONB) @> '["百合","科幻"]'::jsonb
                    # 使用 type_coerce 强制将 JSON 路径转为 JSONB，生成 @> 运算符
                    stmt = stmt.where(
                        type_coerce(
                            BangumiChunk.meta_info["tags"],
                            JSONB,
                        ).contains(sa_cast(required_tags, JSONB))
                    )
                    logger.debug("标签硬过滤: %s", required_tags)

                # ── JSONB 硬过滤: min_score ────────────────────
                if min_score is not None:
                    # (meta_info -> 'score')::float >= min_score
                    stmt = stmt.where(
                        sa_cast(
                            BangumiChunk.meta_info["score"],
                            Float,
                        )
                        >= min_score
                    )
                    logger.debug("评分硬过滤: >= %s", min_score)

                # ── 向量余弦距离排序 ───────────────────────────
                stmt = stmt.order_by(
                    BangumiChunk.embedding.cosine_distance(query_embedding)
                ).limit(top_k)

                # ── 执行查询 ───────────────────────────────────
                rows = session.exec(stmt).all()

        except Exception as exc:
            logger.error("数据库查询失败: %s", exc)
            raise RuntimeError(f"混合检索查询失败: {exc}") from exc

        # ── 无结果防御 ────────────────────────────────────────
        if not rows:
            logger.info(
                "无匹配结果: query='%s', tags=%s, min_score=%s",
                query[:50],
                required_tags,
                min_score,
            )
            return []

        # ── Step 4: 组装返回结果 ──────────────────────────────
        results: list[SearchResult] = []
        for row in rows:
            # 计算余弦距离（用于排序和展示）
            # pgvector 返回的 embedding 是 numpy 数组，
            # 不能直接用 or [] 判空（NumPy 会抛出 ValueError）。
            row_embedding: list[float] = (
                row.embedding.tolist() if row.embedding is not None else []
            )
            distance = _compute_cosine_distance(
                query_embedding,
                row_embedding,
            )

            meta = row.meta_info or {}

            results.append(
                SearchResult(
                    entity_id=row.entity_id,
                    chunk_text=row.chunk_text,
                    name=meta.get("name", ""),
                    score=meta.get("score", 0.0),
                    tags=meta.get("tags", []),
                    subject_type=meta.get("subject_type", 0),
                    cosine_distance=distance,
                )
            )

        logger.info(
            "混合检索完成: query='%s', 结果数=%d, top1_distance=%.4f",
            query[:50],
            len(results),
            results[0].cosine_distance if results else float("inf"),
        )

        return results


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
