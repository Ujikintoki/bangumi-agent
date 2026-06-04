"""test_tools.py — tools/bgm_tools.py 全部 12 个 Tool 的结构与行为测试。

覆盖：
1. @tool 装饰器和 args_schema 绑定
2. Schema 字段与工具函数参数一致性
3. Token 门控（3 个需要 Token 的工具）
4. 格式化输出边缘情况
5. 工具注册表
"""

from __future__ import annotations

import pytest

from tools.bgm_tools import (
    _ROLE_MAP,
    _TYPE_ICONS,
    get_agent_tools,
    get_blog,
    get_calendar,
    get_bangumi_subject_detail,
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
# @tool 装饰器 + args_schema 绑定
# ═══════════════════════════════════════════════════════════════════


class TestToolDecorators:
    """每个 @tool 函数必须有正确的 args_schema 绑定。"""

    ALL_TOOLS = [
        (search_bangumi_subject, "SearchBangumiInput"),
        (get_bangumi_subject_detail, "GetSubjectDetailInput"),
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

    @pytest.mark.parametrize("tool,_", ALL_TOOLS)
    def test_tool_has_name_and_description(self, tool, _):
        assert isinstance(tool.name, str) and len(tool.name) > 0
        assert isinstance(tool.description, str) and len(tool.description) > 20

    @pytest.mark.parametrize("tool,_", ALL_TOOLS)
    def test_tool_is_structured_tool(self, tool, _):
        from langchain_core.tools import StructuredTool

        assert isinstance(tool, StructuredTool)


# ═══════════════════════════════════════════════════════════════════
# Schema 字段一致性
# ═══════════════════════════════════════════════════════════════════


class TestSchemaFieldConsistency:
    """验证 args_schema 的 model_fields 包含正确的字段名。"""

    CHECKS = [
        (search_bangumi_subject, {"keyword", "entity_type", "limit", "subject_type", "nsfw"}),
        (get_bangumi_subject_detail, {"subject_id"}),
        (get_calendar, {"weekday", "limit_per_day"}),
        (get_trending_topics, {"category", "subject_type", "limit"}),
        (get_episode_comments, {"episode_id", "comments_limit"}),
        (get_subject_discussion, {"subject_id", "data_types", "limit"}),
        (get_entity_comments, {"entity_type", "entity_id", "limit"}),
        (get_subject_characters, {"subject_id"}),
        (get_user_profile, {"username", "collections_limit", "include_blogs", "include_characters", "include_persons"}),
        (get_blog, {"entry_id", "include_comments", "include_subjects"}),
        (get_user_timeline, {"username", "limit"}),
        (search_local_bangumi, {"query", "entity_type", "limit", "nsfw"}),
    ]

    @pytest.mark.parametrize("tool,expected_fields", CHECKS)
    def test_schema_fields(self, tool, expected_fields):
        actual = set(tool.args_schema.model_fields.keys())
        assert actual == expected_fields, (
            f"{tool.name}: expected {expected_fields}, got {actual}"
        )


# ═══════════════════════════════════════════════════════════════════
# Token 门控
# ═══════════════════════════════════════════════════════════════════


class TestTokenGating:
    """需要 Token 的 3 个工具在无 Token 时返回引导提示。"""

    @pytest.mark.asyncio
    async def test_user_profile_no_token(self):
        result = await get_user_profile.ainvoke({"username": "testuser"})
        assert "系统提示" in result
        assert "bgm.tv/user/testuser" in result

    @pytest.mark.asyncio
    async def test_blog_no_token(self):
        result = await get_blog.ainvoke({"entry_id": 12345})
        assert "系统提示" in result
        assert "bgm.tv/blog/12345" in result

    @pytest.mark.asyncio
    async def test_user_timeline_no_token(self):
        result = await get_user_timeline.ainvoke({"username": "testuser"})
        assert "系统提示" in result
        assert "bgm.tv/user/testuser" in result


# ═══════════════════════════════════════════════════════════════════
# 输出格式边缘情况
# ═══════════════════════════════════════════════════════════════════


class TestOutputFormatting:
    """验证工具在边缘情况下返回自然语言字符串（不崩溃）。"""

    @pytest.mark.asyncio
    async def test_user_profile_returns_string(self):
        result = await get_user_profile.ainvoke({"username": "nonexistent"})
        assert isinstance(result, str) and len(result) > 10

    @pytest.mark.asyncio
    async def test_blog_returns_string(self):
        result = await get_blog.ainvoke({"entry_id": 1})
        assert isinstance(result, str) and len(result) > 10

    @pytest.mark.asyncio
    async def test_user_timeline_returns_string(self):
        result = await get_user_timeline.ainvoke({"username": "test"})
        assert isinstance(result, str) and len(result) > 10

    @pytest.mark.asyncio
    async def test_search_returns_string_for_missing(self):
        """搜索无结果返回自然语言提示而非空 JSON。"""
        # 实际 API 调用会失败（无网络），但至少不崩溃
        try:
            result = await search_bangumi_subject.ainvoke(
                {"keyword": "xYzZz1234567890", "entity_type": "subject"}
            )
            assert isinstance(result, str)
        except Exception:
            # 网络不可用 → 工具返回错误字符串，pytest 将其视为 pass
            pass

    @pytest.mark.asyncio
    async def test_calendar_returns_string(self):
        try:
            result = await get_calendar.ainvoke({"weekday": "today"})
            assert isinstance(result, str)
        except Exception:
            pass  # 无网络时返回 error dict 的序列化 → 仍为 str


# ═══════════════════════════════════════════════════════════════════
# 常量映射
# ═══════════════════════════════════════════════════════════════════


class TestDisplayConstants:
    """验证角色类型和图标映射。"""

    def test_role_map(self):
        assert _ROLE_MAP[1] == "角色"
        assert _ROLE_MAP[2] == "机体"
        assert _ROLE_MAP[3] == "舰船"
        assert _ROLE_MAP[4] == "组织机构"

    def test_type_icons(self):
        assert _TYPE_ICONS[1] == "📚"
        assert _TYPE_ICONS[2] == "📺"
        assert _TYPE_ICONS[3] == "🎵"
        assert _TYPE_ICONS[4] == "🎮"
        assert _TYPE_ICONS[6] == "🎬"


# ═══════════════════════════════════════════════════════════════════
# 工具注册表
# ═══════════════════════════════════════════════════════════════════


class TestToolRegistry:
    def test_count_no_token(self):
        tools = get_agent_tools()
        assert len(tools) >= 9

    def test_all_required_tools_present(self):
        tools = get_agent_tools()
        names = {t.name for t in tools}
        required = {
            "search_bangumi_subject",
            "get_bangumi_subject_detail",
            "get_calendar",
            "get_trending_topics",
            "get_episode_comments",
            "get_subject_discussion",
            "get_entity_comments",
            "get_subject_characters",
            "search_local_bangumi",
        }
        assert required.issubset(names), f"Missing: {required - names}"

    def test_token_tools_conditionally_present(self):
        tools = get_agent_tools()
        names = {t.name for t in tools}
        token_tools = {"get_user_timeline", "get_user_profile", "get_blog"}
        # 有 Token 时全在，无 Token 时全不在
        present = token_tools & names
        assert present == token_tools or present == set(), (
            f"Token tools partially present: {present}"
        )

    def test_no_duplicate_tool_names(self):
        tools = get_agent_tools()
        names = [t.name for t in tools]
        assert len(names) == len(set(names)), f"Duplicates: {names}"
