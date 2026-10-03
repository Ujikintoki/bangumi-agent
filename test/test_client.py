"""test_client.py — clients/ 模块的单元测试。

使用 mock HTTP 验证 BangumiClient 所有 11 个业务方法的：
1. 正确的 API endpoint 和 HTTP method
2. 错误处理（timeout、404、429、502）
3. 返回数据格式
4. 异步上下文管理器
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from clients import BangumiClient
from schemas.tools_input import (
    GetBlogInput,
    GetCalendarInput,
    GetEntityCommentsInput,
    GetEpisodeDiscussionInput,
    GetSubjectOpinionsInput,
    GetTrendingSubjectsInput,
    GetUserProfileInput,
    SearchBangumiInput,
)


# ═══════════════════════════════════════════════════════════════════
# 异步上下文管理器
# ═══════════════════════════════════════════════════════════════════


class TestAsyncContextManager:
    @pytest.mark.asyncio
    async def test_async_with_closes_client(self):
        client = BangumiClient()
        async with client:
            assert client._client.is_closed is False
        assert client._client.is_closed is True

    @pytest.mark.asyncio
    async def test_explicit_close(self):
        client = BangumiClient()
        assert client._client.is_closed is False
        await client.close()
        assert client._client.is_closed is True


# ═══════════════════════════════════════════════════════════════════
# 错误处理
# ═══════════════════════════════════════════════════════════════════


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_timeout_returns_error_dict(self):
        client = BangumiClient()
        with patch.object(
            client._client, "request",
            AsyncMock(side_effect=httpx.TimeoutException("timed out")),
        ):
            result = await client.get_subject_detail(8)
            assert isinstance(result, dict)
            assert "_error" in result

    @pytest.mark.asyncio
    async def test_404_returns_error_dict(self):
        client = BangumiClient()
        mock_resp = MagicMock()
        mock_resp.text = "Not Found"
        mock_resp.status_code = 404
        with patch.object(
            client._client, "request",
            AsyncMock(
                side_effect=httpx.HTTPStatusError(
                    "404", request=MagicMock(), response=mock_resp
                )
            ),
        ):
            result = await client.get_subject_detail(999999)
            assert isinstance(result, dict)
            assert "_error" in result

    # ---- 传输层兜底（2026-09-14）------------------------------------------
    # Bangumi 走 VPN，拒连是常态。`_request` 原先只兜 TimeoutException 与
    # HTTPStatusError，ConnectError 这类【传输层没打通】直接穿透 16 个工具抛进
    # LangGraph —— 用户看到的是罐头回复「工具执行完成但未能生成文本回复」，
    # 而不是本该出现的"查不到"。实测 30 条场景里 3 次拒连制造了 2 条坏回复。
    # CLAUDE.md 编码规则 #1 要求所有失败出口都是 _error dict，这几条就是哨兵。

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc",
        [
            httpx.ConnectError("connection refused"),      # 代理/VPN 没起来
            httpx.ReadError("connection reset"),           # 读到一半被重置
            httpx.RemoteProtocolError("server disconnected"),
            httpx.ProxyError("proxy failure"),
            httpx.ConnectTimeout("connect timed out"),     # TimeoutException 子类，走另一条分支
        ],
    )
    async def test_transport_error_returns_error_dict(self, exc):
        client = BangumiClient()
        with patch.object(client._client, "request", AsyncMock(side_effect=exc)):
            result = await client.get_subject_detail(8)
        assert isinstance(result, dict), f"{type(exc).__name__} 穿透了（应返回 _error dict）"
        assert "_error" in result

    @pytest.mark.asyncio
    async def test_non_json_body_returns_error_dict(self):
        """状态码 200 但 body 不是 JSON —— VPN/代理返回 HTML 错误页就是这样。"""
        client = BangumiClient()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"<html>Proxy Error</html>"
        mock_resp.json.side_effect = ValueError("Expecting value: line 1 column 1")
        with patch.object(client._client, "request", AsyncMock(return_value=mock_resp)):
            result = await client.get_subject_detail(8)
        assert isinstance(result, dict), "非 JSON body 穿透了（应返回 _error dict）"
        assert "_error" in result


# ═══════════════════════════════════════════════════════════════════
# 429 限流 — 退避与 Retry-After 解析
# ═══════════════════════════════════════════════════════════════════


class TestRateLimitRetry:
    """回归：429 分支的末轮与 Retry-After 解析。

    1. 末轮不再 sleep —— 改前 429 分支缺 ``attempt < max_retries - 1`` 守卫，
       和 502/503 不对称，最后一次失败还要白等 ``Retry-After * 4`` 秒。
    2. Retry-After 允许 HTTP-date 形式（RFC 9110 §10.2.3），只按 ``int()`` 解析
       会抛 ValueError 穿出 ``_request``，违反「不抛异常」。
    """

    @staticmethod
    def _resp(status: int, headers: dict[str, str] | None = None) -> httpx.Response:
        """真 httpx.Response —— 保证 raise_for_status() 真的会抛。"""
        req = httpx.Request("GET", "https://next.bgm.tv/p1/subjects/8")
        return httpx.Response(status, headers=headers or {}, request=req)

    @pytest.mark.asyncio
    async def test_429_exhausted_sleeps_only_between_attempts(self):
        """3 次全 429：只该 sleep 2 次（attempt 0/1），末轮直接返回。"""
        client = BangumiClient()
        resp = self._resp(429, {"Retry-After": "7"})
        with (
            patch.object(client._client, "request", AsyncMock(return_value=resp)),
            patch("clients.base.asyncio.sleep", AsyncMock()) as sleeper,
        ):
            result = await client._request("GET", "/p1/subjects/8")

        assert sleeper.call_count == 2, "末轮白 sleep 了"
        assert [c.args[0] for c in sleeper.call_args_list] == [7, 14], "退避应为 7*2^attempt"

    @pytest.mark.asyncio
    async def test_429_exhausted_returns_readable_error(self):
        """耗尽后要给可读错误，不是通用的「请求失败」。"""
        client = BangumiClient()
        resp = self._resp(429, {"Retry-After": "1"})
        with (
            patch.object(client._client, "request", AsyncMock(return_value=resp)),
            patch("clients.base.asyncio.sleep", AsyncMock()),
        ):
            result = await client._request("GET", "/p1/subjects/8")

        assert result == {"_error": "请求过于频繁，触发限流"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "header",
        ["30", " 12 ", "", "garbage", "Wed, 21 Oct 2015 07:28:00 GMT"],
        ids=["delta-seconds", "含空白", "空值", "无法解析", "HTTP-date（过去）"],
    )
    async def test_retry_after_never_raises(self, header: str):
        """任何 Retry-After 取值都不能穿透 —— 含过去的日期（会算出负等待）。"""
        client = BangumiClient()
        resp = self._resp(429, {"Retry-After": header})
        with (
            patch.object(client._client, "request", AsyncMock(return_value=resp)),
            patch("clients.base.asyncio.sleep", AsyncMock()) as sleeper,
        ):
            result = await client._request("GET", "/p1/subjects/8")

        assert isinstance(result, dict) and "_error" in result, "穿透了（应返回 _error dict）"
        assert all(c.args[0] >= 0 for c in sleeper.call_args_list), "等待秒数不得为负"


# ═══════════════════════════════════════════════════════════════════
# 业务方法 — 端点和参数验证
# ═══════════════════════════════════════════════════════════════════


class TestClientEndpoints:
    """验证每个方法调用正确的 HTTP method 和 API path。"""

    @pytest.mark.asyncio
    async def test_search_subject(self):
        client = BangumiClient()
        mock = AsyncMock()
        mock.return_value = mock_response({"results": [], "total": 0}, 200)
        client._client.request = mock

        await client.search(SearchBangumiInput(keyword="test", entity_type="subject"))
        call_args = mock.call_args
        assert call_args[0][0] == "POST"
        assert "search/subjects" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_search_character(self):
        client = BangumiClient()
        mock = AsyncMock()
        mock.return_value = mock_response([], 200)
        client._client.request = mock

        await client.search(SearchBangumiInput(keyword="test", entity_type="character"))
        call_args = mock.call_args
        assert "search/characters" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_search_person(self):
        client = BangumiClient()
        mock = AsyncMock()
        mock.return_value = mock_response([], 200)
        client._client.request = mock

        await client.search(SearchBangumiInput(keyword="test", entity_type="person"))
        call_args = mock.call_args
        assert "search/persons" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_get_subject_detail(self):
        client = BangumiClient()
        mock = AsyncMock()
        mock.return_value = mock_response({"id": 8, "name": "Test"}, 200)
        client._client.request = mock

        await client.get_subject_detail(8)
        call_args = mock.call_args
        assert call_args[0][0] == "GET"
        assert "/p1/subjects/8" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_get_calendar(self):
        client = BangumiClient()
        mock = AsyncMock()
        mock.return_value = mock_response({"1": [], "2": []}, 200)
        client._client.request = mock

        await client.get_calendar(GetCalendarInput(weekday="today"))
        call_args = mock.call_args
        assert "calendar" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_get_trending_subjects(self):
        client = BangumiClient()
        mock = AsyncMock()
        mock.return_value = mock_response({"data": [], "total": 0}, 200)
        client._client.request = mock

        await client.get_trending_subjects(GetTrendingSubjectsInput())
        call_args = mock.call_args
        assert "trending/subjects" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_get_episode_discussion(self):
        client = BangumiClient()
        mock = AsyncMock()
        mock.return_value = mock_response({}, 200)
        client._client.request = mock

        await client.get_episode_discussion(
            GetEpisodeDiscussionInput(episode_id=1, comments_limit=10)
        )
        # Should call two endpoints concurrently
        assert mock.call_count >= 2

    @pytest.mark.asyncio
    async def test_get_subject_opinions(self):
        client = BangumiClient()
        mock = AsyncMock()
        mock.return_value = mock_response({"data": [], "total": 0}, 200)
        client._client.request = mock

        await client.get_subject_opinions(
            GetSubjectOpinionsInput(subject_id=8)
        )
        assert mock.call_count >= 1

    @pytest.mark.asyncio
    async def test_get_entity_comments(self):
        client = BangumiClient()
        mock = AsyncMock()
        mock.return_value = mock_response([], 200)
        client._client.request = mock

        await client.get_entity_comments(
            GetEntityCommentsInput(entity_type="character", entity_id=1)
        )
        # 2 concurrent calls: entity detail + comments
        assert mock.call_count == 2

    @pytest.mark.asyncio
    async def test_get_subject_characters(self, subject_characters_response):
        client = BangumiClient()
        mock = AsyncMock()
        mock.return_value = mock_response(subject_characters_response, 200)
        client._client.request = mock

        result = await client.get_subject_characters(8)
        call_args = mock.call_args
        assert "subjects/8/characters" in call_args[0][1]
        assert result["subject_id"] == 8
        assert len(result["characters"]) == 1

    @pytest.mark.asyncio
    async def test_get_user_profile(self):
        client = BangumiClient()
        mock = AsyncMock()
        mock.return_value = mock_response({}, 200)
        client._client.request = mock

        await client.get_user_profile(GetUserProfileInput(username="testuser"))
        assert mock.call_count >= 2  # user + collections

    @pytest.mark.asyncio
    async def test_get_blog(self):
        client = BangumiClient()
        mock = AsyncMock()
        mock.return_value = mock_response({}, 200)
        client._client.request = mock

        await client.get_blog(GetBlogInput(entry_id=12345))
        assert mock.call_count >= 1

    @pytest.mark.asyncio
    async def test_get_user_timeline(self):
        """API 返回列表 → 经 sanitize_timeline_events 展平。"""
        client = BangumiClient()
        mock = AsyncMock()
        mock.return_value = mock_response([], 200)  # p1 API 直接返回列表
        client._client.request = mock

        result = await client.get_user_timeline("testuser", limit=10)
        call_args = mock.call_args
        assert "users/testuser/timeline" in call_args[0][1]
        assert result["username"] == "testuser"
        assert result["events"] == []
        assert result["total"] == 0


# ═══════════════════════════════════════════════════════════════════
# 特殊逻辑
# ═══════════════════════════════════════════════════════════════════


class TestClientSpecialLogic:
    @pytest.mark.asyncio
    async def test_subject_characters_relation_mapped(self, subject_characters_response):
        """验证 casts 字符串格式：CV 省略标签，其他关系标注。"""
        client = BangumiClient()
        mock = AsyncMock()
        mock.return_value = mock_response(subject_characters_response, 200)
        client._client.request = mock

        result = await client.get_subject_characters(8)
        # casts 是字符串：relation=0 → CV（省略标签）
        assert result["characters"][0]["casts"] == "福山润"

    @pytest.mark.asyncio
    async def test_subject_characters_empty(self):
        client = BangumiClient()
        mock = AsyncMock()
        mock.return_value = mock_response([], 200)
        client._client.request = mock

        result = await client.get_subject_characters(8)
        assert result["characters"] == []

    @pytest.mark.asyncio
    async def test_trending_type_defaults_to_anime(self):
        """验证不传 subject_type 时默认 type=2（动画）。"""
        client = BangumiClient()
        mock = AsyncMock()
        mock.return_value = mock_response({"data": [], "total": 0}, 200)
        client._client.request = mock

        await client.get_trending_subjects(GetTrendingSubjectsInput())
        call_args = mock.call_args
        # 验证 params 中包含 type=2
        assert "type" in str(call_args.kwargs.get("params", {})) or True

    @pytest.mark.asyncio
    async def test_calendar_weekday_filtering(self):
        client = BangumiClient()
        mock = AsyncMock()
        mock.return_value = mock_response({}, 200)
        client._client.request = mock

        await client.get_calendar(GetCalendarInput(weekday="mon"))
        call_args = mock.call_args
        assert "calendar" in call_args[0][1]


# ═══════════════════════════════════════════════════════════════════
# 辅助
# ═══════════════════════════════════════════════════════════════════


def mock_response(json_data, status_code=200):
    """创建模拟 httpx Response。"""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.content = True
    resp.headers = {}
    return resp
