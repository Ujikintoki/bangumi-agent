"""test_sanitizers.py — clients/sanitizers.py 全部纯函数的单元测试。

覆盖：BBCode 剥离、噪音过滤、截断、评论清洗、搜索/详情/日历/趋势/用户收藏。
所有测试纯 CPU 运算，无网络/数据库依赖。
"""

from __future__ import annotations

import pytest
from clients.sanitizers import (
    _cn_name,
    _is_noise,
    _strip_bbcode,
    _truncate,
    sanitize_calendar,
    sanitize_comments,
    sanitize_entity_comments,
    sanitize_entity_search,
    sanitize_episode_comments,
    sanitize_search_subjects,
    sanitize_subject_comments,
    sanitize_subject_detail,
    sanitize_trending,
    sanitize_user_collections,
)


# ═══════════════════════════════════════════════════════════════════
# _strip_bbcode
# ═══════════════════════════════════════════════════════════════════


class TestStripBBCode:
    """BBCode 标签剥离 — 视觉标签去除 + 语义标签转换。"""

    # ── 视觉标签去除 ──

    @pytest.mark.parametrize(
        "input_text,expected",
        [
            ("[b]bold[/b]", "bold"),
            ("[i]italic[/i]", "italic"),
            ("[u]underline[/u]", "underline"),
            ("[s]strike[/s]", "strike"),
            ("[size=20]big[/size]", "big"),
            ("[color=red]red[/color]", "red"),
            ("[color=#ff0000]hex[/color]", "hex"),
            ("[font=SimHei]font[/font]", "font"),
            ("[align=center]centered[/align]", "centered"),
            ("[left]left[/left]", "left"),
            ("[center]center[/center]", "center"),
            ("[right]right[/right]", "right"),
            ("[b][color=red]nested[/color][/b]", "nested"),
            ("纯文本无标签", "纯文本无标签"),
            ("", ""),
        ],
    )
    def test_visual_tags_stripped(self, input_text, expected):
        assert _strip_bbcode(input_text) == expected

    def test_multiple_visual_tags_in_sentence(self):
        text = "[b]进击[/b]的[i]巨人[/i]太好看了"
        assert _strip_bbcode(text) == "进击的巨人太好看了"

    # ── 语义标签转换 ──

    def test_mask_to_spoiler_label(self):
        assert _strip_bbcode("[mask]隐藏内容[/mask]") == "【剧透】隐藏内容【/剧透】"

    def test_spoiler_to_spoiler_label(self):
        assert _strip_bbcode("[spoiler]小心[/spoiler]") == "【剧透】小心【/剧透】"

    def test_quote_with_author(self):
        result = _strip_bbcode("[quote=网友A]我也觉得[/quote]")
        assert result == "【引用 网友A】我也觉得【/引用】"

    def test_quote_without_author(self):
        result = _strip_bbcode("[quote]无作者引用[/quote]")
        assert result == "【引用】无作者引用【/引用】"

    def test_url_to_text_link(self):
        result = _strip_bbcode("[url=https://bgm.tv]Bangumi[/url]")
        assert result == "Bangumi(https://bgm.tv)"

    def test_img_removed(self):
        assert _strip_bbcode("前面[img]photo.jpg[/img]后面") == "前面后面"
        assert _strip_bbcode("[img=800,600]photo.jpg[/img]") == ""

    # ── 混合内容 ──

    def test_real_world_mixed_bbcode(self):
        text = (
            "[b]进击的巨人[/b] 最终季\n"
            "[size=18][color=red]⚠️ 剧透警告[/color][/size]\n"
            "[mask]艾伦最后变成了...[/mask]\n"
            "[quote=某网友]我也觉得这个结局很好[/quote]\n"
            "详情见 [url=https://bgm.tv/subject/8]条目页面[/url]"
        )
        result = _strip_bbcode(text)
        assert "进击的巨人" in result
        assert "剧透警告" in result
        assert "【剧透】" in result
        assert "【引用 某网友】" in result
        assert "条目页面(https://bgm.tv/subject/8)" in result
        # No raw BBCode tags remain
        assert "[b]" not in result
        assert "[/color]" not in result

    # ── 边缘情况 ──

    def test_nested_semantic_tags(self):
        text = "[quote=作者][b]加粗引用[/b]内容[/quote]"
        result = _strip_bbcode(text)
        assert "【引用 作者】" in result
        assert "加粗引用" in result
        assert "[/b]" not in result

    def test_multiline_mask(self):
        result = _strip_bbcode("[mask]第一行\n第二行[/mask]")
        assert "第一行" in result
        assert "第二行" in result


