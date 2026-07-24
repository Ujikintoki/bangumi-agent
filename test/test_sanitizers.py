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
    sanitize_discussion_topics,
    sanitize_entity_comments,
    sanitize_entity_search,
    sanitize_episode_comments,
    sanitize_episode_detail,
    sanitize_reviews,
    sanitize_search_subjects,
    sanitize_subject_characters,
    sanitize_subject_comments,
    sanitize_subject_detail,
    sanitize_subject_episodes,
    sanitize_trending,
    sanitize_trending_topics,
    sanitize_user_collections,
    sanitize_user_stats,
    sanitize_timeline_events,
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

    def test_chronological_order_and_no_reaction_prefix(self):
        """时间倒序：最新在前 → 反转后最新在后。无 [N] 前缀。"""
        comments = [
            {"content": "最新的评论", "replies": 0},
            {"content": "较早的评论", "replies": 0},
            {"content": "最早的评论", "replies": 0},
        ]
        result = sanitize_comments(comments, 10)
        assert len(result) == 3
        # 反转：最早→最新
        assert "最早的评论" in result[0]
        assert "最新的评论" in result[2]
        # 无 [N] 前缀
        assert not result[0].startswith("[")

    def test_bbc_code_stripped_in_content(self):
        comments = [{"content": "[b]加粗[/b]的评论", "replies": 0}]
        result = sanitize_comments(comments, 10)
        assert "加粗的评论" in result[0]
        assert "[b]" not in result[0]

    def test_noise_filtered_out(self):
        comments = [
            {"content": "好", "replies": 0},  # 1 char → noise
            {"content": "正常评论", "replies": 0},
        ]
        result = sanitize_comments(comments, 10)
        assert len(result) == 1
        assert "正常评论" in result[0]

    def test_replies_label(self):
        """仅在有回复时追加标签。"""
        comments = [
            {"content": "有回复的评论", "replies": 5},
            {"content": "无回复的评论", "replies": 0},
        ]
        result = sanitize_comments(comments, 10)
        # 时间倒序后，无回复在前（旧），有回复在后（新）
        assert any("【5条回复】" in r for r in result)
        assert all("【0条回复】" not in r for r in result)

    def test_limit_truncation(self):
        comments = [
            {"content": f"comment {i}", "replies": 0}
            for i in range(20)
        ]
        result = sanitize_comments(comments, 3)
        assert len(result) == 3

    def test_deduplicate_identical_content(self):
        """完全相同内容去重，保留首次出现。"""
        comments = [
            {"content": "重复评论内容", "replies": 0},
            {"content": "重复评论内容", "replies": 0},
            {"content": "重复评论内容", "replies": 0},
            {"content": "独特评论", "replies": 0},
        ]
        result = sanitize_comments(comments, 10)
        assert len(result) == 2  # 去重后仅2条
        assert "独特评论" in result
        assert sum(1 for r in result if "重复评论内容" in r) == 1

    def test_empty_and_missing_fields(self):
        comments = [{}, {"content": "", "replies": None}]
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
        # 时间倒序（最新在后）：不行(旧)→还行(中)→神作(新)
        assert "[2星]" in result["comments"][0]
        assert "[5星]" in result["comments"][1]
        assert "[9星]" in result["comments"][2]
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

    def test_chronological_order(self):
        """时间倒序：API 返回最新在前，反转为旧→新。"""
        comments = [
            {"comment": "新评论", "rate": 8, "reactions": []},
            {"comment": "旧评论", "rate": 7, "reactions": []},
        ]
        result = sanitize_subject_comments(comments, 10)
        # API 顺序 [新, 旧] → 反转后 [旧, 新]
        assert "旧评论" in result["comments"][0]
        assert "新评论" in result["comments"][1]

    def test_bbcode_stripped(self):
        comments = [{"comment": "[b]加粗神作[/b]", "rate": 10, "reactions": []}]
        result = sanitize_subject_comments(comments, 10)
        assert "加粗神作" in result["comments"][0]


# ═══════════════════════════════════════════════════════════════════
# sanitize_search_subjects
# ═══════════════════════════════════════════════════════════════════


