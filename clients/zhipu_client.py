"""
智谱 AI 客户端 — Embedding 基础设施

提供统一的 ZhipuAiClient 初始化、单例管理和 embedding 封装。
由 RAG 模块（检索/摄入）和 Agent 记忆系统（MemoryManager）共享。

设计原则：
    - 初始化失败 → 返回 None + error_message（不抛异常）
    - 单例模式 → 进程内复用同一客户端连接
    - 优雅降级 → API 超时/失败返回 None，调用方自行回退
"""

from __future__ import annotations

import logging
from typing import Optional

from core.config import get_settings

logger = logging.getLogger("bgm-agent.clients.zhipu")


# ============================================================================
# 客户端初始化（移自 rag/utils.py）
# ============================================================================


def init_zhipu_client(
    api_key: str = "",
    base_url: str = "https://open.bigmodel.cn/api/paas/v4",
) -> tuple[Optional[object], Optional[str]]:
    """初始化智谱 ZhipuAiClient，统一处理导入错误和异常。

    所有 RAG 组件（检索器、摄入器）和记忆系统（MemoryManager）通过
    此函数获取客户端实例，避免分散的 try/except 初始化逻辑。

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


# ============================================================================
# 单例（供 MemoryManager 等长生命周期组件使用）
# ============================================================================

_zhipu_singleton: Optional[object] = None
_zhipu_singleton_error: Optional[str] = None
_zhipu_singleton_initialized: bool = False


def get_zhipu_client() -> Optional[object]:
    """获取进程级单例 ZhipuAiClient。

    首次调用时从 Settings 读取 API Key 并初始化客户端。
    后续调用直接返回缓存的实例。初始化失败时返回 None。

    Returns:
        ZhipuAiClient 实例，或 None（初始化失败时）。
    """
    global _zhipu_singleton, _zhipu_singleton_error, _zhipu_singleton_initialized

    if not _zhipu_singleton_initialized:
        _zhipu_singleton_initialized = True
        settings = get_settings()
        _zhipu_singleton, _zhipu_singleton_error = init_zhipu_client(
            api_key=settings.ZHIPU_API_KEY,
            base_url=settings.ZHIPU_BASE_URL,
        )
        if _zhipu_singleton_error:
            logger.warning("ZhipuAiClient 单例初始化失败: %s", _zhipu_singleton_error)

    return _zhipu_singleton


# ============================================================================
# Embedding 封装（供 RAG 和 MemoryManager 共用）
# ============================================================================


async def embed_single(
    text: str,
    client: Optional[object] = None,
    model: str = "embedding-3",
) -> Optional[list[float]]:
    """将单条文本向量化。

    Args:
        text: 待向量化的文本。
        client: ZhipuAiClient 实例。为 None 时自动获取单例。
        model: embedding 模型名，默认 embedding-3。

    Returns:
        向量（float 列表），或 None（失败时）。
    """
    zhipu = client or get_zhipu_client()
    if zhipu is None:
        logger.warning("embed_single: 智谱客户端不可用")
        return None

    try:
        response = zhipu.embeddings.create(model=model, input=text)
        return response.data[0].embedding
    except Exception as exc:
        logger.error("embed_single 失败: %s", exc)
        return None


async def embed_batch(
    texts: list[str],
    client: Optional[object] = None,
    model: str = "embedding-3",
) -> Optional[list[list[float]]]:
    """批量向量化多条文本。

    Args:
        texts: 待向量化的文本列表。
        client: ZhipuAiClient 实例。为 None 时自动获取单例。
        model: embedding 模型名，默认 embedding-3。

    Returns:
        向量列表，或 None（失败时）。
    """
    zhipu = client or get_zhipu_client()
    if zhipu is None:
        logger.warning("embed_batch: 智谱客户端不可用")
        return None

    try:
        response = zhipu.embeddings.create(model=model, input=texts)
        return [item.embedding for item in response.data]
    except Exception as exc:
        logger.error("embed_batch 失败: %s", exc)
        return None
