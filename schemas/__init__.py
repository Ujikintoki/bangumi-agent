"""Bangumi Agent Tool 输入 Schema。

所有 LLM-facing Tool 的 Pydantic v2 输入契约集中于此模块。
"""

from schemas.tools_input import (
    GetBlogInput,
    GetCalendarInput,
    GetCharacterDetailInput,
    GetEntityCommentsInput,
    GetEpisodeDiscussionInput,
    GetPersonDetailInput,
    GetSubjectCharactersInput,
    GetSubjectDetailInput,
    GetSubjectDiscussionInput,
    GetTrendingInput,
    GetUserProfileInput,
    LocalSearchInput,
    SearchBangumiInput,
    UserTimelineInput,
)

__all__ = [
    "SearchBangumiInput",
    "GetCalendarInput",
    "GetCharacterDetailInput",
    "GetTrendingInput",
    "GetEpisodeDiscussionInput",
    "GetSubjectDiscussionInput",
    "GetEntityCommentsInput",
    "GetPersonDetailInput",
    "GetUserProfileInput",
    "GetBlogInput",
    "GetSubjectDetailInput",
    "GetSubjectCharactersInput",
    "LocalSearchInput",
    "UserTimelineInput",
]
