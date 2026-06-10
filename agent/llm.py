"""
LLM 工厂模块

提供统一的多 Provider LLM 初始化接口，支持：
- OpenAI（默认）
- Azure OpenAI
- DeepSeek、Qwen 等所有 OpenAI SDK 兼容 API

设计原则：单一入口 create_llm()，通过 Settings 判断 provider 模式。
"""

from __future__ import annotations

import logging
import os
from typing import Any

from langchain_openai import AzureChatOpenAI, ChatOpenAI

from core.config import Settings, get_settings

logger = logging.getLogger("bgm-agent.llm")

# 为 create_llm 暴露的公开参数名
_DEFAULT_TEMPERATURE = 0.3
_DEFAULT_MAX_TOKENS = 4096


def create_llm(
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
    model: str | None = None,
    request_timeout: float | None = None,
    settings: Settings | None = None,
    **kwargs: Any,
) -> ChatOpenAI:
    """创建 ChatOpenAI 实例，自动适配 provider。

    判断逻辑（优先级从高到低）：
        1. 若 LLM_AZURE_ENDPOINT 或 AZURE_OPENAI_ENDPOINT 有值 → Azure 模式
        2. 若 LLM_BASE_URL 有值 → 自定义 endpoint 模式（DeepSeek / Qwen）
        3. 否则 → 标准 OpenAI 模式

    API Key 查找顺序（适配多种环境变量命名）：
        settings.LLM_API_KEY → os.environ["OPENAI_API_KEY"]
        → os.environ["AZURE_OPENAI_API_KEY"]

    Args:
        temperature: 温度参数。None 时使用 Settings.LLM_TEMPERATURE。
        max_tokens: 最大输出 Token。None 时使用 Settings.LLM_MAX_TOKENS。
        model: 模型名/部署名。None 时使用 Settings.LLM_MODEL。
        request_timeout: HTTP 请求超时（秒）。None 时使用 Settings.LLM_REQUEST_TIMEOUT。
            轻量场景（如意图分类）可传入更短的超时（如 10s）。
        settings: Settings 实例。None 时调用 get_settings()。
        **kwargs: 透传给 ChatOpenAI 的额外参数。

    Returns:
        配置好的 ChatOpenAI 实例。

    Raises:
        ValueError: 所有来源均未找到 API Key 时抛出。
    """
    if settings is None:
        settings = get_settings()

    # ── 解析 API Key ───────────────────────────────────────
    api_key = _resolve_api_key(settings)

    # ── 解析模型名 ─────────────────────────────────────────
    resolved_model = model or settings.LLM_MODEL

    # ── 解析温度 / max_tokens / timeout ─────────────────────
    resolved_temperature = temperature if temperature is not None else settings.LLM_TEMPERATURE
    resolved_max_tokens = max_tokens or settings.LLM_MAX_TOKENS
    resolved_timeout = request_timeout if request_timeout is not None else settings.LLM_REQUEST_TIMEOUT

    # ── Azure 模式 ─────────────────────────────────────────
    azure_endpoint = settings.LLM_AZURE_ENDPOINT or os.environ.get("AZURE_OPENAI_ENDPOINT", "")
    if azure_endpoint:
        azure_api_version = settings.LLM_AZURE_API_VERSION or os.environ.get(
            "AZURE_OPENAI_API_VERSION", "2024-10-21"
        )
        logger.info(
            "create_llm: Azure mode — endpoint=%s, deployment=%s, api_version=%s",
            azure_endpoint,
            resolved_model,
            azure_api_version,
        )
        return AzureChatOpenAI(
            azure_endpoint=azure_endpoint.rstrip("/"),
            deployment_name=resolved_model,
            openai_api_version=azure_api_version,
            openai_api_key=api_key,
            temperature=resolved_temperature,
            max_tokens=resolved_max_tokens,
            request_timeout=resolved_timeout,
            **kwargs,
        )

    # ── 自定义 base_url 模式（DeepSeek / Qwen / ...） ─────
    base_url = settings.LLM_BASE_URL
    if base_url:
        logger.info(
            "create_llm: custom endpoint — base_url=%s, model=%s",
            base_url,
            resolved_model,
        )
        return ChatOpenAI(
            model=resolved_model,
            base_url=base_url,
            api_key=api_key,
            temperature=resolved_temperature,
            max_tokens=resolved_max_tokens,
            request_timeout=resolved_timeout,
            **kwargs,
        )

    # ── 标准 OpenAI 模式 ───────────────────────────────────
    logger.info("create_llm: OpenAI mode — model=%s", resolved_model)
    return ChatOpenAI(
        model=resolved_model,
        api_key=api_key,
        temperature=resolved_temperature,
        max_tokens=resolved_max_tokens,
        request_timeout=resolved_timeout,
        **kwargs,
    )


def _resolve_api_key(settings: Settings) -> str:
    """按优先级查找 API Key。

    优先级：
        1. settings.LLM_API_KEY
        2. os.environ["OPENAI_API_KEY"]
        3. os.environ["AZURE_OPENAI_API_KEY"]

    Returns:
        API Key 字符串。

    Raises:
        ValueError: 所有来源均未找到 API Key。
    """
    # 1. Settings 中的显式配置
    if settings.LLM_API_KEY:
        return settings.LLM_API_KEY

    # 2. 标准 OpenAI 环境变量
    key = os.environ.get("OPENAI_API_KEY", "")
    if key:
        return key

    # 3. Azure 环境变量
    key = os.environ.get("AZURE_OPENAI_API_KEY", "")
    if key:
        return key

    raise ValueError(
        "未找到 LLM API Key。请设置以下任一环境变量或 .env 字段：\n"
        "  - LLM_API_KEY\n"
        "  - OPENAI_API_KEY\n"
        "  - AZURE_OPENAI_API_KEY"
    )
