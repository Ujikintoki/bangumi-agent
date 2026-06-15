"""
Phase 5 L2/L3 记忆系统 ORM 模型

三张表，与现有 ``rag_entities`` 共用同一个 PostgreSQL + pgvector 实例：

- ``session_memories`` — 每次对话的 LLM 摘要 + embedding，按 user_id 分区
- ``user_profiles``  — 跨 session 聚合的用户偏好画像，每个 user_id 仅一行
- ``public_memories`` — 全局社区共识快照（Phase 6 写入，Phase 5 仅建表）

设计决策：
    - UUID 主键：分布式友好、不暴露数据规模
    - session_memories.user_id 无外键约束：异步写入时 user_profile
      可能晚于 session_memory 到达，软关联（仅索引）避免 INSERT 失败
    - user_profiles 用 JSONB：画像维度会扩展，避免频繁 DDL
    - embedding 可为 NULL：embedding API 失败时仍能存储摘要文本

建表：由 ``SQLModel.metadata.create_all(engine)`` 自动执行。
索引：由 ``database/engine.py:init_db()`` 统一创建 HNSW + B-tree 索引。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from core.config import get_settings

_EMBEDDING_DIM = get_settings().EMBEDDING_DIMENSION
"""当前 embedding 模型维度，与 pgvector Vector(n) 列定义一致。"""


# ============================================================================
# L2: Session 摘要记忆
# ============================================================================


class SessionMemory(SQLModel, table=True):
    """单次对话的 LLM 摘要记忆。

    每次 Agent 返回 final_reply 后，MemoryManager 异步生成 ~200 字
    中文摘要并写入此表。摘要经 Zhipu embedding-3 向量化后存入
    ``embedding`` 列，用于后续语义检索召回。

    写入可靠性：
        - embedding API 超时 → embedding=NULL，召回时回退为按 recency 排序
        - DB 写入失败 → 仅 WARNING 日志，不阻塞用户响应
    """

    __tablename__ = "session_memories"

    __table_args__ = (
        UniqueConstraint(
            "user_id", "session_id",
            name="uq_session_memories_user_session",
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    """UUID 主键，分布式友好。"""

    session_id: str = Field(index=True)
    """会话标识。由 /chat 端点传入，用于区分同一用户的不同对话时段。"""

    user_id: str = Field(index=True)
    """用户标识。软关联 user_profiles.user_id，无外键约束（异步写入兼容）。"""

    # ── 核心摘要 ──────────────────────────────────────────────

    summary_text: str
    """LLM 生成的 ~200 字中文摘要。保留语义要点，不保留用户原始措辞。"""

    embedding: list[float] | None = Field(
        default=None,
        sa_column=Column(Vector(_EMBEDDING_DIM), nullable=True),
    )
    """摘要的向量嵌入（Zhipu embedding-3，{} 维）。可为 NULL。""".format(_EMBEDDING_DIM)

    # ── 结构化元数据 ──────────────────────────────────────────

    key_entities: dict | None = Field(default_factory=dict, sa_column=Column(JSONB))
    """对话中涉及的关键实体。

    格式::

        [
            {"type": "subject", "id": "subject_10", "name": "高达Seed"},
            {"type": "character", "id": "character_5", "name": "キラ・ヤマト"},
        ]
    """

    intent_distribution: dict | None = Field(default_factory=dict, sa_column=Column(JSONB))
    """本轮对话的意图分布。

    格式::

        {"lookup": 1, "discovery": 2}
    """

    tools_used: list[str] | None = Field(default_factory=list, sa_column=Column(JSONB))
    """本轮调用的工具名称列表（去重）。"""

    message_count: int = Field(default=0)
    """对话轮数。用于评估摘要质量和画像活跃度。"""

    # ── 时间戳 ────────────────────────────────────────────────

    created_at: datetime = Field(default_factory=datetime.utcnow)
    """摘要创建时间。用于召回排序中的时间衰减因子。"""


# ============================================================================
# L2: 用户画像
# ============================================================================


class UserProfile(SQLModel, table=True):
    """跨 session 聚合的用户偏好画像。

    每个 ``user_id`` 仅一条记录。每次 ``remember_session()``
    完成后增量更新，不重建。

    ``preferences_json`` 结构（JSONB 灵活 schema）::

        {
            "favorite_genres": [
                {"genre": "机战", "count": 12, "last_seen": "2026-06-10"},
                {"genre": "科幻", "count": 8, "last_seen": "2026-06-08"}
            ],
            "entity_affinities": {
                "subject_10": {"name": "高达Seed", "interest_score": 0.9}
            },
            "activity_profile": {
                "query_types": {"discovery": 15, "lookup": 8, "chitchat": 3},
                "total_sessions": 26
            }
        }

    注入条件：``total_sessions >= 3``（冷启动保护）。
    """

    __tablename__ = "user_profiles"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    """UUID 主键。"""

    user_id: str = Field(unique=True, index=True)
    """用户标识（唯一）。用于 UPSERT 的冲突检测键。"""

    # ── 核心画像 ──────────────────────────────────────────────

    preferences_json: dict = Field(default_factory=dict, sa_column=Column(JSONB))
    """用户偏好 JSON。维度随时间扩展，使用 JSONB 避免频繁 DDL。"""

    # ── 聚合统计（冗余列，加速常见查询）──────────────────────

    total_sessions: int = Field(default=0)
    """累计对话数。用于冷启动判断（< 3 时不注入画像）。"""

    avg_session_length: float = Field(default=0.0)
    """平均每 session 轮次。指数移动平均更新。"""

    dominant_intent: str | None = Field(default=None)
    """最频繁的意图类型（如 "discovery"）。每 N 次 session 更新一次。"""

    # ── 时间戳 ────────────────────────────────────────────────

    first_seen_at: datetime = Field(default_factory=datetime.utcnow)
    """用户首次出现时间。"""

    last_active_at: datetime = Field(default_factory=datetime.utcnow)
    """最近一次活跃时间。用于衰减/清理策略。"""

    updated_at: datetime = Field(default_factory=datetime.utcnow)
    """画像最后更新时间。"""


# ============================================================================
# L3: 公共记忆（Phase 6 写入）
# ============================================================================


class PublicMemory(SQLModel, table=True):
    """全局社区共识快照。

    不是关于"某个用户"的记忆，而是关于"Bangumi 社区整体"的记忆：
    社区热议话题、类型共识排名、周期性事件等。

    Phase 5 建表，Phase 6 实现写入管道。写入来源包括
    小组讨论分析结果、热门趋势快照、编辑精选等。
    """

    __tablename__ = "public_memories"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)

    topic: str
    """话题标题。如 "2026年6月新番满意度调查"。"""

    summary_text: str
    """摘要文本（200-500 字）。"""

    embedding: list[float] | None = Field(
        default=None,
        sa_column=Column(Vector(_EMBEDDING_DIM), nullable=True),
    )

    source_type: str
    """来源类型：'group_discussion' | 'trending' | 'editorial'。"""

    source_id: str | None = Field(default=None)
    """来源标识（group_id / topic_id）。"""

    heat_score: int = Field(default=0)
    """热度信号。用于排序和过期判断。"""

    tags: list[str] | None = Field(default_factory=list, sa_column=Column(JSONB))
    """标签列表。如 ['新番', '2026Q2']。"""

    created_at: datetime = Field(default_factory=datetime.utcnow)

    expires_at: datetime | None = Field(default=None)
    """过期时间。可由 cron job 定期清理。"""

    is_active: bool = Field(default=True)
    """是否活跃。关闭后不再召回。"""