class TestSanitizeSearchSubjects:
    def test_basic(self):
        raw = {"results": [{"id": 1, "name": "Test", "nameCN": "测试", "type": 2, "info": "25话", "rating": {"score": 7.5, "rank": 100}}], "total": 1}
        result = sanitize_search_subjects(raw)
        assert result["total"] == 1
        r = result["results"][0]
        assert r["id"] == 1
        assert r["name"] == "Test"
        assert r["name_cn"] == "测试"
        assert r["type"] == "动画"
        assert r["score"] == 7.5
        assert r["rank"] == 100
        assert r["info"] == "25话"

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
        assert result["rating_total"] == 9438
        assert result["eps"] == 25
        assert result["date"] == "2008-04-06"
        assert result["nsfw"] is False
        assert isinstance(result["rating_count"], list)
        assert isinstance(result["collection"], dict)
        assert isinstance(result["infobox"], dict)
        assert len(result["tags"]) == 2
        assert "summary" in result

    def test_missing_fields_get_defaults(self):
        result = sanitize_subject_detail({"id": 1})
        assert result["score"] == 0
        assert result["tags"] == []
        assert result["rating_count"] == []
        assert result["collection"] == {}
        assert result["infobox"] == {}
        assert result["date"] == ""
        assert result["summary"] == ""

    def test_tags_not_truncated(self):
        tags = [{"name": f"tag{i}", "count": i} for i in range(15)]
        result = sanitize_subject_detail({"id": 1, "tags": tags})
        assert len(result["tags"]) == 15

    def test_summary_truncated(self):
        result = sanitize_subject_detail({"id": 1, "summary": "x" * 600})
        assert len(result["summary"]) <= 303  # 300 + "..."

    def test_infobox_cleaned(self):
        raw = {
            "id": 1,
            "infobox": [
                {"key": "中文名", "values": [{"v": "测试"}]},       # duplicate → drop
                {"key": "导演", "values": [{"v": "庵野秀明"}]},     # keep
                {"key": "官方网站", "values": [{"v": "http://x"}]}, # URL → drop
                {"key": "售价", "values": [{"v": "6,800円"}]},     # price → drop
                {"key": "", "values": [{"v": ""}]},                 # empty → drop
                {"key": "播放电视台", "values": [{"v": "MBS"}]},    # drop key
            ],
        }
        result = sanitize_subject_detail(raw)
        assert "导演" in result["infobox"]
        assert result["infobox"]["导演"] == "庵野秀明"
        assert "中文名" not in result["infobox"]
        assert "官方网站" not in result["infobox"]
        assert "售价" not in result["infobox"]
        assert "播放电视台" not in result["infobox"]
        assert len(result["infobox"]) == 1


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
        item = result["items"][0]
        assert item["name"] == "Hot Anime"
        assert item["trending_score"] == 500
        assert item["type"] == "动画"  # int→中文
        assert "score" in item
        assert "name_cn" not in item  # 已去重
        assert result["total"] == 1
        assert "动画" in result["summary"]

    def test_empty(self):
        result = sanitize_trending({}, "anime")
        assert result["items"] == []
        assert result["total"] == 0
        assert "暂无" in result["summary"]

    def test_unknown_subject_type_label(self):
        result = sanitize_trending({"data": []}, "unknown")
        assert "unknown" in result["summary"] or "条目" in result["summary"]


# ═══════════════════════════════════════════════════════════════════
# sanitize_trending_topics
# ═══════════════════════════════════════════════════════════════════


