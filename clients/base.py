"""
Bangumi API HTTP 基础设施

职责：管理 httpx.AsyncClient 会话生命周期、认证注入、重试策略。
此层不关心业务逻辑，纯属网络通信工具。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from core.config import get_settings

logger = logging.getLogger("bgm-agent.client.base")

P1_BASE_URL = "https://next.bgm.tv/p1"
"""p1 private API 基底 URL，所有工具方法基于此构建。"""

USER_AGENT = "BangumiAgent/0.1.0 (https://github.com/Ujikintoki/bangumi-agent)"
"""遵循 Bangumi 社区规范的自定义 User-Agent。"""


class BaseClient:
    """HTTP 基础设施基类。

    封装要点：
      - 统一的 User-Agent（Bangumi 要求所有第三方客户端必须设置）
      - Bearer Token 注入（从 Settings 自动读取）
      - 指数退避重试（对 429 / 502 / 503 / Timeout）
      - 请求日志（仅打印 path + status，不打印 body）
    """

    def __init__(self, access_token: str | None = None) -> None:
        settings = get_settings()
        headers: dict[str, str] = {
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
        }
        token = access_token or settings.BANGUMI_ACCESS_TOKEN
        if token:
            headers["Authorization"] = f"Bearer {token}"

        self._client = httpx.AsyncClient(
            base_url=P1_BASE_URL,
            headers=headers,
            timeout=httpx.Timeout(30.0, connect=10.0),
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """带重试的通用 HTTP 请求。

        重试条件: 状态码 429、502、503 或 TimeoutException
        最大重试: 3 次
        退避策略: sleep(1 * 2^attempt) 递增，429 时优先取 Retry-After 头
        """
        max_retries = 3
        last_error: dict[str, Any] = {"_error": f"请求失败 (path={path})"}

        for attempt in range(max_retries):
            try:
                response = await self._client.request(method, path, **kwargs)
                logger.debug("[%s] %s → %d", method, path, response.status_code)

                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", "5"))
                    wait = retry_after * (2**attempt)
                    logger.warning(
                        "429 限流，%d 秒后重试 (attempt=%d)", wait, attempt + 1
                    )
                    await asyncio.sleep(wait)
                    continue

                if response.status_code in (502, 503) and attempt < max_retries - 1:
                    wait = 2 * (2**attempt)
                    logger.warning(
                        "%d 服务端错误，%d 秒后重试 (attempt=%d)",
                        response.status_code,
                        wait,
                        attempt + 1,
                    )
                    await asyncio.sleep(wait)
                    continue

                response.raise_for_status()
                # 204 No Content 等无 body 响应返回空字典
                if response.status_code == 204 or not response.content:
                    return {}
                return response.json()

            except httpx.TimeoutException:
                if attempt < max_retries - 1:
                    wait = 1 * (2**attempt)
                    logger.warning(
                        "请求超时，%d 秒后重试 (attempt=%d)", wait, attempt + 1
                    )
                    await asyncio.sleep(wait)
                    continue
                last_error = {"_error": f"请求超时 (path={path})"}

            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status in (502, 503) and attempt < max_retries - 1:
                    wait = 2 * (2**attempt)
                    logger.warning(
                        "%d 服务端错误，%d 秒后重试 (attempt=%d)",
                        status,
                        wait,
                        attempt + 1,
                    )
                    await asyncio.sleep(wait)
                    continue
                return self._handle_http_error(path, status)

        return last_error

    async def _get(self, path: str, **kwargs: Any) -> dict[str, Any]:
        """GET 请求委托。"""
        return await self._request("GET", path, **kwargs)

    async def _post(self, path: str, **kwargs: Any) -> dict[str, Any]:
        """POST 请求委托。"""
        return await self._request("POST", path, **kwargs)

    @staticmethod
    def _handle_http_error(path: str, status: int) -> dict[str, Any]:
        """将 HTTP 状态码转译为可读的错误消息。"""
        errors: dict[int, str] = {
            404: f"未找到资源 (path={path})",
            401: "认证失败，Access Token 可能已过期",
            403: "无权限访问该资源",
            500: "Bangumi 服务器内部错误",
        }
        return {"_error": errors.get(status, f"HTTP {status} (path={path})")}

    async def close(self) -> None:
        """关闭底层 HTTP 客户端。"""
        await self._client.aclose()
