"""外部 API 客户端。

提供：
- BangumiClient: Bangumi API 业务客户端（搜索、详情、日历、热门趋势、讨论、评论、用户画像等）
- Zhipu 客户端: 智谱 Embedding 客户端初始化与单例管理（RAG 和记忆系统共享）
"""

from clients.client import BangumiClient
from clients.zhipu_client import (  # noqa: F401
    embed_batch,
    embed_single,
    get_zhipu_client,
    init_zhipu_client,
)

__all__ = [
    "BangumiClient",
    "init_zhipu_client",
    "get_zhipu_client",
    "embed_single",
    "embed_batch",
]