class TestSanitizeTrendingTopics:
    def test_basic(self):
        raw = {
            "data": [{
                "id": 40406,
                "title": "看着鲁迪左拥右抱开后宫是真感觉恶心",
                "replyCount": 102,
                "creator": {"nickname": "吐槽君", "avatar": {"large": "http://x.jpg"}},
                "subject": {"id": 501963, "name": "無職転生Ⅲ", "nameCN": "无职转生 第三季",
                            "images": {"large": "http://y.jpg"}, "rating": {"score": 8.0}},
                "createdAt": 1783959451,
                "state": 0, "display": 1,
            }],
            "total": 1,
        }
        result = sanitize_trending_topics(raw)
        assert len(result["items"]) == 1
        t = result["items"][0]
        assert t["title"] == "看着鲁迪左拥右抱开后宫是真感觉恶心"
        assert t["reply_count"] == 102
        assert t["subject_name"] == "无职转生 第三季"
        assert t["subject_id"] == 501963
        assert result["total"] == 1
        # id 和 creator_name 已丢弃（无下游工具消费）
        assert "id" not in t
        assert "creator_name" not in t

    def test_name_fallback(self):
        """subject nameCN 空时回退日文名。"""
        raw = {
            "data": [{
                "id": 1, "title": "test", "replyCount": 0,
                "creator": {}, "subject": {"id": 2, "name": "JP名", "nameCN": ""},
            }],
            "total": 1,
        }
        result = sanitize_trending_topics(raw)
        assert result["items"][0]["subject_name"] == "JP名"

    def test_name_cn_preferred(self):
        """nameCN 存在时使用中文名。"""
        raw = {
            "data": [{
                "id": 1, "title": "test", "replyCount": 0,
                "creator": {}, "subject": {"id": 2, "name": "JP", "nameCN": "中文"},
            }],
            "total": 1,
        }
        result = sanitize_trending_topics(raw)
        assert result["items"][0]["subject_name"] == "中文"

    def test_drops_creator_subject_bloat(self):
        """D 字段：creator.*、subject.* 全部丢弃。"""
        raw = {
            "data": [{
                "id": 1, "title": "test", "replyCount": 5,
                "creator": {"id": 99, "username": "u", "nickname": "n",
                            "avatar": {"large": "http://a.jpg"}, "sign": "s"},
                "subject": {"id": 2, "name": "S", "nameCN": "",
                            "images": {"large": "http://i.jpg"},
                            "rating": {"score": 9.0, "rank": 100},
                            "metaTags": ["TV"], "info": "long info"},
            }],
            "total": 1,
        }
        result = sanitize_trending_topics(raw)
        t = result["items"][0]
        for banned in ("avatar", "sign", "username", "images", "rating", "metaTags", "info"):
            assert banned not in t, f"{banned} should be dropped"

    def test_empty(self):
        result = sanitize_trending_topics({})
        assert result["items"] == []
        assert result["total"] == 0

    def test_reply_count_fallback(self):
        """replyCount 兼容 reply_count。"""
        raw = {
            "data": [{
                "id": 1, "title": "test", "reply_count": 42,
                "creator": {}, "subject": {"id": 2, "name": "S", "nameCN": ""},
            }],
        }
        result = sanitize_trending_topics(raw)
        assert result["items"][0]["reply_count"] == 42


# ═══════════════════════════════════════════════════════════════════
# sanitize_entity_search
# ═══════════════════════════════════════════════════════════════════


class TestSanitizeEntitySearch:
    def test_character(self, sample_character_data):
        results = sanitize_entity_search([sample_character_data], "character")
        assert results[0]["role"] == "角色"
        assert results[0]["nsfw"] is False
        assert "info" in results[0]

    def test_person(self, sample_person_data):
        results = sanitize_entity_search([sample_person_data], "person")
        assert results[0]["career"] == ["seiyu", "actor"]
        assert results[0]["type"] == "个人"
        assert results[0]["nsfw"] is False

    def test_person_career_string_fallback(self):
        results = sanitize_entity_search(
            [{"id": 1, "name": "X", "nameCN": "", "career": "producer"}], "person"
        )
        assert results[0]["career"] == ["producer"]

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
# sanitize_episode_detail
# ═══════════════════════════════════════════════════════════════════


class TestSanitizeEpisodeDetail:
    def test_core_fields(self, episode_response):
        result = sanitize_episode_detail(episode_response)
        assert result["id"] == 1023497
        assert result["sort"] == 1
        assert result["name"] == "梨花の決断"  # CN 空时回退 JP
        assert result["airdate"] == "2024-01-01"
        assert result["duration"] == "24m"
        assert result["comment_count"] == 5
        assert result["subject_id"] == 8
        assert result["subject_name"] == "Test Subject"

    def test_name_cn_preferred(self):
        raw = {"id": 1, "sort": 1, "name": "JP", "nameCN": "中文",
               "airdate": "", "duration": "", "comment": 0,
               "subject": {"id": 2, "name": "S", "nameCN": "条目中文"}}
        result = sanitize_episode_detail(raw)
        assert result["name"] == "中文"
        assert result["subject_name"] == "条目中文"

    def test_drops_duplicates(self):
        """D 字段：name_cn, subject_name_cn, type, disc, subjectID 丢弃。"""
        raw = {"id": 1, "sort": 1, "name": "A", "nameCN": "",
               "type": 0, "disc": 1, "subjectID": 99,
               "airdate": "", "duration": "", "comment": 0,
               "subject": {"id": 2, "name": "S", "nameCN": ""}}
        result = sanitize_episode_detail(raw)
        for banned in ("name_cn", "subject_name_cn", "type", "disc", "subjectID", "ep_name"):
            assert banned not in result, f"{banned} should be dropped"

    def test_desc_truncated(self):
        raw = {"id": 1, "sort": 1, "name": "A", "nameCN": "",
               "airdate": "", "duration": "", "comment": 0,
               "desc": "A" * 600, "subject": {"id": 2, "name": "S", "nameCN": ""}}
        result = sanitize_episode_detail(raw)
        assert len(result["desc"]) <= 505  # 500 + "..." overhead


# ═══════════════════════════════════════════════════════════════════
# sanitize_entity_comments
# ═══════════════════════════════════════════════════════════════════


