"""Bangumi API 异步客户端。

统一导出 BangumiClient，底层基于 httpx + p1 private API，
提供搜索、详情、日历、热门趋势、讨论、评论、用户画像等全部业务方法。
"""

from clients.client import BangumiClient

__all__ = [
    "BangumiClient",
]
