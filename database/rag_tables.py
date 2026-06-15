"""
RAG 实体表 ORM 定义

面向 Bangumi 番剧数据的向量化存储与语义检索场景。
使用 SQLModel + pgvector 定义与 PostgreSQL 交互的表结构。

============================================================================
  架构演进: 单表多态 (Single Table Polymorphism) 三维知识图谱
============================================================================
  放弃多表 JOIN，所有 RAG 实体（Subject, Character, Person）共用同一张
  ``rag_entities`` 表，通过 ``entity_type`` 区分实体类型，通过前缀化
  主键 ``id`` 防止碰撞（如 "subject_10" / "character_5" / "person_3"）。

  入库前，使用本模块定义的 Pydantic v2 Meta 契约模型对 ``meta_info``
  JSONB 列做强类型校验，确保反范式化数据的结构一致性。
============================================================================
"""

from __future__ import annotations

import uuid
from typing import Optional

from pgvector.sqlalchemy import Vector
from pydantic import BaseModel, ConfigDict
from pydantic import Field as PydanticField
from sqlalchemy import Column
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from core.config import get_settings

# 从配置中读取 embedding 维度，与 pgvector Vector(n) 列定义保持一致
# 智谱 embedding-3 → 2048
# 备选项 OpenAI ada-002/3-small → 1536, 3-large → 3072
_EMBEDDING_DIM = get_settings().EMBEDDING_DIMENSION


# ============================================================================
# 新架构: RagEntity — 单表多态 RAG 实体
# ============================================================================
# 建表后需执行以下 DDL 以建立高性能索引（由 init_db() 统一执行）:
#
#   -- 向量索引: HNSW + cosine 距离算子
#   CREATE INDEX IF NOT EXISTS ix_rag_entities_embedding
#       ON rag_entities USING hnsw (embedding vector_cosine_ops);
#
#   -- 全文索引: GIN + trigram，加速 name / chunk_text 的模糊匹配
#   CREATE EXTENSION IF NOT EXISTS pg_trgm;
#   CREATE INDEX IF NOT EXISTS ix_rag_entities_name_trgm
#       ON rag_entities USING gin (name gin_trgm_ops);
#   CREATE INDEX IF NOT EXISTS ix_rag_entities_chunk_text_trgm
#       ON rag_entities USING gin (chunk_text gin_trgm_ops);
# ============================================================================


class RagEntity(SQLModel, table=True):
    """单表多态 RAG 实体模型。

    将 Subject（番剧）、Character（角色）、Person（现实人物）三类实体
    统一存储在同一张表中，通过 ``entity_type`` 列区分类型，通过前缀化
    主键 ``id`` 防止不同实体类型的 ID 碰撞。

    适度反范式化设计：
      - ``name`` / ``name_cn`` 提升为列级字段并建立索引，加速精确匹配。
      - 各实体特有的冗余字段和嵌套关联数据压入 ``meta_info`` JSONB 列，
        入库前由对应的 Pydantic Meta 契约模型做强类型校验。

    Attributes:
        id: 带前缀的全局唯一标识，如 ``"subject_10"`` / ``"character_5"``。
        entity_type: 实体类型标签，``"subject"`` / ``"character"`` / ``"person"``。
        name: 实体原文名称，建立 B-Tree 索引。
        name_cn: 实体中文名称，可为空。
        chunk_text: 文本块原始内容（摘要分块后的片段，非完整摘要）。
        embedding: 文本块的向量嵌入，维度 2048，由 pgvector 存储。
        meta_info: 反范式化元数据，JSONB 格式。入库前由 Pydantic 契约校验。
    """

    __tablename__ = "rag_entities"

    id: str = Field(
        primary_key=True,
        description="全局唯一标识，前缀格式: subject_10 / character_5 / person_3",
    )

    entity_type: str = Field(
        index=True,
        description="实体类型: subject / character / person",
    )

    name: str = Field(
        index=True,
        description="实体原文名称",
    )

    name_cn: Optional[str] = Field(
        default=None,
        description="实体中文名称",
    )

    nsfw: bool = Field(
        default=False,
        index=True,
        description="是否 R18 内容。所有实体类型共用，默认 False。",
    )

    chunk_text: str = Field(
        description="文本块原始内容（分块后的片段，非完整摘要）",
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
        description="反范式化元数据，入库前由 Pydantic 契约校验",
    )

    model_config = {
        "arbitrary_types_allowed": True,
    }


# ============================================================================
# Pydantic v2 Meta 契约模型 — 用于 meta_info JSONB 列的数据规范化与校验
# ============================================================================


