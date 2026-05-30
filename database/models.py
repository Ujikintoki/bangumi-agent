"""
数据库 ORM 模型定义

使用 SQLModel + pgvector 定义与 PostgreSQL 交互的表结构。
面向 Bangumi 番剧数据的向量化存储与语义检索场景。
"""

import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import Column
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from core.config import get_settings

# 从配置中读取 embedding 维度，与 pgvector Vector(n) 列定义保持一致
# 智谱 embedding-3 → 2048
# 备选项 OpenAI ada-002/3-small → 1536, 3-large → 3072
_EMBEDDING_DIM = get_settings().EMBEDDING_DIMENSION


class BangumiChunk(SQLModel, table=True):
    """番剧文本块的向量化存储模型。

    将番剧简介、长评等文本分块后存入此表，每条记录包含原始文本
    及其对应的向量嵌入（embedding），用于 RAG 语义检索管道。

    Attributes:
        id: 主键，UUID v4，由数据库自动生成。
        entity_type: 条目类型标识，如 "subject"（番剧）、"character"（角色），建立索引。
        entity_id: Bangumi 官方对应的条目 ID，建立索引，与 entity_type 组合可唯一定位实体。
        chunk_text: 原始文本块内容。
        embedding: 文本块的向量嵌入，维度由 EMBEDDING_DIMENSION 配置决定。
        meta_info: 扩展元数据（如来源、分块序号、更新时间等），以 JSON 格式存储。
    """

    __tablename__ = "bangumi_chunks"

    id: uuid.UUID = Field(
        default_factory=uuid.uuid4,
        primary_key=True,
        description="主键，UUID v4",
    )

    entity_type: str = Field(
        index=True,
        description="条目类型，如 subject / character，与 entity_id 组合定位实体",
    )

    entity_id: int = Field(
        index=True,
        description="Bangumi 官方对应的条目 ID",
    )

    chunk_text: str = Field(
        description="文本块原始内容",
    )

    embedding: list[float] = Field(
        default=None,
        sa_column=Column(
            Vector(_EMBEDDING_DIM),
            nullable=True,
        ),
        description=f"向量嵌入，维度 {_EMBEDDING_DIM}，建表后由应用层填充",
    )

    meta_info: dict = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=True),
        description="扩展元数据（来源、分块序号、时间戳等），JSON 格式存储",
    )

    model_config = {
        "arbitrary_types_allowed": True,
    }