class TestSanitizeEntityComments:
    def test_character(self, sample_comment):
        """entity_detail 提供 nameCN → entity_name 精确归属。"""
        comments = [sample_comment]
        entity_detail = {"name": "TestCharJP", "nameCN": "测试角色"}
        result = sanitize_entity_comments(comments, 10, "character", 123, entity_detail)
        assert result["entity_type"] == "character"
        assert result["entity_id"] == 123
        assert result["entity_name"] == "测试角色"
        assert len(result["comments"]) == 1

    def test_empty(self):
        """空评论列表 + 无 entity_detail。"""
        result = sanitize_entity_comments([], 10, "person", 456, None)
        assert result["comments"] == []
        assert result["comment_count"] == 0
        assert result["entity_name"] == ""
        assert result["entity_id"] == 456

    def test_no_entity_detail(self, sample_comment):
        """entity_detail=None 时 entity_name 为空字符串，不崩溃。"""
        result = sanitize_entity_comments([sample_comment], 10, "character", 789, None)
        assert result["entity_name"] == ""
        assert result["entity_id"] == 789

    def test_entity_detail_with_error(self, sample_comment):
        """entity_detail 包含 _error 时跳过姓名提取。"""
        result = sanitize_entity_comments(
            [sample_comment], 10, "person", 1,
            {"_error": "timeout"}
        )
        assert result["entity_name"] == ""

    def test_name_fallback_to_jp(self, sample_comment):
        """nameCN 缺失时 fallback 到 name（日文名）。"""
        entity_detail = {"name": "ルルーシュ"}
        result = sanitize_entity_comments(
            [sample_comment], 10, "character", 1, entity_detail
        )
        assert result["entity_name"] == "ルルーシュ"


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
        # type_distribution 不再由 sanitizer 计算，改由 user endpoint 的 stats 提供
        # total 用 api_total（本次测试未传=0，fallback 到采样 len）
        assert result["total"] == 30
        assert "score_dist" in result["collection_stats"]

    def test_empty(self):
        result = sanitize_user_collections([], 10)
        assert result["collections"] == []
        assert result["total"] == 0

    def test_less_than_cap(self):
        data = [
            {
                "subject": {"id": 1, "name": "A", "nameCN": "", "type": 2, "rating": {}},
                "interest": {"type": 2, "rate": 8, "comment": "", "tags": [], "updatedAt": 1700000000},
            }
        ]
        result = sanitize_user_collections(data, 10)
        assert len(result["collections"]) == 1
        assert result["collection_stats"]["avg_score"] == 8.0

    def test_score_distribution(self):
        data = [
            {"subject": {"id": 1, "name": "A", "nameCN": "", "type": 2, "rating": {}}, "interest": {"type": 2, "rate": 9, "comment": "", "tags": [], "updatedAt": 1700000000}},
            {"subject": {"id": 2, "name": "B", "nameCN": "", "type": 2, "rating": {}}, "interest": {"type": 2, "rate": 3, "comment": "", "tags": [], "updatedAt": 1700000000}},
            {"subject": {"id": 3, "name": "C", "nameCN": "", "type": 2, "rating": {}}, "interest": {"type": 2, "rate": 5, "comment": "", "tags": [], "updatedAt": 1700000000}},
        ]
        result = sanitize_user_collections(data, 10)
        sd = result["collection_stats"]["score_dist"]
        assert sd["9-10"] == 1
        assert sd["4-6"] == 1
        assert sd["1-3"] == 1

    def test_unrated_entries_not_in_score_stats(self):
        data = [
            {"subject": {"id": 1, "name": "A", "nameCN": "", "type": 2, "rating": {}}, "interest": {"type": 2, "rate": 0, "comment": "", "tags": [], "updatedAt": 1700000000}}
        ]
        result = sanitize_user_collections(data, 10)
        assert "avg_score" not in result["collection_stats"]

    def test_new_fields_updated_at_comment_info_meta_tags(self):
        """验证新增字段：updated_at、comment、info、meta_tags。"""
        data = [
            {
                "subject": {
                    "id": 1, "name": "Test", "nameCN": "", "type": 2,
                    "rating": {}, "info": "2024-01-15 / 某作者 / 某出版社",
                    "metaTags": ["日本", "漫画", "原创"],
                },
                "interest": {
                    "type": 2, "rate": 8,
                    "comment": "这部作品真的很棒，推荐给所有人看！剧情紧凑节奏完美。",
                    "tags": [],
                    "updatedAt": 1719761848,
                },
            }
        ]
        result = sanitize_user_collections(data, 10)
        c = result["collections"][0]
        assert c["updated_at"] == "2024-06-30"
        assert "这部作品真的很棒" in c["comment"]
        assert len(c["comment"]) <= 100
        assert c["info"] == "2024-01-15 / 某作者 / 某出版社"
        assert c["meta_tags"] == ["日本", "漫画", "原创"]

    def test_empty_comment_info_meta_tags_omitted(self):
        """空 comment、info、meta_tags 不输出 key 以省 token。"""
        data = [
            {
                "subject": {"id": 1, "name": "A", "nameCN": "", "type": 2, "rating": {}},
                "interest": {"type": 2, "rate": 0, "comment": "", "tags": [], "updatedAt": 0},
            }
        ]
        result = sanitize_user_collections(data, 10)
        c = result["collections"][0]
        assert "comment" not in c
        assert "info" not in c
        assert "meta_tags" not in c
        assert c["updated_at"] == ""  # 0 timestamp → 空字符串


