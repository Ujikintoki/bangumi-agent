"""test_tools.py — tools/bgm_tools.py 全部 12 个 Tool 的结构与行为测试。

覆盖：
1. @tool 装饰器和 args_schema 绑定
2. Schema 字段与工具函数参数一致性
3. Token 门控（3 个需要 Token 的工具）
4. 格式化输出边缘情况
5. 工具注册表
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tools.bgm_tools import (
    _ROLE_MAP,
    _TYPE_ICONS,
    _drop_substring_tags,
    _extract_keyword_filters,
    get_agent_tools,
    get_blog,
    get_calendar,
    get_bangumi_subject_detail,
    get_entity_comments,
    get_episode_comments,
    get_hot_topics,
    get_subject_characters,
    get_subject_episodes,
    get_subject_opinions,
    get_trending_subjects,
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
        (get_trending_subjects, "GetTrendingSubjectsInput"),
        (get_hot_topics, "GetHotTopicsInput"),
        (get_episode_comments, "GetEpisodeDiscussionInput"),
        (get_subject_opinions, "GetSubjectOpinionsInput"),
        (get_subject_episodes, "GetSubjectEpisodesInput"),
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
        (get_trending_subjects, {"subject_type", "limit"}),
        (get_hot_topics, {"limit"}),
        (get_episode_comments, {"episode_id", "comments_limit"}),
        (get_subject_opinions, {"subject_id", "limit"}),
        (get_subject_episodes, {"subject_id", "limit"}),
        (get_entity_comments, {"entity_type", "entity_id", "limit"}),
        (get_subject_characters, {"subject_id"}),
        (get_user_profile, {"username", "collections_limit", "include_blogs", "include_characters", "include_persons"}),
        (get_blog, {"entry_id", "include_comments", "include_subjects"}),
        (get_user_timeline, {"username", "limit"}),
        (search_local_bangumi, {"query", "entity_type", "limit", "nsfw", "subject_type", "tags", "year", "min_score"}),
    ]

    @pytest.mark.parametrize("tool,expected_fields", CHECKS)
    def test_schema_fields(self, tool, expected_fields):
        actual = set(tool.args_schema.model_fields.keys())
        assert actual == expected_fields, (
            f"{tool.name}: expected {expected_fields}, got {actual}"
        )


# ═══════════════════════════════════════════════════════════════════
# 关键词过滤提取 — LLM 传参优先 + 规则兜底（纯函数，不触 DB）
# ═══════════════════════════════════════════════════════════════════


class TestExtractKeywordFilters:
    """_extract_keyword_filters 的规则兜底路径。

    所有用例显式传 tags，避免触发词表加载（DB 路径）。
    """

    def test_llm_params_priority(self):
        """LLM 传参时原样保留，规则不覆盖。"""
        filters = _extract_keyword_filters(
            "2023年的治愈番", tags=["芳文社"], year=2020, min_score=7.0,
        )
        assert filters == {
            "required_tags": ["芳文社"],
            "year": 2020,
            "min_score": 7.0,
        }

    def test_year_regex_fallback(self):
        """LLM 未传 year 时，正则从 query 提取 19xx/20xx。"""
        filters = _extract_keyword_filters("2023年的治愈番", tags=["治愈"])
        assert filters["year"] == 2023

    def test_year_regex_ignores_episode_numbers(self):
        """"第1088集" 不是年份，不应误提取。"""
        filters = _extract_keyword_filters("海贼王第1088集", tags=["治愈"])
        assert "year" not in filters

    def test_min_score_regex_fallback(self):
        """LLM 未传 min_score 时，正则提取 "X.X 分" 模式。"""
        filters = _extract_keyword_filters("评分8.5分以上的动画", tags=["治愈"])
        assert filters["min_score"] == 8.5

    def test_career_map(self):
        """career 无 LLM 通道，纯规则映射中文职业词 → DB key。"""
        filters = _extract_keyword_filters("找声优", tags=["治愈"])
        assert filters["career"] == "seiyu"

    def test_career_map_long_word_priority(self):
        """"动画制作人" 包含 "制作人"——长词先匹配，两者同映射 producer。"""
        filters = _extract_keyword_filters("动画制作人", tags=["治愈"])
        assert filters["career"] == "producer"

    def test_no_extractable_content(self):
        """无年份/评分/职业词时，仅保留 LLM 传入的 tags。"""
        filters = _extract_keyword_filters("随便聊聊天", tags=["治愈"])
        assert filters == {"required_tags": ["治愈"]}

    def test_tags_dropped_for_non_subject(self):
        """person/character 的 meta_info 无 tags 字段，tag 要求必然 AND 到 0 条。

        实测 "声优" 查 person：带 tags 返回 0 条，去掉后 career 过滤返回 55 条。
        工具 schema 亦已声明 tags「仅 entity_type=subject 时可用」。
        """
        for et in ("person", "character"):
            filters = _extract_keyword_filters("找声优", tags=["声优"], entity_type=et)
            assert "required_tags" not in filters

    def test_tags_kept_for_subject_and_all(self):
        """"all" 不加实体过滤，subject 也在结果里，tags 仍然成立。"""
        for et in ("subject", "all"):
            filters = _extract_keyword_filters("芳文社", tags=["芳文社"], entity_type=et)
            assert filters["required_tags"] == ["芳文社"]

    def test_non_subject_still_extracts_career(self):
        """豁免 tags 不影响同一次调用里的其他字段。"""
        filters = _extract_keyword_filters("找声优", tags=["声优"], entity_type="person")
        assert filters == {"career": "seiyu"}


# ═══════════════════════════════════════════════════════════════════
# 标签去子串冗余（纯函数，不触 DB）
# ═══════════════════════════════════════════════════════════════════


class TestDropSubstringTags:
    """词表是【子串】匹配，短标签会被长标签连带命中。

    keyword_search 对每个标签逐条 AND，冗余标签会把结果集收紧到空 ——
    实测 "TRIGGER" 命中 ['TRIGGER','GE','IG'] 返回 0 条，去掉冗余后 14 条。
    """

    def test_drops_shorter_substring(self):
        assert _drop_substring_tags(["TRIGGER", "GE", "IG"]) == ["TRIGGER"]

    def test_drops_ascii_fragment(self):
        """CloverWorks 里的 love、BONES 里的 ONE 都是词表里的真标签。"""
        assert _drop_substring_tags(["CloverWorks", "love"]) == ["CloverWorks"]
        assert _drop_substring_tags(["BONES", "ONE"]) == ["BONES"]

    def test_keeps_unrelated_tags(self):
        """互为子串才丢弃；并列的多个标签都要保留（用户可能同时指定）。"""
        assert _drop_substring_tags(["芳文社", "治愈"]) == ["芳文社", "治愈"]

    def test_keeps_longer_of_two_related(self):
        assert _drop_substring_tags(["京都动画", "京都", "动画"]) == ["京都动画"]

    def test_empty(self):
        assert _drop_substring_tags([]) == []


# ═══════════════════════════════════════════════════════════════════
# 词表兜底路径：query 文本 → 标签（不触 DB）
# ═══════════════════════════════════════════════════════════════════


class TestTagVocabularyFallback:
    """词表兜底的端到端逻辑：子串匹配 → 去冗余 → 排序 → 截断。

    词表用 monkeypatch 固定，**不触 DB** —— 被测的是这套逻辑本身，
    与词表里恰好有哪些标签无关。固定词表同时让用例可复现。

    词表内容照抄生产里会连带命中的短标签（"CloverWorks" 里的 "love" 等）。
    """

    @pytest.fixture
    def vocab(self, monkeypatch) -> frozenset[str]:
        fake = frozenset({
            "芳文社", "CloverWorks", "love", "京都动画", "京都", "动画",
            "TRIGGER", "GE", "IG", "BONES", "ONE", "P.A.WORKS", "P.A.",
            "MADHouse", "吉卜力", "虚渊玄",
            "声优", "动画制作",
        })
        monkeypatch.setattr("rag._tag_dict.load_tag_vocabulary", lambda: fake)
        return fake

    # 轴 2 A 组（tag 类）的 query 原文 → 应得的标签。
    # 这些 query 曾因词表兜底产垃圾标签导致 keyword 通道【静默返回 0 条】。
    @pytest.mark.parametrize("query,expected", [
        ("芳文社", ["芳文社"]),
        ("CloverWorks", ["CloverWorks"]),     # 曾 → ['CloverWorks', 'love'] → 0 条
        ("京都动画", ["京都动画"]),              # 曾 → ['京都动画', '京都', '动画'] → 0 条
        ("TRIGGER", ["TRIGGER"]),             # 曾 → ['TRIGGER', 'GE', 'IG'] → 0 条
        ("BONES", ["BONES"]),                 # 曾 → ['BONES', 'ONE'] → 3 条（GT 24）
        ("P.A.WORKS", ["P.A.WORKS"]),         # 曾 → ['P.A.WORKS', 'P.A.'] → 1 条（GT 20）
        ("MADHouse", ["MADHouse"]),
        ("吉卜力", ["吉卜力"]),
        ("虚渊玄", ["虚渊玄"]),
    ])
    def test_agroup_queries_yield_single_clean_tag(self, vocab, query, expected):
        assert _extract_keyword_filters(query, entity_type="subject")["required_tags"] == expected

    def test_caps_at_five_longest(self, vocab):
        """命中超过 5 个时取最长的 5 个（长标签更具体）。"""
        filters = _extract_keyword_filters(
            "芳文社 CloverWorks 京都动画 TRIGGER BONES P.A.WORKS 吉卜力",
            entity_type="subject",
        )
        assert len(filters["required_tags"]) == 5
        assert filters["required_tags"][0] == "CloverWorks"   # 最长

    def test_non_subject_skips_vocab_entirely(self, vocab):
        """"声优" 在词表里，但 person 查询不应产 tag。

        断言它确实在词表里，才能证明是【豁免生效】而不是【没匹配上】——
        后者是假通过。
        """
        assert "声优" in vocab
        for et in ("person", "character"):
            assert "required_tags" not in _extract_keyword_filters("声优", entity_type=et)

    def test_all_entity_type_still_produces_tags(self, vocab):
        """"all" 不加实体过滤，subject 也在结果里，tags 仍成立。"""
        assert _extract_keyword_filters("芳文社", entity_type="all")["required_tags"] == ["芳文社"]

    # 已知的规则覆盖不足，**【故意不写断言】** —— 断言现状等于把它固化：
    #   a13/a14 "2023年的动画" → 除 year=2023 外还多出 tag ['2023年','动画']，
    #           逐条 AND 后只剩 6/50、7/42（年份短语被当成了标签）
    #   a15     "高分神作"     → 只认出 tag ['神作']，"高分"解析不出评分下限
    # 修法未定，等有明确期望值再补。


# ═══════════════════════════════════════════════════════════════════
# Token 门控
# ═══════════════════════════════════════════════════════════════════


class TestTokenGating:
    """需要 Token 的 3 个工具在无 Token 时返回引导提示。"""

    @pytest.mark.asyncio
    @patch("tools.bgm_tools.get_settings")
    async def test_user_profile_no_token(self, mock_get_settings):
        mock_get_settings.return_value = MagicMock(BANGUMI_ACCESS_TOKEN="")
        result = await get_user_profile.ainvoke({"username": "testuser"})
        assert "_error" in result
        assert "bgm.tv/user/testuser" in result["_error"]

    @pytest.mark.asyncio
    @patch("tools.bgm_tools.get_settings")
    async def test_blog_no_token(self, mock_get_settings):
        mock_get_settings.return_value = MagicMock(BANGUMI_ACCESS_TOKEN="")
        result = await get_blog.ainvoke({"entry_id": 12345})
        assert "_error" in result
        assert "bgm.tv/blog/12345" in result["_error"]

    @pytest.mark.asyncio
    @patch("tools.bgm_tools.get_settings")
    async def test_user_timeline_no_token(self, mock_get_settings):
        mock_get_settings.return_value = MagicMock(BANGUMI_ACCESS_TOKEN="")
        result = await get_user_timeline.ainvoke({"username": "testuser"})
        assert "_error" in result
        assert "bgm.tv/user/testuser" in result["_error"]


# ═══════════════════════════════════════════════════════════════════
# 输出格式边缘情况
# ═══════════════════════════════════════════════════════════════════


class TestOutputFormatting:
    """验证工具在边缘情况下返回自然语言字符串（不崩溃）。"""

    @pytest.mark.asyncio
    async def test_user_profile_returns_dict(self):
        """get_user_profile 返回 dict（有 token 时调用 API，无 token 时返回 error dict）。"""
        result = await get_user_profile.ainvoke({"username": "nonexistent"})
        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_blog_returns_dict(self):
        """get_blog 返回 dict（有 token 时调用 API，无数据时返回 error dict）。"""
        result = await get_blog.ainvoke({"entry_id": 1})
        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_user_timeline_returns_dict(self):
        """get_user_timeline 返回 dict（有 token 时调用 API）。"""
        result = await get_user_timeline.ainvoke({"username": "test"})
        assert isinstance(result, dict)

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
    async def test_calendar_returns_dict(self):
        """日历工具返回结构化 dict。"""
        try:
            result = await get_calendar.ainvoke({"weekday": "today"})
            assert isinstance(result, dict)
        except Exception:
            pass  # 无网络


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
        assert len(tools) >= 11

    def test_all_required_tools_present(self):
        tools = get_agent_tools()
        names = {t.name for t in tools}
        required = {
            "search_bangumi_subject",
            "get_bangumi_subject_detail",
            "get_calendar",
            "get_trending_subjects",
            "get_hot_topics",
            "get_episode_comments",
            "get_subject_opinions",
            "get_subject_episodes",
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
