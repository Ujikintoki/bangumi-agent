"""Bangumi API 数据模型。

覆盖 v0 公开 API 与 p1 private API 的完整响应契约。
"""

from rag.Rag_schemas.bangumi import (
    CastItem,
    CollectionSummary,
    DetailedSubjectResponse,
    P1CharacterResponse,
    P1PersonResponse,
    P1SubjectResponse,
    SlimSubjectResponse,
    WorkItem,
)

__all__ = [
    # v0 API
    "SlimSubjectResponse",
    "CollectionSummary",
    "DetailedSubjectResponse",
    # p1 API
    "P1SubjectResponse",
    "P1CharacterResponse",
    "P1PersonResponse",
    "CastItem",
    "WorkItem",
]
