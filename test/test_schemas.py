"""test_schemas.py — schemas/tools_input.py 全部 12 个 Schema 的单元测试。

覆盖：实例化、默认值、字段类型、边界值、无效输入。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from schemas.tools_input import (
    GetBlogInput,
    GetCalendarInput,
    GetEntityCommentsInput,
    GetEpisodeDiscussionInput,
    GetSubjectCharactersInput,
    GetSubjectDetailInput,
    GetSubjectDiscussionInput,
    GetTrendingInput,
    GetUserProfileInput,
    LocalSearchInput,
    SearchBangumiInput,
    UserTimelineInput,
)


# ═══════════════════════════════════════════════════════════════════
# SearchBangumiInput
# ═══════════════════════════════════════════════════════════════════


class TestSearchBangumiInput:
    def test_minimal_instantiation(self):
        s = SearchBangumiInput(keyword="test")
        assert s.keyword == "test"
        assert s.entity_type == "subject"
        assert s.limit == 5

    def test_all_fields(self):
        s = SearchBangumiInput(
            keyword="進撃の巨人", entity_type="character", limit=8, subject_type=2, nsfw=False
        )
        assert s.keyword == "進撃の巨人"
        assert s.entity_type == "character"
        assert s.limit == 8
        assert s.subject_type == 2
        assert s.nsfw is False

    def test_missing_keyword_raises(self):
        with pytest.raises(ValidationError):
            SearchBangumiInput()

    def test_invalid_entity_type_raises(self):
        with pytest.raises(ValidationError):
            SearchBangumiInput(keyword="test", entity_type="invalid")

    def test_limit_out_of_range_raises(self):
        with pytest.raises(ValidationError):
            SearchBangumiInput(keyword="test", limit=0)
        with pytest.raises(ValidationError):
            SearchBangumiInput(keyword="test", limit=11)

    def test_subject_type_boundaries(self):
        s1 = SearchBangumiInput(keyword="t", subject_type=1)
        assert s1.subject_type == 1
        s6 = SearchBangumiInput(keyword="t", subject_type=6)
        assert s6.subject_type == 6
        with pytest.raises(ValidationError):
            SearchBangumiInput(keyword="t", subject_type=7)

    def test_nsfw_nullable(self):
        s = SearchBangumiInput(keyword="t", nsfw=None)
        assert s.nsfw is None


# ═══════════════════════════════════════════════════════════════════
# GetSubjectDetailInput
# ═══════════════════════════════════════════════════════════════════


class TestGetSubjectDetailInput:
    def test_valid(self):
        s = GetSubjectDetailInput(subject_id=8)
        assert s.subject_id == 8

    def test_missing_raises(self):
        with pytest.raises(ValidationError):
            GetSubjectDetailInput()

    def test_non_positive_raises(self):
        with pytest.raises(ValidationError):
            GetSubjectDetailInput(subject_id=0)


# ═══════════════════════════════════════════════════════════════════
# GetCalendarInput
# ═══════════════════════════════════════════════════════════════════


class TestGetCalendarInput:
    def test_defaults(self):
        c = GetCalendarInput()
        assert c.weekday == "today"
        assert c.limit_per_day == 10

    def test_all_weekdays(self):
        for w in ("today", "mon", "tue", "wed", "thu", "fri", "sat", "sun", "all"):
            c = GetCalendarInput(weekday=w)
            assert c.weekday == w

    def test_invalid_weekday_raises(self):
        with pytest.raises(ValidationError):
            GetCalendarInput(weekday="invalid")

    def test_limit_boundaries(self):
        assert GetCalendarInput(limit_per_day=1).limit_per_day == 1
        assert GetCalendarInput(limit_per_day=15).limit_per_day == 15
        with pytest.raises(ValidationError):
            GetCalendarInput(limit_per_day=0)
        with pytest.raises(ValidationError):
            GetCalendarInput(limit_per_day=16)


# ═══════════════════════════════════════════════════════════════════
# GetTrendingInput
# ═══════════════════════════════════════════════════════════════════


class TestGetTrendingInput:
    def test_defaults(self):
        t = GetTrendingInput()
        assert t.category == "both"
        assert t.subject_type is None
        assert t.limit == 10

    def test_categories(self):
        for cat in ("subjects", "topics", "both"):
            t = GetTrendingInput(category=cat)
            assert t.category == cat

    def test_subject_types(self):
        for st in ("anime", "book", "music", "game", "real"):
            t = GetTrendingInput(subject_type=st)
            assert t.subject_type == st

    def test_invalid_category_raises(self):
        with pytest.raises(ValidationError):
            GetTrendingInput(category="invalid")


# ═══════════════════════════════════════════════════════════════════
# GetEpisodeDiscussionInput
# ═══════════════════════════════════════════════════════════════════


class TestGetEpisodeDiscussionInput:
    def test_valid(self):
        e = GetEpisodeDiscussionInput(episode_id=1023497, comments_limit=30)
        assert e.episode_id == 1023497
        assert e.comments_limit == 30

    def test_defaults(self):
        e = GetEpisodeDiscussionInput(episode_id=1)
        assert e.comments_limit == 15

    def test_limit_boundaries(self):
        assert GetEpisodeDiscussionInput(episode_id=1, comments_limit=1).comments_limit == 1
        assert GetEpisodeDiscussionInput(episode_id=1, comments_limit=40).comments_limit == 40
        with pytest.raises(ValidationError):
            GetEpisodeDiscussionInput(episode_id=1, comments_limit=0)


# ═══════════════════════════════════════════════════════════════════
# GetSubjectDiscussionInput
# ═══════════════════════════════════════════════════════════════════


class TestGetSubjectDiscussionInput:
    def test_defaults(self):
        s = GetSubjectDiscussionInput(subject_id=8)
        assert s.subject_id == 8
        assert s.data_types == ["comments", "reviews"]
        assert s.limit == 8

    def test_custom_data_types(self):
        s = GetSubjectDiscussionInput(
            subject_id=8, data_types=["comments", "topics", "episodes"]
        )
        assert len(s.data_types) == 3

    def test_empty_data_types_allowed(self):
        s = GetSubjectDiscussionInput(subject_id=8, data_types=[])
        assert s.data_types == []

    def test_invalid_data_type_raises(self):
        with pytest.raises(ValidationError):
            GetSubjectDiscussionInput(subject_id=8, data_types=["invalid"])


# ═══════════════════════════════════════════════════════════════════
# GetEntityCommentsInput
# ═══════════════════════════════════════════════════════════════════


class TestGetEntityCommentsInput:
    def test_character(self):
        e = GetEntityCommentsInput(entity_type="character", entity_id=1)
        assert e.entity_type == "character"

    def test_person(self):
        e = GetEntityCommentsInput(entity_type="person", entity_id=100)
        assert e.entity_type == "person"

    def test_invalid_entity_type_raises(self):
        with pytest.raises(ValidationError):
            GetEntityCommentsInput(entity_type="subject", entity_id=1)

    def test_default_limit(self):
        e = GetEntityCommentsInput(entity_type="character", entity_id=1)
        assert e.limit == 10


# ═══════════════════════════════════════════════════════════════════
# GetSubjectCharactersInput
# ═══════════════════════════════════════════════════════════════════


class TestGetSubjectCharactersInput:
    def test_valid(self):
        s = GetSubjectCharactersInput(subject_id=8)
        assert s.subject_id == 8

    def test_missing_raises(self):
        with pytest.raises(ValidationError):
            GetSubjectCharactersInput()


# ═══════════════════════════════════════════════════════════════════
# GetUserProfileInput
# ═══════════════════════════════════════════════════════════════════


class TestGetUserProfileInput:
    def test_defaults(self):
        u = GetUserProfileInput(username="testuser")
        assert u.username == "testuser"
        assert u.collections_limit == 20
        assert u.include_blogs is True
        assert u.include_characters is False
        assert u.include_persons is False

    def test_all_flags(self):
        u = GetUserProfileInput(
            username="test",
            collections_limit=30,
            include_blogs=False,
            include_characters=True,
            include_persons=True,
        )
        assert u.collections_limit == 30
        assert u.include_blogs is False
        assert u.include_characters is True

    def test_limit_boundaries(self):
        with pytest.raises(ValidationError):
            GetUserProfileInput(username="t", collections_limit=0)


# ═══════════════════════════════════════════════════════════════════
# GetBlogInput
# ═══════════════════════════════════════════════════════════════════


class TestGetBlogInput:
    def test_defaults(self):
        b = GetBlogInput(entry_id=12345)
        assert b.entry_id == 12345
        assert b.include_comments is True
        assert b.include_subjects is True

    def test_flags_off(self):
        b = GetBlogInput(entry_id=1, include_comments=False, include_subjects=False)
        assert b.include_comments is False
        assert b.include_subjects is False


# ═══════════════════════════════════════════════════════════════════
# LocalSearchInput
# ═══════════════════════════════════════════════════════════════════


class TestLocalSearchInput:
    def test_defaults(self):
        s = LocalSearchInput(query="80年代机战番")
        assert s.query == "80年代机战番"
        assert s.entity_type == "all"
        assert s.limit == 5
        assert s.nsfw is False

    def test_all_entity_types(self):
        for et in ("subject", "character", "person", "all"):
            s = LocalSearchInput(query="test", entity_type=et)
            assert s.entity_type == et

    def test_nsfw_flag(self):
        s = LocalSearchInput(query="test", nsfw=True)
        assert s.nsfw is True

    def test_invalid_entity_type_raises(self):
        with pytest.raises(ValidationError):
            LocalSearchInput(query="test", entity_type="invalid")


# ═══════════════════════════════════════════════════════════════════
# UserTimelineInput
# ═══════════════════════════════════════════════════════════════════


class TestUserTimelineInput:
    def test_defaults(self):
        u = UserTimelineInput(username="testuser")
        assert u.username == "testuser"
        assert u.limit == 10

    def test_limit_boundaries(self):
        assert UserTimelineInput(username="t", limit=1).limit == 1
        assert UserTimelineInput(username="t", limit=20).limit == 20
        with pytest.raises(ValidationError):
            UserTimelineInput(username="t", limit=0)
        with pytest.raises(ValidationError):
            UserTimelineInput(username="t", limit=21)
