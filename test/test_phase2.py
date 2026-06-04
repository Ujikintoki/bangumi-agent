"""Phase 2 验收测试 — 验证所有 Tool 函数的结构与行为。

无需 PostgreSQL，仅验证：
1. Tool 装饰器和 args_schema 正确绑定
2. 函数签名和参数流转正确
3. 无 Token 时的优雅降级
4. 格式化输出结构
"""

from __future__ import annotations

import json

import pytest

from tools.bgm_tools import (
    get_agent_tools,
    get_blog,
    get_calendar,
    get_entity_comments,
    get_episode_comments,
    get_subject_characters,
    get_subject_discussion,
    get_trending_topics,
    get_user_profile,
    get_user_timeline,
    search_bangumi_subject,
    search_local_bangumi,
)


# ═══════════════════════════════════════════════════════════════════
# 基础结构测试
# ═══════════════════════════════════════════════════════════════════


class TestToolStructure:
    """验证每个 Tool 的 @tool 装饰器和 args_schema 正确绑定。"""

    ALL_TOOLS = [
        (search_bangumi_subject, "SearchBangumiInput"),
        (get_calendar, "GetCalendarInput"),
        (get_trending_topics, "GetTrendingInput"),
        (get_episode_comments, "GetEpisodeDiscussionInput"),
        (get_subject_discussion, "GetSubjectDiscussionInput"),
        (get_entity_comments, "GetEntityCommentsInput"),
        (get_subject_characters, "GetSubjectCharactersInput"),
        (get_user_profile, "GetUserProfileInput"),
        (get_blog, "GetBlogInput"),
        (get_user_timeline, "UserTimelineInput"),
        (search_local_bangumi, "LocalSearchInput"),
    ]

    @pytest.mark.parametrize("tool,expected_schema", ALL_TOOLS)
    def test_tool_has_correct_args_schema(self, tool, expected_schema):
        assert hasattr(tool, "args_schema"), f"{tool.name} missing args_schema"
        assert tool.args_schema is not None, f"{tool.name} args_schema is None"
        assert tool.args_schema.__name__ == expected_schema, (
            f"{tool.name}: expected {expected_schema}, got {tool.args_schema.__name__}"
        )

    def test_registry_count(self):
        tools = get_agent_tools()
        assert len(tools) >= 9, f"No-token tools count: expected >=9, got {len(tools)}"


# ═══════════════════════════════════════════════════════════════════
# 无 Token 降级测试
# ═══════════════════════════════════════════════════════════════════


class TestTokenGating:
    """验证需要 Token 的工具在无 Token 时返回引导提示。"""

    @pytest.mark.asyncio
    async def test_user_profile_no_token_returns_guidance(self):
        result = await get_user_profile.ainvoke({"username": "testuser"})
        assert "系统提示" in result
        assert "BANGUMI_ACCESS_TOKEN" in result or "Access Token" in result
        assert "bgm.tv/user/testuser" in result

    @pytest.mark.asyncio
    async def test_blog_no_token_returns_guidance(self):
        result = await get_blog.ainvoke({"entry_id": 12345})
        assert "系统提示" in result
        assert "bgm.tv/blog/12345" in result

    @pytest.mark.asyncio
    async def test_user_timeline_no_token_returns_guidance(self):
        result = await get_user_timeline.ainvoke({"username": "testuser"})
        assert "系统提示" in result
        assert "bgm.tv/user/testuser" in result


# ═══════════════════════════════════════════════════════════════════
# Tool 函数签名测试 — 验证参数名与 Schema 字段名一致
# ═══════════════════════════════════════════════════════════════════
# 注：LangChain 的 @tool(args_schema=...) 装饰器会在运行时自动校验
# 参数名是否匹配 Schema 字段名。此处仅做静态检查：确认 schema 的
# model_fields 存在且可访问。
# ═══════════════════════════════════════════════════════════════════


