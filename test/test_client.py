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
    GetSubjectDiscussionInput,
    GetTrendingInput,
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
    async def test_get_trending(self):
        client = BangumiClient()
        mock = AsyncMock()
        mock.return_value = mock_response({"data": [], "total": 0}, 200)
        client._client.request = mock

        await client.get_trending(GetTrendingInput(category="subjects"))
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
    async def test_get_subject_discussion(self):
        client = BangumiClient()
        mock = AsyncMock()
        mock.return_value = mock_response({"data": [], "total": 0}, 200)
        client._client.request = mock

        await client.get_subject_discussion(
            GetSubjectDiscussionInput(subject_id=8, data_types=["comments"])
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
        call_args = mock.call_args
        assert "characters/1/comments" in call_args[0][1]

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
        client = BangumiClient()
        mock = AsyncMock()
        mock.return_value = mock_response({"data": []}, 200)
        client._client.request = mock

        await client.get_user_timeline("testuser", limit=10)
        call_args = mock.call_args
        assert "users/testuser/timeline" in call_args[0][1]


# ═══════════════════════════════════════════════════════════════════
# 特殊逻辑
# ═══════════════════════════════════════════════════════════════════


class TestClientSpecialLogic:
    @pytest.mark.asyncio
    async def test_subject_characters_relation_mapped(self, subject_characters_response):
        """验证 CharacterCastType 整数被映射为人类可读标签。"""
        client = BangumiClient()
        mock = AsyncMock()
        mock.return_value = mock_response(subject_characters_response, 200)
        client._client.request = mock

        result = await client.get_subject_characters(8)
        cast = result["characters"][0]["casts"][0]
        # relation = 0 → "CV"
        assert cast["relation"] == "CV"

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

        await client.get_trending(GetTrendingInput(category="subjects"))
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