# ═══════════════════════════════════════════════════════════════════
# sanitize_user_stats
# ═══════════════════════════════════════════════════════════════════


class TestSanitizeUserStats:
    """user endpoint stats 整数 code → 人类可读标签。"""

    def test_empty(self):
        assert sanitize_user_stats({}) == {}

    def test_none(self):
        assert sanitize_user_stats(None) == {}  # type: ignore[arg-type]

    def test_full_stats(self):
        raw = {
            "subject": {
                "2": {"1": 4, "2": 27, "3": 4, "4": 1, "5": 2},
                "1": {"2": 8},
            },
            "mono": {"character": 10, "person": 5},
            "blog": 2,
            "friend": 12,
            "group": 7,
            "index": {"create": 0, "collect": 1},
        }
        result = sanitize_user_stats(raw)
        assert result["by_type"]["动画"] == {"想看": 4, "看过": 27, "在看": 4, "搁置": 1, "抛弃": 2}
        assert result["by_type"]["书籍"] == {"看过": 8}
        assert result["角色"] == 10
        assert result["人物"] == 5
        assert result["日志"] == 2

    def test_zero_counts_omitted(self):
        """计数为 0 的状态/类型不输出。"""
        raw = {"subject": {"2": {"1": 0, "2": 5, "3": 0}}}
        result = sanitize_user_stats(raw)
        # 只有"看过"有非零值，且没有空的 mono/blog
        assert result["by_type"]["动画"] == {"看过": 5}
        assert "角色" not in result
        assert "日志" not in result

    def test_no_subject_stats(self):
        """Stats 有 mono 但没有 subject 时不崩溃。"""
        raw = {"mono": {"character": 3}}
        result = sanitize_user_stats(raw)
        assert "by_type" not in result
        assert result["角色"] == 3


# ═══════════════════════════════════════════════════════════════════
# sanitize_subject_characters
# ═══════════════════════════════════════════════════════════════════


