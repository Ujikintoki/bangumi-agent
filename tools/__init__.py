"""Agent Tool 层。

将 BangumiClient 业务方法包装为 LLM 可调用的 @tool 函数。
全部 Pydantic Schema 来自 schemas.tools_input，HTTP 通信统一走 clients.BangumiClient。
"""

from tools.bgm_tools import (
    get_agent_tools,
    get_blog,
    get_calendar,
    get_entity_comments,
    get_episode_comments,
    get_bangumi_subject_detail,
    get_subject_characters,
    get_subject_discussion,
    get_trending_topics,
    get_user_profile,
    get_user_timeline,
    search_bangumi_subject,
    search_local_bangumi,
)

__all__ = [
    "get_agent_tools",
    "search_bangumi_subject",
    "get_bangumi_subject_detail",
    "get_calendar",
    "get_trending_topics",
    "get_episode_comments",
    "get_subject_discussion",
    "get_entity_comments",
    "get_subject_characters",
    "get_user_profile",
    "get_blog",
    "get_user_timeline",
    "search_local_bangumi",
]
