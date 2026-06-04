"""Bangumi Agent Tool 输入 Schema。

所有 LLM-facing Tool 的 Pydantic v2 输入契约集中于此模块。
"""

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

__all__ = [
    "SearchBangumiInput",
    "GetCalendarInput",
    "GetTrendingInput",
    "GetEpisodeDiscussionInput",
    "GetSubjectDiscussionInput",
    "GetEntityCommentsInput",
    "GetUserProfileInput",
    "GetBlogInput",
    "GetSubjectDetailInput",
    "GetSubjectCharactersInput",
    "LocalSearchInput",
    "UserTimelineInput",
]