# ═══════════════════════════════════════════════════════════════════
# _is_noise
# ═══════════════════════════════════════════════════════════════════


class TestIsNoise:
    """噪音过滤器 — 短文本 + 纯数字日期 + 重复字符。"""

    @pytest.mark.parametrize("text", ["好", "a"])
    def test_single_char_is_noise(self, text):
        assert _is_noise(text) is True

    @pytest.mark.parametrize("text", ["好看", "神作", "NB", "还行", "不错"])
    def test_two_char_is_not_noise(self, text):
        assert _is_noise(text) is False

    @pytest.mark.parametrize("text", ["12345", "2024-01-01", "2024年1月1日", "  -  "])
    def test_pure_number_or_date_is_noise(self, text):
        assert _is_noise(text) is True

    @pytest.mark.parametrize("text", ["hhhhh", "。。。。", "aaaaaa", "111111"])
    def test_repeat_chars_is_noise(self, text):
        assert _is_noise(text) is True

    def test_emoji_only_is_not_noise_by_default(self):
        # 不同 emoji 不触发重复字符检测
        assert _is_noise("👍👎🤞") is False

    def test_empty_string_not_checked(self):
        # _is_noise 调用方已处理空字符串
        pass


# ═══════════════════════════════════════════════════════════════════
# _truncate
# ═══════════════════════════════════════════════════════════════════


class TestTruncate:
    def test_short_text_passes_through(self):
        assert _truncate("短文本", 100) == "短文本"

    def test_exact_boundary(self):
        text = "a" * 200
        assert len(_truncate(text, 200)) == 200
        assert not _truncate(text, 200).endswith("...")

    def test_cut_at_period(self):
        text = "第一句话。" + "第二句话很长很长很长很长很长。" * 10
        result = _truncate(text, 30)
        assert result.endswith("...")
        assert "第一句话" in result

    def test_no_period_hard_cut(self):
        text = "这是一段没有句号的文本" + "x" * 500
        result = _truncate(text, 100)
        assert len(result) <= 103  # 100 + "..."

    def test_default_max_len(self):
        text = "x" * 600
        result = _truncate(text)
        assert len(result) <= 503


# ═══════════════════════════════════════════════════════════════════
# _cn_name
# ═══════════════════════════════════════════════════════════════════


class TestCnName:
    def test_prefer_cn(self):
        assert _cn_name("テスト", "测试") == "测试"

    def test_fallback_to_name(self):
        assert _cn_name("テスト", "") == "テスト"

    def test_fallback_to_name_none(self):
        assert _cn_name("テスト", None) == "テスト"


# ═══════════════════════════════════════════════════════════════════
# sanitize_comments
# ═══════════════════════════════════════════════════════════════════


class TestSanitizeComments:
    def test_empty_input(self):
        assert sanitize_comments([], 10) == []

    def test_format_and_sort_by_reactions(self, sample_comment):
        comments = [
            {**sample_comment, "content": "热门评论", "reactions": [{"users": [1, 2, 3, 4, 5]}]},
            {**sample_comment, "content": "普通评论", "reactions": []},
            {**sample_comment, "content": "中等评论", "reactions": [{"users": [1, 2]}]},
        ]
        result = sanitize_comments(comments, 10)
        assert len(result) == 3
        # 按 reactions 降序
        assert "热门评论" in result[0]
        assert "[5]" in result[0]
        assert "中等评论" in result[1]
        assert "普通评论" in result[2]
        assert "[0]" in result[2]

    def test_bbc_code_stripped_in_content(self):
        comments = [{"content": "[b]加粗[/b]的评论", "reactions": [], "replies": 0}]
        result = sanitize_comments(comments, 10)
        assert "加粗的评论" in result[0]
        assert "[b]" not in result[0]

    def test_noise_filtered_out(self):
        comments = [
            {"content": "好", "reactions": [], "replies": 0},  # 1 char → noise
            {"content": "正常评论", "reactions": [], "replies": 0},
        ]
        result = sanitize_comments(comments, 10)
        assert len(result) == 1
        assert "正常评论" in result[0]

    def test_replies_label(self, sample_comment):
        result = sanitize_comments([sample_comment], 10)
        assert "【回复: 5条】" in result[0]

    def test_limit_truncation(self):
        comments = [
            {"content": f"comment {i}", "reactions": [], "replies": 0}
            for i in range(20)
        ]
        result = sanitize_comments(comments, 3)
        assert len(result) == 3

    def test_truncation_within_comment(self):
        long_text = "这是一条非常长的评论内容，" + "包含很多细节和信息，" * 20
        result = sanitize_comments(
            [{"content": long_text, "reactions": [], "replies": 0}], 10
        )
        assert len(result[0]) <= 220  # "[0] " + 200 chars + "..."

    def test_empty_and_missing_fields(self):
        comments = [{}, {"content": "", "reactions": None, "replies": None}]
        result = sanitize_comments(comments, 10)
        assert result == []


