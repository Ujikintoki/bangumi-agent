"""
Bangumi API 异步 HTTP 客户端

基于 httpx 封装，负责与 https://api.bgm.tv 通信。
所有方法均防御性捕获异常，绝不向外抛出，便于 Agent 安全调用。
"""

from __future__ import annotations

from typing import Optional

import httpx

from core.config import get_settings
from rag.Rag_schemas.bangumi import DetailedSubjectResponse, SlimSubjectResponse

BANGUMI_API_BASE = "https://api.bgm.tv"


class BangumiClient:
    """Bangumi API 异步客户端。

    管理底层 httpx.AsyncClient 的生命周期，提供搜索与条目查询接口。
    """

    def __init__(self, access_token: Optional[str] = None) -> None:
        settings = get_settings()
        user_agent = f"{settings.PROJECT_NAME}/{settings.VERSION}"

        headers = {
            "User-Agent": user_agent,
            "Content-Type": "application/json",
        }
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"

        self._client = httpx.AsyncClient(
            base_url=BANGUMI_API_BASE,
            headers=headers,
            timeout=httpx.Timeout(30.0),
        )

    async def search_subjects(
        self,
        keyword: str,
        sort: str = "match",
        limit: int = 10,
    ) -> list[SlimSubjectResponse] | dict:
        """搜索条目（对应 POST /v0/search/subjects）。

        Args:
            keyword: 搜索关键词。
            sort: 排序方式（match / heat / rank / score）。
            limit: 返回结果数量上限。

        Returns:
            反序列化后的 SlimSubjectResponse 列表，或包含错误信息的字典。
        """
        try:
            response = await self._client.post(
                "/v0/search/subjects",
                json={"keyword": keyword, "sort": sort, "limit": limit},
            )
            response.raise_for_status()
            data = response.json()
            raw_list = data.get("data", [])
            return [SlimSubjectResponse.model_validate(item) for item in raw_list]

        except httpx.TimeoutException as exc:
            return {"error": f"请求超时: {exc}", "status_code": None}
        except httpx.HTTPStatusError as exc:
            return {
                "error": f"HTTP 错误: {exc.response.text}",
                "status_code": exc.response.status_code,
            }
        except httpx.HTTPError as exc:
            return {"error": f"网络异常: {exc}", "status_code": None}

    async def get_subject(
        self,
        subject_id: int,
    ) -> DetailedSubjectResponse | dict:
        """获取条目详情（对应 GET /v0/subjects/{subject_id}）。

        Args:
            subject_id: 条目 ID。

        Returns:
            反序列化后的 DetailedSubjectResponse，或包含错误信息的字典。
        """
        try:
            response = await self._client.get(f"/v0/subjects/{subject_id}")
            response.raise_for_status()
            data = response.json()
            return DetailedSubjectResponse.model_validate(data)

        except httpx.TimeoutException as exc:
            return {"error": f"请求超时: {exc}", "status_code": None}
        except httpx.HTTPStatusError as exc:
            return {
                "error": f"HTTP 错误: {exc.response.text}",
                "status_code": exc.response.status_code,
            }
        except httpx.HTTPError as exc:
            return {"error": f"网络异常: {exc}", "status_code": None}

    async def close(self) -> None:
        """关闭底层 HTTP 连接。"""
        await self._client.aclose()

    async def __aenter__(self) -> BangumiClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()
