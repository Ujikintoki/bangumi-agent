"""
测试基础设施：配置解析 & Pydantic Schema 校验

覆盖：
  - Settings 默认值兜底
  - SlimSubjectResponse / DetailedSubjectResponse 防脏数据校验
  - BangumiClient 异常分支（Mock HTTP）
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from core.config import Settings

# ═══════════════════════════════════════════════════════════════
# Config 测试
# ═══════════════════════════════════════════════════════════════


class TestSettingsDefaults:
    """验证 Settings 各字段的默认值和类型。"""

    def test_default_project_name(self, monkeypatch):
        monkeypatch.delenv("PROJECT_NAME", raising=False)
        s = Settings(_env_file=None)  # 跳过 .env 以测纯默认值
        assert s.PROJECT_NAME == "BGM Agent"

    def test_default_version(self):
        s = Settings()
        assert s.VERSION == "0.1.0"

    def test_default_environment(self):
        s = Settings()
        assert s.ENVIRONMENT == "development"

    def test_default_database_url(self):
        s = Settings()
        assert "postgresql://" in s.DATABASE_URL
        assert "bangumidb" in s.DATABASE_URL

    def test_default_zhipu_key_empty(self, monkeypatch):
        monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
        s = Settings(_env_file=None)  # 跳过 .env 以测纯默认值
        assert s.ZHIPU_API_KEY == ""

    def test_default_embedding_dimension(self):
        s = Settings()
        assert s.EMBEDDING_DIMENSION == 2048

    def test_default_zhipu_base_url(self):
        s = Settings()
        assert "bigmodel.cn" in s.ZHIPU_BASE_URL

    def test_extra_fields_ignored(self):
        """extra="ignore" — 未声明的字段应被静默忽略。"""
        s = Settings(UNKNOWN_FIELD="should_be_ignored")
        assert not hasattr(s, "UNKNOWN_FIELD")


class TestSettingsEnvOverride:
    """验证环境变量能正确覆盖默认值。"""

    def test_env_overrides_project_name(self, monkeypatch):
        monkeypatch.setenv("PROJECT_NAME", "Test Agent")
        s = Settings()
        assert s.PROJECT_NAME == "Test Agent"

    def test_env_overrides_zhipu_key(self, monkeypatch):
        monkeypatch.setenv("ZHIPU_API_KEY", "test-key-123")
        s = Settings()
        assert s.ZHIPU_API_KEY == "test-key-123"


# ═══════════════════════════════════════════════════════════════
# Pydantic Schema 校验测试
# ═══════════════════════════════════════════════════════════════


VALID_SLIM_RAW = {
    "id": 1,
    "type": 2,
    "name": "やがて君になる",
    "name_cn": "终将成为你",
    "short_summary": "小糸侑是一名高中一年级学生...",
    "score": 7.8,
    "rank": 450,
    "tags": [
        {"name": "百合", "count": 8500},
        {"name": "校园", "count": 6000},
        {"name": "恋爱", "count": 5500},
    ],
}

VALID_DETAILED_RAW = {
    **VALID_SLIM_RAW,
    "total_episodes": 13,
    "eps": 13,
    "platform": "TV",
    "date": "2018-10-05",
    "collection": {"wish": 5000, "doing": 1200, "collect": 15000},
}


class TestSlimSubjectResponse:
    """SlimSubjectResponse 模型校验。"""

    def test_valid_data_parses(self):
        from rag.Rag_schemas.bangumi import SlimSubjectResponse

        obj = SlimSubjectResponse.model_validate(VALID_SLIM_RAW)
        assert obj.id == 1
        assert obj.name == "やがて君になる"
        assert obj.score == 7.8
        assert obj.tags == ["百合", "校园", "恋爱"]

    def test_missing_fields_get_defaults(self):
        """缺失字段应填充默认值而非崩溃。"""
        from rag.Rag_schemas.bangumi import SlimSubjectResponse

        minimal = {"id": 99}
        obj = SlimSubjectResponse.model_validate(minimal)
        assert obj.id == 99
        assert obj.name == ""  # 默认值
        assert obj.score == 0.0  # 默认值
        assert obj.tags == []  # 默认值

    def test_dirty_fields_are_ignored(self):
        """extra="ignore" — 未知字段不引发错误。"""
        from rag.Rag_schemas.bangumi import SlimSubjectResponse

        dirty = {**VALID_SLIM_RAW, "garbage": "should_be_dropped"}
        obj = SlimSubjectResponse.model_validate(dirty)
        assert obj.name == "やがて君になる"

    def test_type_coercion_fails_gracefully(self):
        """类型错误应抛出 ValidationError。"""
        from rag.Rag_schemas.bangumi import SlimSubjectResponse

        bad = {**VALID_SLIM_RAW, "id": "not_an_integer"}
        with pytest.raises(ValidationError):
            SlimSubjectResponse.model_validate(bad)

    def test_summary_fallback_from_summary_field(self):
        """short_summary 缺失时自动从 summary 字段回退。"""
        from rag.Rag_schemas.bangumi import SlimSubjectResponse

        data = {
            "id": 1,
            "name": "Test",
            "summary": "A very long summary...",
        }
        obj = SlimSubjectResponse.model_validate(data)
        assert obj.short_summary == "A very long summary..."

    def test_tags_list_of_strings_accepted(self):
        """tags 已是字符串列表时应直接使用。"""
        from rag.Rag_schemas.bangumi import SlimSubjectResponse

        data = {**VALID_SLIM_RAW, "tags": ["科幻", "原创"]}
        obj = SlimSubjectResponse.model_validate(data)
        assert obj.tags == ["科幻", "原创"]


class TestDetailedSubjectResponse:
    """DetailedSubjectResponse 模型校验。"""

    def test_valid_detailed_parses(self):
        from rag.Rag_schemas.bangumi import DetailedSubjectResponse

        obj = DetailedSubjectResponse.model_validate(VALID_DETAILED_RAW)
        assert obj.total_episodes == 13
        assert obj.platform == "TV"
        assert obj.date == "2018-10-05"
        assert obj.collection is not None
        assert obj.collection.wish == 5000

    def test_collection_missing_returns_none(self):
        """collection 缺失时默认 None。"""
        from rag.Rag_schemas.bangumi import DetailedSubjectResponse

        data = {**VALID_DETAILED_RAW}
        del data["collection"]
        obj = DetailedSubjectResponse.model_validate(data)
        assert obj.collection is None

    def test_novel_entry_missing_episodes(self):
        """书籍条目（type=1）缺失动画字段时应有默认值。"""
        from rag.Rag_schemas.bangumi import DetailedSubjectResponse

        novel = {
            "id": 123,
            "type": 1,
            "name": "とある魔術の禁書目録",
            "volumes": 55,
            "score": 7.0,
        }
        obj = DetailedSubjectResponse.model_validate(novel)
        assert obj.total_episodes == 0  # 默认值
        assert obj.volumes == 55


# ═══════════════════════════════════════════════════════════════
# BangumiClient Mock 测试
# ═══════════════════════════════════════════════════════════════


class TestBangumiClientErrors:
    """验证 BangumiClient 的异常分支正确返回错误字典而非抛出。"""

    @pytest.mark.asyncio
    async def test_search_timeout_returns_error_dict(self):
        from clients import BangumiClient

        client = BangumiClient()
        with patch.object(
            client._client,
            "request",
            AsyncMock(side_effect=__import__("httpx").TimeoutException("timed out")),
        ):
            result = await client.search(
                __import__("schemas.tools_input").SearchBangumiInput(
                    keyword="test", entity_type="subject"
                )
            )
            assert isinstance(result, dict)
            assert "_error" in result
        await client.close()

    @pytest.mark.asyncio
    async def test_get_subject_http_error_returns_error_dict(self):
        import httpx

        from clients import BangumiClient

        client = BangumiClient()
        mock_resp = MagicMock()
        mock_resp.text = "Not Found"
        mock_resp.status_code = 404
        with patch.object(
            client._client,
            "request",
            AsyncMock(
                side_effect=httpx.HTTPStatusError(
                    "404", request=MagicMock(), response=mock_resp
                )
            ),
        ):
            result = await client.get_subject_detail(999999)
            assert isinstance(result, dict)
            assert "_error" in result
        await client.close()