class TestSanitizeSubjectCharacters:
    """条目角色列表 v3 极致索引 — 仅保留边的端点和类型。"""

    def test_empty_returns_empty_list(self):
        result = sanitize_subject_characters([], 265)
        assert result == {"subject_id": 265, "characters": []}

    def test_character_core_fields(self):
        """A 字段：character_id, name（CN 优先）, char_type。"""
        data = [{
            "character": {
                "id": 302, "name": "碇シンジ", "nameCN": "碇真嗣",
                "role": 1, "info": "性别 男", "comment": 238,
                "lock": False, "nsfw": False,
                "images": {"large": "http://x.jpg"},
            },
            "casts": [],
            "type": 1, "order": 0,
        }]
        result = sanitize_subject_characters(data, 265)
        ch = result["characters"][0]
        assert ch["character_id"] == 302
        assert ch["name"] == "碇真嗣"  # CN 优先
        assert ch["char_type"] == "主角"

    def test_name_fallback_to_jp(self):
        """nameCN 为空时回退到日文名。"""
        data = [{
            "character": {"id": 1, "name": "シンジ", "nameCN": "", "role": 1,
                          "info": "", "comment": 0, "lock": False, "nsfw": False,
                          "images": {}},
            "casts": [], "type": 1, "order": 0,
        }]
        result = sanitize_subject_characters(data, 1)
        assert result["characters"][0]["name"] == "シンジ"

    def test_drops_role_info_comment_order(self):
        """D 字段：role, info, comment, order 属于 character detail，丢弃。"""
        data = [{
            "character": {"id": 1, "name": "X", "nameCN": "", "role": 1,
                          "info": "性别 男", "comment": 100, "lock": False, "nsfw": False,
                          "images": {}},
            "casts": [], "type": 1, "order": 5,
        }]
        result = sanitize_subject_characters(data, 1)
        ch = result["characters"][0]
        for f in ("role", "info", "comment", "order"):
            assert f not in ch, f"{f} should be dropped"

    def test_drops_images_lock_nsfw(self):
        """D 字段：images, lock, nsfw 丢弃。"""
        data = [{
            "character": {"id": 1, "name": "X", "nameCN": "", "role": 1,
                          "info": "", "comment": 0, "lock": True, "nsfw": True,
                          "images": {"large": "http://lg.jpg"}},
            "casts": [], "type": 1, "order": 0,
        }]
        result = sanitize_subject_characters(data, 1)
        ch = result["characters"][0]
        for f in ("images", "lock", "nsfw"):
            assert f not in ch, f"{f} should be dropped"

    def test_casts_string_format(self):
        """casts 压缩为字符串：CV 省略标签，其他标注关系。"""
        data = [{
            "character": {"id": 1, "name": "A", "nameCN": "", "role": 1,
                          "info": "", "comment": 0, "lock": False, "nsfw": False,
                          "images": {}},
            "casts": [
                {"person": {"id": 4054, "name": "緒方恵美", "nameCN": "绪方惠美",
                            "type": 1, "info": "", "career": [], "comment": 0,
                            "lock": False, "nsfw": False, "images": {}},
                 "relation": 0, "summary": ""},
                {"person": {"id": 75303, "name": "罗伟杰", "nameCN": "",
                            "type": 1, "info": "", "career": [], "comment": 0,
                            "lock": False, "nsfw": False, "images": {}},
                 "relation": 1, "summary": ""},
                {"person": {"id": 67947, "name": "刘艺", "nameCN": "刘艺（配音演员）",
                            "type": 1, "info": "", "career": [], "comment": 0,
                            "lock": False, "nsfw": False, "images": {}},
                 "relation": 3, "summary": ""},
            ],
            "type": 1, "order": 0,
        }]
        result = sanitize_subject_characters(data, 1)
        casts = result["characters"][0]["casts"]
        assert "绪方惠美" in casts
        assert "罗伟杰(Dub)" in casts
        assert "刘艺（配音演员）(中配)" in casts
        assert "person_id" not in casts  # string, not dict

    def test_casts_empty_when_no_persons(self):
        """无 casts 时返回空字符串。"""
        data = [{
            "character": {"id": 1, "name": "A", "nameCN": "", "role": 1,
                          "info": "", "comment": 0, "lock": False, "nsfw": False,
                          "images": {}},
            "casts": [], "type": 1, "order": 0,
        }]
        result = sanitize_subject_characters(data, 1)
        assert result["characters"][0]["casts"] == ""

    def test_subject_id_preserved(self):
        result = sanitize_subject_characters([], 8491)
        assert result["subject_id"] == 8491

    def test_multiple_characters(self):
        data = [
            {"character": {"id": i, "name": f"C{i}", "nameCN": "", "role": 1,
                           "info": "", "comment": 0, "lock": False, "nsfw": False,
                           "images": {}},
             "casts": [], "type": 1, "order": i}
            for i in range(5)
        ]
        result = sanitize_subject_characters(data, 1)
        assert len(result["characters"]) == 5
        assert all(isinstance(ch["casts"], str) for ch in result["characters"])


# ═══════════════════════════════════════════════════════════════════
# sanitize_reviews
# ═══════════════════════════════════════════════════════════════════


class TestSanitizeReviews:
    def test_basic(self):
        raw = {"data": [{
            "id": 327225,
            "entry": {"title": "深度评测", "summary": "这是一篇很长的评测...", "replies": 5,
                      "created_at": "2024-01-01", "icon": "x", "uid": 1,
                      "public": True, "updatedAt": "", "type": 0},
            "user": {"nickname": "评测君", "avatar": {"large": "http://x.jpg"},
                     "sign": "签名", "group": 10, "username": "u", "id": 1, "joinedAt": ""},
        }], "total": 1}
        result = sanitize_reviews(raw)
        assert len(result["items"]) == 1
        r = result["items"][0]
        assert r["id"] == 327225
        assert r["title"] == "深度评测"
        assert r["user_name"] == "评测君"
        assert r["reply_count"] == 5
        assert r["created_at"] == "2024-01-01"
        # D 字段丢弃
        for banned in ("avatar", "sign", "icon", "uid", "public", "type"):
            assert banned not in r

    def test_empty(self):
        result = sanitize_reviews({})
        assert result["items"] == []
        assert result["total"] == 0