class TestFunctionSignatures:
    """验证每个 tool 的 args_schema 定义了正确的字段。"""

    TOOL_SCHEMA_CHECKS = [
        (get_episode_comments, {"episode_id", "comments_limit"}),
        (get_trending_topics, {"category", "subject_type", "limit"}),
        (search_bangumi_subject, {"keyword", "entity_type", "limit", "subject_type", "nsfw"}),
        (get_user_profile, {"username", "collections_limit", "include_blogs", "include_characters", "include_persons"}),
        (get_blog, {"entry_id", "include_comments", "include_subjects"}),
        (get_subject_discussion, {"subject_id", "data_types", "limit"}),
        (get_entity_comments, {"entity_type", "entity_id", "limit"}),
        (get_subject_characters, {"subject_id"}),
    ]

    @pytest.mark.parametrize("tool,expected_fields", TOOL_SCHEMA_CHECKS)
    def test_schema_has_expected_fields(self, tool, expected_fields):
        schema_fields = set(tool.args_schema.model_fields.keys())
        assert schema_fields == expected_fields, (
            f"{tool.name}: expected {expected_fields}, got {schema_fields}"
        )


# ═══════════════════════════════════════════════════════════════════
# 格式化输出测试 — 验证空数据/错误路径的优雅降级
# ═══════════════════════════════════════════════════════════════════


class TestFormattingEdgeCases:
    """验证工具在空数据和错误路径下的输出格式。"""

    @pytest.mark.asyncio
    async def test_user_profile_empty_result(self):
        """无 Token 时返回自然语言提示，而非崩溃。"""
        result = await get_user_profile.ainvoke({"username": "nonexistent"})
        assert isinstance(result, str)
        assert len(result) > 10

    @pytest.mark.asyncio
    async def test_blog_empty_result(self):
        result = await get_blog.ainvoke({"entry_id": 1})
        assert isinstance(result, str)
        assert len(result) > 10

    @pytest.mark.asyncio
    async def test_user_timeline_empty_result(self):
        result = await get_user_timeline.ainvoke({"username": "test"})
        assert isinstance(result, str)
        assert len(result) > 10


# ═══════════════════════════════════════════════════════════════════
# Entity type 常量测试
# ═══════════════════════════════════════════════════════════════════


class TestDisplayConstants:
    """验证角色类型和图标映射正确。"""

    def test_role_map_coverage(self):
        from tools.bgm_tools import _ROLE_MAP

        assert _ROLE_MAP[1] == "角色"
        assert _ROLE_MAP[2] == "机体"
        assert _ROLE_MAP[3] == "舰船"
        assert _ROLE_MAP[4] == "组织机构"

    def test_type_icons_coverage(self):
        from tools.bgm_tools import _TYPE_ICONS

        assert _TYPE_ICONS[1] == "📚"
        assert _TYPE_ICONS[2] == "📺"
        assert _TYPE_ICONS[3] == "🎵"
        assert _TYPE_ICONS[4] == "🎮"
        assert _TYPE_ICONS[6] == "🎬"


# ═══════════════════════════════════════════════════════════════════
# Client 方法接口测试 — 验证 Session A 的契约
# ═══════════════════════════════════════════════════════════════════


class TestClientContracts:
    """验证 BangumiClient 新增方法的返回格式。"""

    def test_get_subject_characters_exists(self):
        from clients import BangumiClient

        assert hasattr(BangumiClient, "get_subject_characters")
        import inspect

        sig = inspect.signature(BangumiClient.get_subject_characters)
        assert "subject_id" in sig.parameters

    def test_get_user_timeline_exists(self):
        from clients import BangumiClient

        assert hasattr(BangumiClient, "get_user_timeline")

    def test_get_subject_detail_exists(self):
        from clients import BangumiClient

        assert hasattr(BangumiClient, "get_subject_detail")


# ═══════════════════════════════════════════════════════════════════
# Schema 一致性测试 — 验证 schemas/tools_input 导出完整
# ═══════════════════════════════════════════════════════════════════


class TestSchemaAvailability:
    """验证所有 12 个 Schema 在 schemas/tools_input 中可用。"""

    EXPECTED_SCHEMAS = [
        "SearchBangumiInput",
        "GetSubjectDetailInput",
        "GetCalendarInput",
        "GetTrendingInput",
        "GetEpisodeDiscussionInput",
        "GetSubjectDiscussionInput",
        "GetEntityCommentsInput",
        "GetSubjectCharactersInput",
        "GetUserProfileInput",
        "GetBlogInput",
        "LocalSearchInput",
        "UserTimelineInput",
    ]

    def test_all_schemas_importable(self):
        from schemas import tools_input as ti

        for name in self.EXPECTED_SCHEMAS:
            assert hasattr(ti, name), f"Missing schema: {name}"

    def test_all_schemas_in_init(self):
        from schemas import __all__ as exported

        for name in self.EXPECTED_SCHEMAS:
            assert name in exported, f"Schema {name} not in __all__"