# ═══════════════════════════════════════════════════════════════════
# sanitize_subject_comments
# ═══════════════════════════════════════════════════════════════════


class TestSanitizeSubjectComments:
    def test_empty_input(self):
        result = sanitize_subject_comments([], 10)
        assert result["comments"] == []
        assert result["rating_distribution"] == {}
        assert result["comment_count"] == 0

    def test_rating_label_and_distribution(self):
        comments = [
            {"comment": "神作", "rate": 9, "reactions": []},
            {"comment": "还行", "rate": 5, "reactions": []},
            {"comment": "不行", "rate": 2, "reactions": []},
        ]
        result = sanitize_subject_comments(comments, 10)
        assert "[9星]" in result["comments"][0]
        assert "[5星]" in result["comments"][1]
        assert "[2星]" in result["comments"][2]
        assert result["rating_distribution"]["9-10"] == 1
        assert result["rating_distribution"]["4-6"] == 1
        assert result["rating_distribution"]["1-3"] == 1

    def test_comment_count_is_real_total(self):
        comments = [
            {"comment": "很好很好很好", "rate": 9, "reactions": []},
            {"comment": "差", "rate": 1, "reactions": []},  # 1 char, filtered
            {"comment": "还行还行还行", "rate": 5, "reactions": []},
        ]
        result = sanitize_subject_comments(comments, 10)
        assert result["comment_count"] == 3  # real total
        assert len(result["comments"]) == 2  # filtered

    def test_no_rate_shows_unrated(self):
        comments = [{"comment": "纯评论无评分", "rate": 0, "reactions": []}]
        result = sanitize_subject_comments(comments, 10)
        assert "[未评分]" in result["comments"][0]

    def test_empty_rating_dist_removed(self):
        comments = [{"comment": "只有一个评分段", "rate": 9, "reactions": []}]
        result = sanitize_subject_comments(comments, 10)
        assert "7-8" not in result["rating_distribution"]
        assert "1-3" not in result["rating_distribution"]

    def test_sort_by_reactions_desc(self):
        comments = [
            {"comment": "低热度", "rate": 7, "reactions": []},
            {"comment": "高热度评论", "rate": 8, "reactions": [{"users": [1, 2, 3, 4]}]},
        ]
        result = sanitize_subject_comments(comments, 10)
        assert "高热度评论" in result["comments"][0]

    def test_bbcode_stripped(self):
        comments = [{"comment": "[b]加粗神作[/b]", "rate": 10, "reactions": []}]
        result = sanitize_subject_comments(comments, 10)
        assert "加粗神作" in result["comments"][0]


# ═══════════════════════════════════════════════════════════════════
# sanitize_search_subjects
# ═══════════════════════════════════════════════════════════════════