class SubjectMeta(BaseModel):
    """番剧条目 (Subject) 的反范式化元数据契约。

    入库前将 Bangumi API 返回的评分、播出日期、标签等结构化字段
    压入此模型并 ``.model_dump()`` 后存入 ``meta_info``。
    """

    model_config = ConfigDict(extra="ignore")

    score: float = PydanticField(default=0.0, description="条目评分")
    rank: int = PydanticField(default=0, description="全站排名（越小越靠前，0=未上榜）")
    rating_total: int = PydanticField(default=0, description="评分人数（热度信号）")
    rating_count: list[int] = PydanticField(
        default_factory=lambda: [0] * 10,
        description="10 档评分分布 [1分人数, ..., 10分人数]，用于判断口碑一致性 vs 两极化",
    )
    collection: dict[int, int] = PydanticField(
        default_factory=dict,
        description="5 种收藏状态分布 {1(想看): N, 2(看过): N, 3(在看): N, 4(搁置): N, 5(抛弃): N}",
    )
    date: Optional[str] = PydanticField(
        default=None, description="播出/发售日期 YYYY-MM-DD"
    )
    year: Optional[int] = PydanticField(
        default=None, description="播出/发售年份，从 airtime.year 或 date 提取"
    )
    platform: str = PydanticField(
        default="", description="播出平台类型，如 TV / Movie / OVA / Web / 书籍 等"
    )
    eps: int = PydanticField(default=0, description="总集数/话数")
    tags: list[dict] = PydanticField(
        default_factory=list,
        description="社区标签列表，格式 [{name: str, count: int}, ...]",
    )


class CharacterCast(BaseModel):
    """角色→作品关联边。

    记录某个角色在特定作品中的出场信息，包括饰演该角色的声优/演员。

    Attributes:
        subject_id: 作品全局 ID，前缀格式 ``"subject_xxx"``。
        subject_name: 作品名称。
        person_id: 饰演者全局 ID，前缀格式 ``"person_xxx"``，可为空。
        person_name: 饰演者名称。
        role_type: 角色出场类型，1=主角 / 2=配角 / 3=客串。
    """

    model_config = ConfigDict(extra="ignore")

    subject_id: str = PydanticField(description="作品 ID，前缀格式 subject_xxx")
    subject_name: str = PydanticField(description="作品名称")
    person_id: Optional[str] = PydanticField(default=None, description="饰演者 ID")
    person_name: Optional[str] = PydanticField(default=None, description="饰演者名称")
    role_type: int = PydanticField(
        default=0, description="角色出场类型: 1=主角, 2=配角, 3=客串"
    )


class CharacterMeta(BaseModel):
    """角色实体 (Character) 的反范式化元数据契约。

    入库前将角色元信息及其出演作品列表压入此模型。
    """

    model_config = ConfigDict(extra="ignore")

    role: int = PydanticField(default=0, description="角色类型编号")
    collects: int = PydanticField(default=0, description="收藏数")
    summary: Optional[str] = PydanticField(
        default=None, description="角色简介/背景故事，来自完整角色详情 API"
    )
    info: Optional[str] = PydanticField(
        default=None, description="一句话简介，来自搜索结果或详情 API"
    )
    casts: list[CharacterCast] = PydanticField(
        default_factory=list,
        description="出演作品列表",
    )


class PersonWork(BaseModel):
    """人物→作品关联边（代表作），对应 API ``PersonWork`` schema。

    记录现实人物（声优/导演/作者等）参与某部作品的关联信息。
    API 结构为 ``{subject: SlimSubject, positions: [SubjectStaffPosition]}``，
    此处反范式化展平为 subject_id/name + positions 列表。

    Attributes:
        subject_id: 作品全局 ID，前缀格式 ``"subject_xxx"``。
        subject_name: 作品名称。
        positions: 职位列表，每项含 ``type_cn``（职位中文名）和 ``summary``。
    """

    model_config = ConfigDict(extra="ignore")

    subject_id: str = PydanticField(description="作品 ID，前缀格式 subject_xxx")
    subject_name: str = PydanticField(description="作品名称")
    positions: list[dict] = PydanticField(
        default_factory=list,
        description="职位列表，格式 [{type_cn: str, summary: str, appear_eps: str}, ...]",
    )


class PersonMeta(BaseModel):
    """现实人物实体 (Person) 的反范式化元数据契约。

    入库前将人物职业、代表作列表等压入此模型。
    """

    model_config = ConfigDict(extra="ignore")

    career: list[str] = PydanticField(
        default_factory=list,
        description="职业标签列表，如 ['seiyu', 'actor']。API 返回 careers 数组",
    )
    type: int = PydanticField(default=0, description="人物类型编号")
    collects: int = PydanticField(default=0, description="收藏数")
    summary: Optional[str] = PydanticField(
        default=None, description="人物简介，来自完整人物详情 API"
    )
    info: Optional[str] = PydanticField(
        default=None, description="一句话简介，来自搜索结果或详情 API"
    )
    works: list[PersonWork] = PydanticField(
        default_factory=list,
        description="代表作列表",
    )


# ============================================================================
# 旧架构兼容: BangumiChunk（已弃用，将在后续 Phase 中移除）
# ============================================================================


class BangumiChunk(SQLModel, table=True):
    """[DEPRECATED] 番剧文本块的向量化存储模型。

    .. deprecated::
        此模型将在后续 Phase 中移除，请迁移至 ``RagEntity``。
        新架构使用单表多态设计，通过 ``entity_type`` 区分 Subject、
        Character、Person 三类实体，主键改为前缀化字符串。

    将番剧简介、长评等文本分块后存入此表，每条记录包含原始文本
    及其对应的向量嵌入（embedding），用于 RAG 语义检索管道。
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
