"""
RAG 模块共享工具函数。

避免 embedding 客户端初始化逻辑在 retriever / ingestion 之间重复。
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("bgm-agent.rag.utils")


def init_zhipu_client(
    api_key: str = "",
    base_url: str = "https://open.bigmodel.cn/api/paas/v4",
) -> tuple[Optional[object], Optional[str]]:
    """初始化智谱 ZhipuAiClient，统一处理导入错误和异常。

    所有 RAG 组件（检索器、摄入器）通过此函数获取客户端实例，
    避免 4 处重复的 try/except 初始化逻辑。

    Args:
        api_key: 智谱 API 密钥。
        base_url: 智谱 API 基础 URL。

    Returns:
        ``(client, None)`` 初始化成功；``(None, error_message)`` 失败。
    """
    try:
        from zai import ZhipuAiClient

        client = ZhipuAiClient(api_key=api_key, base_url=base_url)
        logger.info("ZhipuAiClient 初始化成功")
        return client, None
    except ImportError:
        msg = "zai-sdk 未安装，embedding 功能不可用。请执行: pip install zai-sdk"
        logger.warning(msg)
        return None, msg
    except Exception as exc:
        msg = f"智谱客户端初始化失败: {exc}"
        logger.error(msg)
        return None, msg
