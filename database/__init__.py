"""
数据库模块

提供 PostgreSQL + PGVector 的连接管理、表结构定义及 Session 生命周期控制。
"""

from database.database import engine, get_session, init_db
from database.models import BangumiChunk

__all__ = [
    "BangumiChunk",
    "engine",
    "get_session",
    "init_db",
]
