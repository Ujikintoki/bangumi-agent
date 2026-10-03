"""
数据库模块

提供 PostgreSQL + PGVector 的连接管理、表结构定义及 Session 生命周期控制。

导出:
    - RagEntity: 单表多态 RAG 实体模型（新架构）
    - BangumiChunk: [DEPRECATED] 旧 chunk 模型，已弃用
    - engine: SQLAlchemy Engine 单例
    - get_session: FastAPI 依赖注入用 Session 生成器
    - init_db: 数据库初始化（启用扩展 + 执行 Alembic 迁移）
"""

from database.engine import engine, get_session, init_db
from database.rag_tables import (
    BangumiChunk,
    CharacterCast,
    CharacterMeta,
    PersonMeta,
    PersonWork,
    RagEntity,
    SubjectMeta,
)

__all__ = [
    # 新架构
    "RagEntity",
    "SubjectMeta",
    "CharacterCast",
    "CharacterMeta",
    "PersonWork",
    "PersonMeta",
    # 旧架构（兼容）
    "BangumiChunk",
    # 基础设施
    "engine",
    "get_session",
    "init_db",
]