# ═══════════════════════════════════════════════════════════════════
# sanitize_discussion_topics
# ═══════════════════════════════════════════════════════════════════


class TestSanitizeDiscussionTopics:
    def test_basic(self):
        raw = {"data": [{
            "id": 40423, "title": "EVA深度讨论",
            "replyCount": 15,
            "creator": {"nickname": "讨论君", "avatar": {"large": "http://x.jpg"},
                        "sign": "", "group": 10, "username": "u", "id": 1, "joinedAt": ""},
            "creatorID": 1, "parentID": 0, "createdAt": "", "updatedAt": "",
            "state": 0, "display": 1,
        }], "total": 1}
        result = sanitize_discussion_topics(raw)
        assert len(result["items"]) == 1
        t = result["items"][0]
        assert t["id"] == 40423
        assert t["title"] == "EVA深度讨论"
        assert t["reply_count"] == 15
        assert t["creator_name"] == "讨论君"
        for banned in ("avatar", "sign", "creatorID", "parentID", "state", "display"):
            assert banned not in t

    def test_empty(self):
        result = sanitize_discussion_topics({})
        assert result["items"] == []
        assert result["total"] == 0


# ═══════════════════════════════════════════════════════════════════
# sanitize_subject_episodes
# ═══════════════════════════════════════════════════════════════════


class TestSanitizeSubjectEpisodes:
    def test_basic(self):
        raw = {"data": [
            {"id": 1054, "sort": 2, "name": "第二集", "nameCN": "", "type": 0,
             "airdate": "1995-10-11", "comment": 50, "desc": "第二集描述",
             "disc": 0, "subjectID": 265, "duration": "24m"},
            {"id": 1053, "sort": 1, "name": "第一集", "nameCN": "使徒、袭来", "type": 0,
             "airdate": "1995-10-04", "comment": 106, "desc": "第一集描述",
             "disc": 0, "subjectID": 265, "duration": "24m"},
        ], "total": 2}
        result = sanitize_subject_episodes(raw)
        assert len(result["items"]) == 2
        # 按集数升序
        assert result["items"][0]["sort"] == 1
        assert result["items"][0]["name"] == "使徒、袭来"  # CN 优先
        assert result["items"][1]["sort"] == 2
        assert result["items"][1]["name"] == "第二集"  # CN 空，回退 JP
        assert result["items"][0]["comment_count"] == 106

    def test_filters_non_mainline(self):
        """过滤 type != 0 的非主线剧集。"""
        raw = {"data": [
            {"id": 1, "sort": 1, "name": "主线", "nameCN": "", "type": 0,
             "airdate": "", "comment": 0, "desc": ""},
            {"id": 2, "sort": 2, "name": "SP", "nameCN": "", "type": 1,
             "airdate": "", "comment": 0, "desc": ""},
        ]}
        result = sanitize_subject_episodes(raw)
        assert len(result["items"]) == 1
        assert result["items"][0]["name"] == "主线"

    def test_empty(self):
        result = sanitize_subject_episodes({})
        assert result["items"] == []
        assert result["total"] == 0

    def test_drops_name_cn_and_redundant(self):
        raw = {"data": [
            {"id": 1, "sort": 1, "name": "A", "nameCN": "中文", "type": 0,
             "airdate": "", "comment": 0, "desc": "",
             "disc": 1, "subjectID": 99, "duration": "24m"},
        ]}
        result = sanitize_subject_episodes(raw)
        e = result["items"][0]
        for banned in ("name_cn", "disc", "subjectID", "duration"):
            assert banned not in e, f"{banned} should be dropped"


# ═══════════════════════════════════════════════════════════════════
# sanitize_timeline_events
# ═══════════════════════════════════════════════════════════════════


