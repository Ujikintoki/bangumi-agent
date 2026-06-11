"""
RAG 共享工具 — 向后兼容 re-export 桩

``init_zhipu_client()`` 已提升至 ``clients/zhipu_client.py``（Lift Shared Dependency 重构）。
本文件保留为 re-export 以兼容现有 import 路径，避免测试和旧代码断裂。

新代码请直接从 ``clients.zhipu_client`` 导入：
    >>> from clients.zhipu_client import init_zhipu_client, get_zhipu_client
"""

from __future__ import annotations

from clients.zhipu_client import (  # noqa: F401
    embed_batch,
    embed_single,
    get_zhipu_client,
    init_zhipu_client,
)
