"""Bangumi Agent Tool 输入 Schema。

所有 LLM-facing Tool 的 Pydantic v2 输入契约集中于此模块。
"""

from schemas.tools_input import (
    GetBlogInput,
    GetCalendarInput,
    GetCharacterDetailInput,
    GetEntityCommentsInput,
    GetEpisodeDiscussionInput,
    GetHotTopicsInput,
    GetPersonDetailInput,
    GetSubjectCharactersInput,
    GetSubjectDetailInput,
    GetSubjectEpisodesInput,
    GetSubjectOpinionsInput,
    GetTrendingSubjectsInput,
    GetUserProfileInput,
    LocalSearchInput,
    SearchBangumiInput,
    UserTimelineInput,
)

__all__ = [
    "SearchBangumiInput",
    "GetCalendarInput",
    "GetCharacterDetailInput",
    "GetTrendingSubjectsInput",
    "GetHotTopicsInput",
    "GetEpisodeDiscussionInput",
    "GetSubjectOpinionsInput",
    "GetSubjectEpisodesInput",
    "GetEntityCommentsInput",
    "GetPersonDetailInput",
    "GetUserProfileInput",
    "GetBlogInput",
    "GetSubjectDetailInput",
    "GetSubjectCharactersInput",
    "LocalSearchInput",
    "UserTimelineInput",
]