class TestSanitizeTimelineEvents:
    """时光机事件展平 — 从 memo 深层提取 + 噪音过滤。"""

    def test_empty(self):
        result = sanitize_timeline_events([], 10)
        assert result == {"events": [], "total": 0}

    def test_daily_events_filtered_out(self):
        """type=2 每日签到事件应被丢弃。"""
        raw = [
            {"type": 2, "cat": 1, "createdAt": 1700000000,
             "memo": {"daily": {"users": [{"nickname": "X"}]}}},
            {"type": 2, "cat": 1, "createdAt": 1700000001,
             "memo": {"daily": {"users": [{"nickname": "Y"}]}}},
        ]
        result = sanitize_timeline_events(raw, 10)
        assert result["total"] == 0
        assert result["events"] == []

    def test_string_entries_skipped(self):
        """API 有时在列表中混入字符串（错误信息），应跳过。"""
        raw = ["statusCode", {"type": 0, "cat": 4, "createdAt": 1700000000,
                "memo": {"progress": {"batch": {"epsTotal": "5", "subject": {
                    "id": 1, "name": "Test", "nameCN": "测试"}}}}}]
        result = sanitize_timeline_events(raw, 10)
        assert result["total"] == 1

    def test_collection_event_type_9(self):
        """type=9 条目收藏 → subject_name, rate, comment。"""
        raw = [{
            "type": 9, "cat": 3, "createdAt": 1719761848,
            "memo": {"subject": [{
                "subject": {"id": 8, "name": "EVA", "nameCN": "新世纪福音战士"},
                "rate": 9, "comment": "神作不解释",
            }]},
        }]
        result = sanitize_timeline_events(raw, 10)
        e = result["events"][0]
        assert e["type"] == "收藏"
        assert e["subject_name"] == "新世纪福音战士"
        assert e["subject_id"] == 8
        assert e["rate"] == 9
        assert e["comment"] == "神作不解释"
        assert "2024-06-30" in e["created_at"]

    def test_collection_no_rate_no_comment(self):
        """无评分无短评时 key 应省略。"""
        raw = [{
            "type": 9, "cat": 3, "createdAt": 1700000000,
            "memo": {"subject": [{
                "subject": {"id": 1, "name": "Test"},
                "rate": 0, "comment": "",
            }]},
        }]
        result = sanitize_timeline_events(raw, 10)
        e = result["events"][0]
        assert "rate" not in e
        assert "comment" not in e

    def test_progress_event(self):
        """type=0 进度事件 → eps_total。"""
        raw = [{
            "type": 0, "cat": 4, "createdAt": 1700000000,
            "memo": {"progress": {"batch": {
                "epsTotal": "7",
                "subject": {"id": 100, "name": "Anime", "nameCN": "动画"},
            }}},
        }]
        result = sanitize_timeline_events(raw, 10)
        e = result["events"][0]
        assert e["type"] == "进度"
        assert e["subject_name"] == "动画"
        assert e["eps_total"] == "7"

    def test_blog_event(self):
        """type=1 日志事件 → blog_id, title, summary。"""
        raw = [{
            "type": 1, "cat": 6, "createdAt": 1700000000,
            "memo": {"blog": {
                "id": 123, "title": "我的评测",
                "summary": "[mask]剧透内容[/mask] 这是一篇评测",
                "replies": 5,
            }},
        }]
        result = sanitize_timeline_events(raw, 10)
        e = result["events"][0]
        assert e["type"] == "日志"
        assert e["blog_id"] == 123
        assert e["title"] == "我的评测"
        assert "[mask]" not in e["summary"]  # BBCode stripped
        assert "剧透内容" in e["summary"]
        assert e["replies"] == 5

    def test_limit_respected(self):
        """limit 截断生效。"""
        raw = [
            {"type": 0, "cat": 4, "createdAt": 1700000000 + i,
             "memo": {"progress": {"batch": {"epsTotal": f"{i}", "subject": {
                 "id": i, "name": f"N{i}"}}}}}
            for i in range(10)
        ]
        result = sanitize_timeline_events(raw, 3)
        assert result["total"] == 3

    def test_mixed_events_filter_and_order(self):
        """混合事件：daily 丢弃，有效事件保留。"""
        raw = [
            {"type": 2, "cat": 1, "createdAt": 1700000000,
             "memo": {"daily": {"users": [{"nickname": "X"}]}}},
            {"type": 9, "cat": 3, "createdAt": 1700000001,
             "memo": {"subject": [{"subject": {"id": 1, "name": "A"},
                                    "rate": 8, "comment": ""}]}},
            {"type": 0, "cat": 4, "createdAt": 1700000002,
             "memo": {"progress": {"batch": {"epsTotal": "3", "subject": {
                 "id": 2, "name": "B"}}}}},
            {"type": 2, "cat": 1, "createdAt": 1700000003,
             "memo": {"daily": {"users": [{"nickname": "Y"}]}}},
        ]
        result = sanitize_timeline_events(raw, 10)
        assert result["total"] == 2
        assert result["events"][0]["type"] == "收藏"
        assert result["events"][1]["type"] == "进度"

    def test_name_cn_fallback(self):
        """nameCN 缺失时回退到 name。"""
        raw = [{
            "type": 9, "cat": 3, "createdAt": 1700000000,
            "memo": {"subject": [{
                "subject": {"id": 1, "name": "進撃の巨人"},
                "rate": 0, "comment": "",
            }]},
        }]
        result = sanitize_timeline_events(raw, 10)
        assert result["events"][0]["subject_name"] == "進撃の巨人"
