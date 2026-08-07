"""
RAG 模块

提供文本清洗、向量化、语义检索等检索增强生成能力。

导出:
    - RagEntityIngestor: 单表多态 RAG 实体摄入器
    - RagEntityRetriever: 多态 RAG 检索器
    - CharacterEnricher: 角色数据富化器
    - PersonEnricher: 人物数据富化器
    - SubjectCollector: 作品数据收集器
"""

from rag.enricher import CharacterEnricher, PersonEnricher, SubjectCollector
from rag.ingestion import RagEntityIngestor
from rag.retriever import RagEntityRetriever

__all__ = [
    "RagEntityIngestor",
    "RagEntityRetriever",
    "CharacterEnricher",
    "PersonEnricher",
    "SubjectCollector",
]
