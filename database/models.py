"""
数据库 ORM 模型 — 向后兼容 re-export 桩

RAG 实体表定义已提升至 ``database/rag_tables.py``（命名一致性重构，
与 ``database/memory_tables.py`` 对称）。

本文件保留为 re-export 以兼容现有 import 路径，避免测试和旧代码断裂。

新代码请直接从 ``database.rag_tables`` 导入：
    >>> from database.rag_tables import RagEntity, SubjectMeta, BangumiChunk
"""

from __future__ import annotations

from database.rag_tables import (  # noqa: F401
    BangumiChunk,
    CharacterCast,
    CharacterMeta,
    PersonMeta,
    PersonWork,
    RagEntity,
    SubjectMeta,
)
