"""
RAG 数据摄入模块

负责将预处理后的番剧文本块批量向量化并写入 PostgreSQL + pgvector，
严格遵循"正文与 Metadata 分离"的 Embedding 策略：
  - 仅对 chunk_text（纯摘要正文）做向量化
  - tags / score / subject_type 等结构化字段存入 meta_info JSON 列
  - 绝不将 tags 拼接到正文中，避免语义稀释与噪音引入
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session

from database.models import BangumiChunk

logger = logging.getLogger("bgm-agent.ingestion")


class BangumiIngestor:
    """Bangumi 数据摄入器。

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