class TestSanitizeSearchSubjects:
    def test_basic(self):
        raw = {"results": [{"id": 1, "name": "Test", "nameCN": "测试", "type": 2, "rating": {"score": 7.5, "rank": 100}}], "total": 1}
        result = sanitize_search_subjects(raw)
        assert result["total"] == 1
        r = result["results"][0]
        assert r["id"] == 1
        assert r["name"] == "Test"
        assert r["type_id"] == 2
        assert r["type"] == "动画"
        assert r["score"] == 7.5
        assert r["rank"] == 100

    def test_fallback_to_data_key(self):
        raw = {"data": [{"id": 1, "name": "X", "nameCN": "", "type": 1}]}
        result = sanitize_search_subjects(raw)
        assert len(result["results"]) == 1

    def test_empty(self):
        result = sanitize_search_subjects({})
        assert result["results"] == []
        assert result["total"] == 0

    def test_missing_rating(self):
        raw = {"results": [{"id": 1, "name": "X", "nameCN": "", "type": 3}]}
        result = sanitize_search_subjects(raw)
        assert result["results"][0]["score"] == 0
        assert result["results"][0]["rank"] == 0


# ═══════════════════════════════════════════════════════════════════
# sanitize_subject_detail
# ═══════════════════════════════════════════════════════════════════


class TestSanitizeSubjectDetail:
    def test_full_fields(self, full_subject):
        result = sanitize_subject_detail(full_subject)
        assert result["id"] == 8
        assert result["name"] == "コードギアス 反逆のルルーシュR2"
        assert result["type"] == "动画"
        assert result["score"] == 8.19
        assert result["rank"] == 42
        assert result["total_rating_count"] == 9438
        assert result["eps"] == 25
        assert len(result["tags"]) == 2
        assert "summary" in result
        assert "image" in result

    def test_missing_fields_get_defaults(self):
        result = sanitize_subject_detail({"id": 1})
        assert result["score"] == 0
        assert result["tags"] == []
        assert result["image"] == ""

    def test_tags_truncated_to_10(self):
        tags = [{"name": f"tag{i}", "count": i} for i in range(15)]
        result = sanitize_subject_detail({"id": 1, "tags": tags})
        assert len(result["tags"]) == 10

    def test_summary_truncated(self):
        result = sanitize_subject_detail({"id": 1, "summary": "x" * 600})
        assert len(result["summary"]) <= 503

    def test_image_fallback(self):
        result = sanitize_subject_detail(
            {"id": 1, "images": {"medium": "https://example.com/m.jpg"}}
        )
        assert result["image"] == "https://example.com/m.jpg"


# ═══════════════════════════════════════════════════════════════════
# sanitize_calendar
# ═══════════════════════════════════════════════════════════════════


class TestSanitizeCalendar:
    def test_basic(self, calendar_items):
        result = sanitize_calendar(calendar_items)
        assert len(result["items"]) == 2
        assert result["items"][0]["watchers"] == 5000  # sorted desc
        assert result["items"][1]["watchers"] == 2000
        assert "今日热门" in result["daily_summary"]

    def test_empty(self):
        result = sanitize_calendar([])
        assert result["items"] == []
        assert result["daily_summary"] == "今日无番剧放送"

    def test_watchers_on_wrapper_not_subject(self):
        """验证 watchers 从 CalendarItem 包装层提取，而非 SlimSubject。"""
        data = [{"subject": {"id": 1, "name": "A", "nameCN": "", "rating": {}}, "watchers": 999}]
        result = sanitize_calendar(data)
        assert result["items"][0]["watchers"] == 999

    def test_single_item_summary(self):
        data = [{"subject": {"id": 1, "name": "唯一", "nameCN": "", "rating": {}}, "watchers": 100}]
        result = sanitize_calendar(data)
        assert "唯一" in result["daily_summary"]


# ═══════════════════════════════════════════════════════════════════
# sanitize_trending
# ═══════════════════════════════════════════════════════════════════


class TestSanitizeTrending:
    def test_basic(self, trending_response):
        result = sanitize_trending(trending_response, "anime")
        assert len(result["items"]) == 1
        assert result["items"][0]["name"] == "Hot Anime"
        assert result["items"][0]["trending_score"] == 500
        assert "动画" in result["summary"]

    def test_empty(self):
        result = sanitize_trending({}, "anime")
        assert result["items"] == []
        assert "暂无" in result["summary"]

    def test_unknown_subject_type_label(self):
        result = sanitize_trending({"data": []}, "unknown")
        assert "unknown" in result["summary"] or "条目" in result["summary"]


# ═══════════════════════════════════════════════════════════════════
# sanitize_entity_search
# ═══════════════════════════════════════════════════════════════════


