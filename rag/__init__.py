"""
RAG 模块

提供文本预处理、向量化、语义检索等检索增强生成能力。

导出:
    - BangumiTextProcessor: 文本清洗与分块
    - RagEntityIngestor: 单表多态 RAG 实体摄入器（新架构）
    - RagEntityRetriever: 多态 RAG 检索器（新架构）
    - BangumiIngestor: [DEPRECATED] 旧版摄入器
    - BangumiRetriever: [DEPRECATED] 旧版检索器

内部工具:
    - rag.utils.init_zhipu_client: 统一的智谱客户端初始化
"""

from rag.ingestion import BangumiIngestor, RagEntityIngestor
from rag.retriever import BangumiRetriever, RagEntityRetriever
from rag.text_processor import BangumiTextProcessor

__all__ = [
    "BangumiTextProcessor",
    "RagEntityIngestor",
    "RagEntityRetriever",
    "BangumiIngestor",
    "BangumiRetriever",
]