class TestSanitizeEntitySearch:
    def test_character(self, sample_character_data):
        results = sanitize_entity_search([sample_character_data], "character")
        assert results[0]["entity_type"] == "character"
        assert results[0]["role"] == "角色"
        assert results[0]["nsfw"] is False

    def test_person(self, sample_person_data):
        results = sanitize_entity_search([sample_person_data], "person")
        assert results[0]["entity_type"] == "person"
        assert results[0]["career"] == "seiyu, actor"

    def test_person_career_string_fallback(self):
        results = sanitize_entity_search(
            [{"id": 1, "name": "X", "nameCN": "", "career": "producer"}], "person"
        )
        assert results[0]["career"] == "producer"

    def test_empty_list(self):
        assert sanitize_entity_search([], "character") == []


# ═══════════════════════════════════════════════════════════════════
# sanitize_episode_comments
# ═══════════════════════════════════════════════════════════════════


class TestSanitizeEpisodeComments:
    def test_returns_dict_structure(self, sample_comment):
        result = sanitize_episode_comments([sample_comment], 10)
        assert "comments" in result
        assert "comment_count" in result
        assert result["comment_count"] == 1

    def test_empty(self):
        result = sanitize_episode_comments([], 10)
        assert result["comments"] == []
        assert result["comment_count"] == 0


# ═══════════════════════════════════════════════════════════════════
# sanitize_entity_comments
# ═══════════════════════════════════════════════════════════════════


class TestSanitizeEntityComments:
    def test_character(self, sample_comment):
        comments = [{**sample_comment, "subject": {"name": "TestChar", "nameCN": "测试角色"}}]
        result = sanitize_entity_comments(comments, 10, "character")
        assert result["entity_type"] == "character"
        assert result["entity_name"] == "测试角色"
        assert len(result["comments"]) == 1

    def test_empty(self):
        result = sanitize_entity_comments([], 10, "person")
        assert result["comments"] == []
        assert result["comment_count"] == 0
        assert result["entity_name"] == ""

    def test_no_subject_info(self, sample_comment):
        result = sanitize_entity_comments([sample_comment], 10, "character")
        # entity_name fallback
        assert isinstance(result["entity_name"], str)


# ═══════════════════════════════════════════════════════════════════
# sanitize_user_collections
# ═══════════════════════════════════════════════════════════════════


class TestSanitizeUserCollections:
    def test_display_cap(self, user_collections_data):
        result = sanitize_user_collections(user_collections_data, 30)
        assert result["total"] == 30
        assert len(result["collections"]) == 15  # capped

    def test_stats_computed_from_full_data(self, user_collections_data):
        result = sanitize_user_collections(user_collections_data, 30)
        assert result["collection_stats"]["avg_score"] == 7.0
        assert result["collection_stats"]["type_distribution"]["看过"] == 30

    def test_empty(self):
        result = sanitize_user_collections([], 10)
        assert result["collections"] == []
        assert result["total"] == 0

    def test_less_than_cap(self):
        data = [
            {"subject": {"id": 1, "name": "A", "nameCN": "", "type": 2}, "type": 2, "rate": 8}
        ]
        result = sanitize_user_collections(data, 10)
        assert len(result["collections"]) == 1
        assert result["collection_stats"]["avg_score"] == 8.0

    def test_score_distribution(self):
        data = [
            {"subject": {"id": 1, "name": "A", "nameCN": "", "type": 2}, "type": 2, "rate": 9},
            {"subject": {"id": 2, "name": "B", "nameCN": "", "type": 2}, "type": 2, "rate": 3},
            {"subject": {"id": 3, "name": "C", "nameCN": "", "type": 2}, "type": 2, "rate": 5},
        ]
        result = sanitize_user_collections(data, 10)
        sd = result["collection_stats"]["score_dist"]
        assert sd["9-10"] == 1
        assert sd["4-6"] == 1
        assert sd["1-3"] == 1

    def test_unrated_entries_not_in_score_stats(self):
        data = [
            {"subject": {"id": 1, "name": "A", "nameCN": "", "type": 2}, "type": 2, "rate": 0}
        ]
        result = sanitize_user_collections(data, 10)
        assert "avg_score" not in result["collection_stats"]
