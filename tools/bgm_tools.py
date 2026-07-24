"""
AI Agent 工具函数层

将底层 BangumiClient 与 p1 API 包装为 LLM 可直接调用的异步工具函数。
每个函数附带详尽的 Google Style 中文 Docstring，帮助大模型
理解工具用途、参数含义及最佳调用时机。

架构约束：
  - 纯读操作：仅 GET 请求，绝无 PUT/POST/DELETE。
  - 认证透明化：access_token 绝不暴露给 LLM Schema。
  - 优雅降级：所有异常捕获后返回自然语言字符串。
  - HTTP 通信统一通过 clients.BangumiClient，工具层不再裸写 HTTP。
  - 所有 Pydantic Schema 统一从 schemas/tools_input 导入。
"""

from __future__ import annotations

import contextvars
import logging
from typing import Any, Optional

from langchain_core.tools import tool

from clients import BangumiClient
from core.config import get_settings
from schemas.tools_input import (
    GetBlogInput,
    GetCalendarInput,
    GetCharacterDetailInput,
    GetEntityCommentsInput,
    GetEpisodeDiscussionInput,
    GetPersonDetailInput,
    GetSubjectCharactersInput,
    GetSubjectDetailInput,
    GetSubjectEpisodesInput,
    GetSubjectOpinionsInput,
    GetTrendingSubjectsInput,
    GetHotTopicsInput,
    GetUserProfileInput,
    LocalSearchInput,
    SearchBangumiInput,
    UserTimelineInput,
)

logger = logging.getLogger("bgm-agent.tools")

# ═══════════════════════════════════════════════════════════════════
# Intent 上下文（contextvars 传递，不改 ToolNode/Graph 拓扑）
# ═══════════════════════════════════════════════════════════════════

_tool_intent: contextvars.ContextVar[str] = contextvars.ContextVar(
    "tool_intent", default="unknown"
)
"""当前推理轮次的意图分类，由 reasoning_node 设置后自动传播到 ToolNode → 工具函数。
lookup → 全量输出; discovery → 极简输出; 其余 → 默认全量。"""


def set_tool_intent(intent: str) -> None:
    """设置当前工具调用的意图上下文（reasoning_node 在返回前调用）。"""
    _tool_intent.set(intent)


def _get_intent() -> str:
    """读取当前意图（工具函数内部使用，不暴露给 LLM Schema）。"""
    return _tool_intent.get()


# Agent 类型上下文（contextvars 传递，与 _tool_intent 同模式）
_tool_agent_type: contextvars.ContextVar[str] = contextvars.ContextVar(
    "tool_agent_type", default="research"
)
"""当前调用的 Agent 类型，由 reasoning_node 设置。
dialogue → compact 输出; research → 全量输出。"""


def set_tool_agent_type(agent_type: str) -> None:
    """设置当前工具调用的 Agent 类型（reasoning_node 在返回前调用）。"""
    _tool_agent_type.set(agent_type)


def _get_agent_type() -> str:
    """读取当前 Agent 类型（工具函数内部使用）。"""
    return _tool_agent_type.get()

# ═══════════════════════════════════════════════════════════════════
# 常量
# ═══════════════════════════════════════════════════════════════════

_ROLE_MAP: dict[int, str] = {
    1: "角色",
    2: "机体",
    3: "舰船",
    4: "组织机构",
}

_TYPE_ICONS: dict[int, str] = {
    1: "📚",
    2: "📺",
    3: "🎵",
    4: "🎮",
    6: "🎬",
}


# ═══════════════════════════════════════════════════════════════════
# 格式化辅助函数
# ═══════════════════════════════════════════════════════════════════


def _compute_subject_signals(
    rating_count: list[int],
    collection: dict,
    score: float = 0,
) -> list[str]:
    """从评分分布和收藏分布计算派生信号，供 LLM 做推荐判断。

    不硬编码"过誉/冷门"标签——只算数字+自然语言描述，让 LLM 结合语境判断。

    Args:
        rating_count: 10 档评分分布 [1分人数, ..., 10分人数]。
        collection: 5 种收藏状态分布 {1: 想看, 2: 看过, 3: 在看, 4: 搁置, 5: 抛弃}。
        score: 条目均分，用于计算热度评分比。

    Returns:
        人类可读的信号摘要列表。
    """
    signals: list[str] = []
    total_ratings = sum(rating_count)
    if total_ratings <= 0:
        return signals

    # 1. 完成率 = 看过 / (看过+抛弃+搁置)
    看过 = collection.get(2, 0)
    抛弃 = collection.get(5, 0)
    搁置 = collection.get(4, 0)
    total_completed = 看过 + 抛弃 + 搁置
    if total_completed > 100:
        rate = 看过 / total_completed
        if rate >= 0.85:
            signals.append(f"完成率 {rate:.0%}（高——大多坚持看完）")
        elif rate >= 0.60:
            signals.append(f"完成率 {rate:.0%}（正常）")
        elif rate >= 0.35:
            signals.append(f"完成率 {rate:.0%}（偏低——较多中途弃番）")
        else:
            signals.append(f"完成率 {rate:.0%}（低——弃番率高）")

    # 2. 口碑集中度 = 最高三档占比
    top3 = sum(rating_count[-3:])
    top3_ratio = top3 / total_ratings
    if top3_ratio >= 0.75:
        signals.append(f"口碑集中度 {top3_ratio:.0%}（一致好评）")
    elif top3_ratio >= 0.50:
        signals.append(f"口碑集中度 {top3_ratio:.0%}（正常分布）")
    elif top3_ratio >= 0.35:
        signals.append(f"口碑集中度 {top3_ratio:.0%}（两极化——争议较大）")
    else:
        signals.append(f"口碑集中度 {top3_ratio:.0%}（严重两极化）")

    # 3. 热度评分比 = total_ratings / (score * 1000)
    if score > 0:
        ratio = total_ratings / (score * 1000)
        if ratio < 0.3:
            signals.append(f"🔥评分比 {ratio:.1f}（冷门高分——评分高但少人评）")
        elif ratio < 1.0:
            signals.append(f"🔥评分比 {ratio:.1f}（小众精品）")
        elif ratio < 3.0:
            signals.append(f"🔥评分比 {ratio:.1f}（正常热度匹配）")
        else:
            signals.append(f"🔥评分比 {ratio:.1f}（热门——高曝光高评价）")

    return signals





# ═══════════════════════════════════════════════════════════════════
# 名字 → ID 映射
# ═══════════════════════════════════════════════════════════════════


@tool(args_schema=SearchBangumiInput)
async def search_bangumi_subject(
    keyword: str,
    entity_type: str = "subject",
    limit: int = 5,
    subject_type: Optional[int] = None,
    nsfw: Optional[bool] = None,
) -> dict:
    """搜索 Bangumi 条目/角色/人物，返回结构化结果字典。

    当用户想要查找动画、书籍、音乐、游戏、角色或声优时调用此工具。
    返回结果包含 ID，便于后续调用详情类工具进行深度查询。

    典型场景：
    - "帮我搜一下《进击的巨人》" → 确认存在，拿到 ID
    - "花泽香菜配过哪些角色？" → 找到对应人物
    - "推荐几部评分高的科幻动画" → 拿到候选列表 + 评分排名
    - "查一下有没有叫'阿尔托莉雅'的角色" → 消歧同名角色

    Args:
        keyword: 搜索关键词，支持日语、中文、英文等多种语言。
        entity_type: 搜索的实体类型。``subject``=番剧/书籍/音乐/游戏条目，
            ``character``=虚拟角色，``person``=现实人物（声优、导演等）。默认 ``subject``。
        limit: 返回结果的最大条数，默认 5。
        subject_type: 【仅 entity_type=subject 时生效】条目类型过滤：
            1=书籍, 2=动画, 3=音乐, 4=游戏, 6=真人。留空则不限制类型。
        nsfw: 【仅 entity_type=character 时生效】是否包含 NSFW 角色。

    Returns:
        dict::
            {
                "results": [
                    # subject:
                    {"id": int, "name": str, "name_cn": str, "type": str,
                     "score": float, "rank": int, "info": str},
                    # character:
                    {"id": int, "name": str, "name_cn": str, "info": str,
                     "role": str, "nsfw": bool},
                    # person:
                    {"id": int, "name": str, "name_cn": str, "info": str,
                     "type": str, "career": [str], "nsfw": bool}
                ],
                "total": int
            }

        无结果时 ``results`` 为空列表。失败时返回 ``{"_error": "..."}``。
    """
    async with BangumiClient() as client:
        result = await client.search(
            SearchBangumiInput(
                keyword=keyword,
                entity_type=entity_type,
                limit=limit,
                subject_type=subject_type,
                nsfw=nsfw,
            )
        )

    if "_error" in result:
        return {"_error": f"搜索失败。{result['_error']}"}

    return result


# ═══════════════════════════════════════════════════════════════════
# 条目详情
# ═══════════════════════════════════════════════════════════════════


@tool(args_schema=GetSubjectDetailInput)
async def get_bangumi_subject_detail(subject_id: int) -> dict:
    """获取 Bangumi 单个条目的完整详细信息，返回结构化字典。

    当用户需要了解某个条目的完整信息时调用此工具，通常在
    ``search_bangumi_subject`` 之后使用。在用户已明确知道条目 ID
    时也可直接调用。

    典型场景：
    - "这部番评分怎么样？口碑两极吗？" → 看 score/rank/rating_count
    - "导演是谁？谁做的音乐？" → 看 infobox
    - "讲的是什么故事？" → 看 summary
    - "是什么类型？和哪些作品类似？" → 看 tags
    - "热度怎么样？有多少人看完了？" → 看 collection
    - "有没有续作/前传？" → 调 get_subject_relations

    Args:
        subject_id: 条目 ID，即 Bangumi 条目详情页 URL 中的数字编号。
            例如 ``https://bgm.tv/subject/8`` 对应的 ``subject_id`` 为 ``8``。

    Returns:
        dict::
            {
                "id": int, "name": str, "name_cn": str, "type": str,
                "info": str, "date": str, "eps": int, "volumes": int,
                "series": bool, "series_entry": bool, "nsfw": bool,
                "summary": str,
                "score": float, "rank": int, "rating_total": int,
                "rating_count": [int×10],
                "collection": {"想看": int, "看过": int, ...},
                "tags": [{"name": str, "count": int} × 30],
                "infobox": {"导演": str, "原作": str, ...}
            }

        失败时返回 ``{"_error": "..."}``。
    """
    async with BangumiClient() as client:
        result = await client.get_subject_detail(subject_id=subject_id)

    if "_error" in result:
        return {"_error": f"获取条目详情失败。{result['_error']}"}

    return result


# ═══════════════════════════════════════════════════════════════════
# 角色/人物详情
# ═══════════════════════════════════════════════════════════════════


@tool(args_schema=GetCharacterDetailInput)
async def get_character_detail(character_id: int) -> dict:
    """获取 Bangumi 虚拟角色的完整详细信息，返回格式化的自然语言摘要。

    当用户想了解某个角色的完整设定、背景故事、收藏热度时调用此工具。
    角色 ID 可通过两种方式获得：
	    1. ``get_subject_characters`` → 输出中的 [角色ID: xxx]（推荐，无需额外搜索）
	    2. ``search_bangumi_subject(entity_type="character")`` → 输出中的 [ID: xxx]

    典型场景：
    - "阿尔托莉雅这个角色有什么背景故事？"
    - "帮我看看角色 12345 的详细信息"
    - "这个角色在 Bangumi 上有多受欢迎？"
    - "了解一下这个角色的设定"

    Args:
        character_id: 角色 ID。优先从 ``get_subject_characters`` 输出中的 [角色ID: xxx] 获取；也可通过 ``search_bangumi_subject(entity_type="character")`` 搜索获得。

    Returns:
        自然语言格式的角色详情摘要，含角色名、类型、NSFW 标记、简介、
        背景故事、收藏数等关键字段，便于 LLM 直接理解和组织回复。
        失败时返回友好的自然语言错误提示。
    """
    async with BangumiClient() as client:
        result = await client.get_character_detail(character_id=character_id)

    if "_error" in result:
        return {"_error": f"获取角色详情失败。{result['_error']}"}

    return result


@tool(args_schema=GetPersonDetailInput)
async def get_person_detail(person_id: int) -> dict:
    """获取 Bangumi 现实人物（声优、导演、作者等）的完整详细信息，返回格式化的自然语言摘要。

    当用户想了解某位声优/导演/作者的职业背景、代表作列表时调用此工具。
    人物 ID 可通过两种方式获得：
	    1. ``get_subject_characters`` → 输出中的 [人物ID: xxx]（推荐，无需额外搜索）
	    2. ``search_bangumi_subject(entity_type="person")`` → 输出中的 [ID: xxx]

    典型场景：
    - "花泽香菜的个人简介和代表作？"
    - "新房昭之导演过哪些知名作品？"
    - "帮我看看人物 12345 的详细信息"
    - "这位声优配过哪些代表作？"

    Args:
        person_id: 人物 ID。优先从 ``get_subject_characters`` 输出中的 [人物ID: xxx] 获取；也可通过 ``search_bangumi_subject(entity_type="person")`` 搜索获得。

    Returns:
        自然语言格式的人物详情摘要，含人物名、类型、职业、NSFW 标记、
        简介、背景、收藏数等关键字段，便于 LLM 直接理解和组织回复。
        失败时返回友好的自然语言错误提示。
    """
    async with BangumiClient() as client:
        result = await client.get_person_detail(person_id=person_id)

    if "_error" in result:
        return {"_error": f"获取人物详情失败。{result['_error']}"}

    return result


# ═══════════════════════════════════════════════════════════════════
# 番组表（放送排期）
# ═══════════════════════════════════════════════════════════════════


@tool(args_schema=GetCalendarInput)
async def get_calendar(weekday: str = "today", limit_per_day: int = 10) -> dict:
    """获取 Bangumi 每日放送排期，返回结构化字典。

    从 Bangumi 番组表中提取当日或指定日期的放送安排，
    按关注人数降序排列。这是"今天/周X有什么番"的发现工具——
    拿到 id 后可调 ``get_bangumi_subject_detail`` 获取完整信息。

    典型场景：
    - "今天有什么新番更新？"
    - "这周五有哪些番放送？"
    - "看看这周的放送安排"

    Args:
        weekday: 目标星期。``today``=今天（系统日期自动推断），
            ``mon``~``sun``=指定星期几，``all``=整周全部数据。默认 ``today``。
        limit_per_day: 每天最多返回的番剧条目数量，默认 10。

    Returns:
        dict::
            {
                "daily_summary": str,
                "items": [
                    {"id": int, "name": str, "score": float, "watchers": int}
                ]
            }

        失败时返回 ``{"_error": "..."}``。
    """
    async with BangumiClient() as client:
        result = await client.get_calendar(
            GetCalendarInput(weekday=weekday, limit_per_day=limit_per_day)
        )

    if "_error" in result:
        return {"_error": f"获取放送排期失败。{result['_error']}"}

    return result


# ═══════════════════════════════════════════════════════════════════
# 热门趋势
# ═══════════════════════════════════════════════════════════════════


@tool(args_schema=GetTrendingSubjectsInput)
async def get_trending_subjects(
    subject_type: Optional[str] = None,
    limit: int = 10,
) -> dict:
    """获取 Bangumi 全站热门条目排名，返回结构化字典。

    回答"最近什么番/书/游戏最火？"——无需关键词，平台按热度排名。
    拿到 id 后可调 ``get_bangumi_subject_detail`` 获取完整信息。

    典型场景：
    - "最近什么番最火？"
    - "这季度大家都在追什么？"
    - "现在社区热度最高的动画有哪些？"

    Args:
        subject_type: 条目类型过滤。``anime``=动画, ``book``=书籍, ``music``=音乐,
            ``game``=游戏, ``real``=真人。留空则不限制类型。
        limit: 返回条数，默认 10。

    Returns:
        dict::
            {
                "summary": str,
                "items": [
                    {"id": int, "name": str, "type": str,
                     "score": float, "trending_score": int}
                ],
                "total": int
            }

        失败时返回 ``{"_error": "..."}``。
    """
    async with BangumiClient() as client:
        result = await client.get_trending_subjects(
            GetTrendingSubjectsInput(subject_type=subject_type, limit=limit)
        )

    if "_error" in result:
        return {"_error": f"获取热门条目失败。{result['_error']}"}

    return result


@tool(args_schema=GetHotTopicsInput)
async def get_hot_topics(limit: int = 10) -> dict:
    """获取 Bangumi 全站热门讨论帖，返回结构化字典。

    回答"社区在热议什么？"——提取讨论帖标题和关联条目引用。
    拿到 subject_id 后可调 ``get_bangumi_subject_detail`` 了解相关作品。
    注意：帖子 ID 和作者名无下游工具可消费，仅保留标题+条目引用+回复数。

    典型场景：
    - "Bangumi 上最近在热议什么？"
    - "看看社区现在讨论热点"
    - "最近有什么引发争议的话题？"

    Args:
        limit: 返回条数，默认 10。

    Returns:
        dict::
            {
                "items": [
                    {"title": str, "reply_count": int,
                     "subject_name": str, "subject_id": int}
                ],
                "total": int
            }

        失败时返回 ``{"_error": "..."}``。
    """
    async with BangumiClient() as client:
        result = await client.get_hot_topics(GetHotTopicsInput(limit=limit))

    if "_error" in result:
        return {"_error": f"获取热门讨论失败。{result['_error']}"}

    return result


# ═══════════════════════════════════════════════════════════════════
# 单集讨论
# ═══════════════════════════════════════════════════════════════════


@tool(args_schema=GetEpisodeDiscussionInput)
async def get_episode_comments(episode_id: int, comments_limit: int = 30) -> dict:
    """获取 Bangumi 单集详情与吐槽箱评论，返回结构化字典。

    同时拉取单集元数据（集数、标题、简介、所属条目）和社区吐槽，
    帮助 Agent 理解特定单集的内容和观众反应。

    典型场景：
    - "海贼王第 1088 集的吐槽箱里大家都说了什么？"
    - "帮我看看《芙莉莲》第 10 集观众的反应"
    - "这一集风评怎么样？"

    Args:
        episode_id: 单集 ID，可通过 get_subject_episodes 获得。
        comments_limit: 吐槽箱评论的最大拉取条数，默认 30，最大 200。

    Returns:
        dict::
            {
                "episode": {
                    "id": int, "sort": int, "name": str, "airdate": str,
                    "duration": str, "desc": str, "comment_count": int,
                    "subject_id": int, "subject_name": str
                },
                "comments": [str],
                "comment_count": int
            }

        评论已按热度（回应数）降序排列，过滤噪音短评。
        ``comments_error`` 字段出现时表示评论获取失败但 episode 元数据仍可用。
        失败时返回 ``{"_error": "..."}``。
    """
    async with BangumiClient() as client:
        result = await client.get_episode_discussion(
            GetEpisodeDiscussionInput(
                episode_id=episode_id, comments_limit=comments_limit
            )
        )

    if "_error" in result:
        return {"_error": f"获取单集讨论失败。{result['_error']}"}

    return result


# ═══════════════════════════════════════════════════════════════════
# 条目口碑（短评 + 长评）
# ═══════════════════════════════════════════════════════════════════


@tool(args_schema=GetSubjectOpinionsInput)
async def get_subject_opinions(subject_id: int, limit: int = 8) -> dict:
    """获取条目社区口碑：短评 + 长评，返回结构化字典。

    同时拉取两个维度——comments（吐槽箱+评分分布）和 reviews（长评摘要）。
    短评反映整体口碑温度，长评提供深度分析入口（id 可调 get_blog 看全文）。

    典型场景：
    - "大家对《进击的巨人》总体评价怎么样？"
    - "这部番口碑如何？两极吗？"
    - "看看有没有深度分析这篇作品的"

    Args:
        subject_id: Bangumi 条目 ID。
        limit: 每个维度返回的条数，默认 8。

    Returns:
        dict::
            {
                "subject_id": int,
                "comments": {
                    "comments": [str], "rating_distribution": dict,
                    "comment_count": int
                },
                "reviews": {
                    "items": [
                        {"id": int, "title": str, "summary": str,
                         "user_name": str, "reply_count": int, "created_at": str}
                    ], "total": int
                }
            }

        某维度失败时返回 ``"{dim}_error"`` 键。
        整体失败时返回 ``{"_error": "..."}``。
    """
    async with BangumiClient() as client:
        result = await client.get_subject_opinions(
            GetSubjectOpinionsInput(subject_id=subject_id, limit=limit)
        )

    if "_error" in result:
        return {"_error": f"获取条目口碑失败。{result['_error']}"}

    return result


# ═══════════════════════════════════════════════════════════════════
# 条目剧集索引
# ═══════════════════════════════════════════════════════════════════


@tool(args_schema=GetSubjectEpisodesInput)
async def get_subject_episodes(subject_id: int, limit: int = 26) -> dict:
    """获取条目全部主线剧集列表，返回结构化字典。

    按集数升序返回编号、标题、简介。拿到 episode id 后可调
    ``get_episode_comments`` 获取单集详情和吐槽箱。

    典型场景：
    - "列出 EVA 所有集" → 全量
    - "EVA 第18集是哪一集？" → 按 name 定位
    - "找一下讲XX的那一集" → 按 desc 关键词定位 → get_episode_comments

    Args:
        subject_id: Bangumi 条目 ID。
        limit: 返回条数，默认 26（覆盖两季番）。

    Returns:
        dict::
            {
                "subject_id": int,
                "items": [
                    {"id": int, "sort": int, "name": str,
                     "airdate": str, "desc": str, "comment_count": int}
                ],
                "total": int
            }

        失败时返回 ``{"_error": "..."}``。
    """
    async with BangumiClient() as client:
        result = await client.get_subject_episodes(
            GetSubjectEpisodesInput(subject_id=subject_id, limit=limit)
        )

    if "_error" in result:
        return {"_error": f"获取剧集列表失败。{result['_error']}"}

    return result


# ═══════════════════════════════════════════════════════════════════
# 角色/人物评论
# ═══════════════════════════════════════════════════════════════════


@tool(args_schema=GetEntityCommentsInput)
async def get_entity_comments(
    entity_type: str,
    entity_id: int,
    limit: int = 10,
) -> dict:
    """获取虚拟角色或现实人物的社区评论。

    角色和人物的评论接口结构完全一致，统一为一个 Tool，
    通过 entity_type 区分。返回实体名称（精确归属）和
    清洗后的评论列表，LLM 可据此直接引用粉丝原话。

    典型场景：
    - "大家怎么评价阿尔托莉雅这个角色？"
    - "花泽香菜在社区的讨论热度怎么样？"
    - "看看大家对这位声优的评价"

    Args:
        entity_type: 实体类型。``character``=虚拟角色（如'阿尔托莉雅'），
            ``person``=现实人物（如'花泽香菜'、'新房昭之'）。
        entity_id: 角色或人物的 Bangumi ID，可通过
            search_bangumi_subject 以对应的 entity_type 搜索名称获得。
        limit: 拉取的评论最大条数，默认 10。

    Returns:
        dict: {
            "entity_type": "character"|"person",
            "entity_id": int,
            "entity_name": "实体中文名",
            "comments": ["评论1", "评论2", ...],
            "comment_count": N
        }
    """
    async with BangumiClient() as client:
        result = await client.get_entity_comments(
            GetEntityCommentsInput(
                entity_type=entity_type, entity_id=entity_id, limit=limit
            )
        )

    if "_error" in result:
        return {"_error": result["_error"]}

    return result


# ═══════════════════════════════════════════════════════════════════
# 条目角色
# ═══════════════════════════════════════════════════════════════════


@tool(args_schema=GetSubjectCharactersInput)
async def get_subject_characters(subject_id: int) -> dict:
    """获取一部作品的全部登场角色及其声优/演员信息，返回结构化字典。

    返回角色列表，包含角色名、出演类型（主角/配角/客串）、
    饰演者（声优/演员）名称及 ID。这是"主角是谁？""声优是谁？"
    的核心数据源，也是角色/人物详情工具的上游——从这里拿到
    character_id 和 person_id 后，可进一步调用 get_character_detail /
    get_person_detail 获取详情。

    典型场景：
    - "《进击的巨人》有哪些主要角色？" → 按 char_type 过滤主角
    - "鲁路修的声优是谁？" → 找到角色 → 看 casts
    - "这部番的配音阵容怎么样？" → 扫描全部 casts
    - "列出这部作品的角色和对应的CV" → 全量输出

    Args:
        subject_id: Bangumi 条目 ID，可通过 search_bangumi_subject 搜索名称获得。

    Returns:
        dict::
            {
                "subject_id": int,
                "characters": [
                    {
                        "character_id": int, "name": str, "char_type": str,
                        "casts": str
                    }
                ]
            }

        ``casts`` 为 ``"声优名(关系), 声优名, ..."`` 格式的字符串。
        CV 关系省略标签，其他关系（Dub/中配/英配等）标注在括号中。
        声优的 person_id 可通过 ``search_bangumi_subject(entity_type="person")``
        用名字找回。失败时返回 ``{"_error": "..."}``。
    """
    async with BangumiClient() as client:
        result = await client.get_subject_characters(subject_id=subject_id)

    if "_error" in result:
        return {"_error": f"获取条目角色失败。{result['_error']}"}

    return result


# ═══════════════════════════════════════════════════════════════════
# 用户画像（需要 Access Token）
# ═══════════════════════════════════════════════════════════════════


@tool(args_schema=GetUserProfileInput)
async def get_user_profile(
    username: str,
    collections_limit: int = 20,
    include_blogs: bool = True,
    include_characters: bool = False,
    include_persons: bool = False,
) -> dict:
    """获取 Bangumi 用户的多维度画像数据。

    一次调用返回多维度数据：用户基本信息 + 条目收藏 +（可选）角色收藏 +
    人物收藏 + 日志列表。LLM 可据此分析用户的评分偏好、类型倾向、
    角色审美及内容产出风格。

    **认证要求**：需要系统配置有效的 Bangumi Access Token。
    如果 Token 未配置，将返回错误。

    典型场景：
    - "分析一下用户 deepseek_jiang 的看番品味"
    - "这个用户喜欢什么类型的动画？"
    - "某用户的评分习惯是怎样的？"

    Args:
        username: Bangumi 用户名（个人主页 URL 中的用户名部分）。
        collections_limit: 收藏条目拉取的最大数量，默认 20。
        include_blogs: 是否拉取该用户的日志列表。需要 Access Token，默认 True。
        include_characters: 是否拉取该用户收藏的虚拟角色列表，默认 False。
        include_persons: 是否拉取该用户收藏的现实人物列表，默认 False。

    Returns:
        多维度用户画像字典，或 ``{"_error": ...}``。
    """
    token = get_settings().BANGUMI_ACCESS_TOKEN
    if not token:
        return {
            "_error": (
                "系统未配置 Bangumi Access Token，无法获取用户画像。"
                f"您可以直接访问该用户的 Bangumi 主页：https://bgm.tv/user/{username}"
            )
        }

    async with BangumiClient(access_token=token) as client:
        result = await client.get_user_profile(
            GetUserProfileInput(
                username=username,
                collections_limit=collections_limit,
                include_blogs=include_blogs,
                include_characters=include_characters,
                include_persons=include_persons,
            )
        )

    if "_error" in result:
        return {"_error": result["_error"]}

    return result


# ═══════════════════════════════════════════════════════════════════
# 日志分析（需要 Access Token）
# ═══════════════════════════════════════════════════════════════════


@tool(args_schema=GetBlogInput)
async def get_blog(
    entry_id: int,
    include_comments: bool = True,
    include_subjects: bool = True,
) -> dict:
    """获取 Bangumi 日志正文、评论及关联条目的聚合视图。

    一次调用返回三个维度的数据——正文（日志内容）、评论反应（社区观点）、
    关联作品（上下文），让 LLM 能对一篇日志做完整的语义分析。

    **认证要求**：需要系统配置有效的 Bangumi Access Token。

    典型场景：
    - "帮我分析一下这篇日志在讨论什么"
    - "这篇番剧评测的评论区反应如何？"
    - "这篇日志关联了哪些作品？"

    Args:
        entry_id: Bangumi 日志条目 ID，可从 URL ``/blog/{entry_id}`` 中获得。
        include_comments: 是否同时拉取该日志的评论区内容（最近 30 条），默认 True。
        include_subjects: 是否同时拉取该日志关联的条目信息，默认 True。

    Returns:
        日志聚合字典，或 ``{"_error": ...}``。
    """
    token = get_settings().BANGUMI_ACCESS_TOKEN
    if not token:
        return {
            "_error": (
                "系统未配置 Bangumi Access Token，无法获取日志内容。"
                f"您可以直接访问日志页面：https://bgm.tv/blog/{entry_id}"
            )
        }

    async with BangumiClient(access_token=token) as client:
        result = await client.get_blog(
            GetBlogInput(
                entry_id=entry_id,
                include_comments=include_comments,
                include_subjects=include_subjects,
            )
        )

    if "_error" in result:
        return {"_error": result["_error"]}

    return result


# ═══════════════════════════════════════════════════════════════════
# 用户时光机（需要 Access Token）
# ═══════════════════════════════════════════════════════════════════


@tool(args_schema=UserTimelineInput)
async def get_user_timeline(username: str, limit: int = 20) -> dict:
    """获取指定用户的时光机动态（收藏、评分、吐槽等）。

    从用户时光机中提取收藏变更、评分、进度、日志等动态，
    帮助 Agent 理解用户的追番偏好和鉴赏风格。自动过滤无分析价值的
    每日签到事件。

    **认证要求**：需要系统配置有效的 Bangumi Access Token。

    典型场景：
    - "看看 deepseek_jiang 最近在追什么番"
    - "这个用户给哪些番打了高分？"
    - "分析一下某用户的看番品味"

    Args:
        username: Bangumi 用户名（即个人主页 URL 中的用户名部分）。
        limit: 返回动态条数上限，默认 20，最大 50。

    Returns:
        时光机事件字典 ``{"username": ..., "events": [...], "total": N}``，
        或 ``{"_error": ...}``。
    """
    token = get_settings().BANGUMI_ACCESS_TOKEN
    if not token:
        return {
            "_error": (
                "系统未配置 Bangumi Access Token，无法获取用户时光机。"
                f"您可以直接访问该用户的主页：https://bgm.tv/user/{username}"
            )
        }

    async with BangumiClient(access_token=token) as client:
        result = await client.get_user_timeline(username=username, limit=limit)

    if "_error" in result:
        return {"_error": result["_error"]}

    return result


# ═══════════════════════════════════════════════════════════════════
# 本地 RAG 语义检索
# ═══════════════════════════════════════════════════════════════════


@tool(args_schema=LocalSearchInput)
async def search_local_bangumi(
    query: str,
    entity_type: str = "all",
    limit: int = 5,
    nsfw: bool = False,
) -> str:
    """本地语义搜索引擎，基于 RAG 向量检索查找 Bangumi 条目。

    从本地已索引的番剧/角色/声优数据库中，通过语义匹配召回最相关的实体。
    支持按实体类型（subject / character / person / all）领域限定检索，
    并自动根据实体类型选用合适的热度信号做桶内降序排列。

    典型场景：
    - "帮我找一个80年代评分最高的机战番" → entity_type="subject"
    - "有哪些知名的傲娇系角色？" → entity_type="character"
    - "配过最多主角的声优是谁？" → entity_type="person"
    - "和进击的巨人相关的内容有哪些？" → entity_type="all"

    Args:
        query: 自然语言查询，越具体越好。
        entity_type: 实体类型过滤，可选 subject / character / person / all。
        limit: 返回结果数上限，默认 5。
        nsfw: 是否包含 R18 内容，默认 False。

    Returns:
        纯文本格式的检索结果摘要。无结果时返回友好提示。
    """
    import asyncio

    return await asyncio.to_thread(
        _search_local_bangumi_sync, query, entity_type, limit, nsfw
    )


def _search_local_bangumi_sync(
    query: str,
    entity_type: str = "all",
    limit: int = 5,
    nsfw: bool = False,
) -> str:
    """search_local_bangumi 的同步实现，在线程池中运行以避免阻塞事件循环。"""
    try:
        from core.config import get_settings as _get_rag_settings
        from database.engine import engine
        from rag.retriever import RagEntityRetriever
    except ImportError as exc:
        logger.error("RAG 模块导入失败: %s", exc)
        return f"系统提示：本地搜索引擎模块加载失败。错误：{exc}"

    try:
        settings = _get_rag_settings()
        retriever = RagEntityRetriever(
            engine=engine,
            zhipu_api_key=settings.ZHIPU_API_KEY,
        )
    except Exception as exc:
        logger.exception("检索器初始化失败")
        return f"系统提示：本地搜索引擎初始化失败。错误：{exc}"

    try:
        results = retriever.hybrid_search(
            query=query,
            entity_type=entity_type,  # type: ignore[arg-type]
            limit=limit,
            exclude_nsfw=not nsfw,
        )
    except Exception as exc:
        logger.exception("RAG 检索执行失败")
        return f"系统提示：语义检索过程中发生异常。错误：{exc}"

    if not results:
        type_hint = f"（实体类型: {entity_type}）" if entity_type != "all" else ""
        nsfw_hint = "，已排除 R18 内容" if not nsfw else ""
        return (
            f"未找到与「{query}」相关的条目{type_hint}{nsfw_hint}。\n"
            "建议：尝试使用更宽泛的关键词，或切换实体类型后重试。"
        )

    # ── 多态格式化 ──────────────────────────────────────────
    lines: list[str] = [
        f"🔍 关于「{query}」的语义检索结果"
        f"{' (' + entity_type + ')' if entity_type != 'all' else ''}"
        f"（共 {len(results)} 条）：\n"
    ]

    type_icons = {"subject": "📺", "character": "🧑", "person": "🎤"}

    for i, r in enumerate(results, 1):
        try:
            icon = type_icons.get(r.entity_type, "📌")
            meta = r.meta_info

            display_name = r.name
            if r.name_cn and r.name_cn != r.name:
                display_name = f"{r.name}（{r.name_cn}）"

            if r.entity_type == "subject":
                score = meta.get("score", 0)
                rank = meta.get("rank", 0)
                rating_total = meta.get("rating_total", 0)
                heat_str = f"评分 {score:.1f}" if score else ""
                if rank:
                    heat_str += f" | 排名 #{rank}"
                if rating_total:
                    heat_str += f" | {rating_total}人评"
                year = meta.get("year")
                if year:
                    heat_str += f" | {year}年"
                platform = meta.get("platform", "")
                if platform:
                    heat_str += f" | {platform}"
                # 收藏分布 + 派生信号
                collection = meta.get("collection", {})
                rating_count = meta.get("rating_count", [])
                if isinstance(collection, dict) and collection:
                    labels = {1: "想看", 2: "看过", 3: "在看", 4: "搁置", 5: "抛弃"}
                    coll_parts = [f"{labels.get(int(k), k)}:{v}" for k, v in sorted(collection.items()) if v]
                    if coll_parts:
                        heat_str += f" | {' | '.join(coll_parts)}"
                if isinstance(rating_count, list) and rating_count:
                    sigs = _compute_subject_signals(
                        rating_count=rating_count,
                        collection=collection if isinstance(collection, dict) else {},
                        score=score,
                    )
                    if sigs:
                        heat_str += f" | 📊 {'；'.join(sigs)}"
                tags = meta.get("tags", [])
                if isinstance(tags, list) and tags:
                    tag_names = [
                        t.get("name", str(t)) if isinstance(t, dict) else str(t)
                        for t in tags[:5]
                    ]
                    heat_str += f" | 标签: {', '.join(tag_names)}"
            elif r.entity_type == "character":
                collects = meta.get("collects", 0)
                heat_str = f"收藏 {collects}" if collects else ""
                casts = meta.get("casts", [])
                if isinstance(casts, list) and casts:
                    top_works = [
                        c.get("subject_name", "")
                        for c in casts[:3]
                        if c.get("subject_name")
                    ]
                    if top_works:
                        heat_str += f" | 出演: {', '.join(top_works)}"
            elif r.entity_type == "person":
                collects = meta.get("collects", 0)
                career = meta.get("career", [])
                heat_str = f"收藏 {collects}" if collects else ""
                if career:
                    heat_str += f" | 职业: {', '.join(career)}"
                works = meta.get("works", [])
                if isinstance(works, list) and works:
                    top_works = []
                    for w in works[:3]:
                        name = w.get("subject_name", "")
                        positions = w.get("positions", [])
                        if positions:
                            role = positions[0].get("type_cn", "")
                            top_works.append(f"{name}({role})" if role else name)
                        elif name:
                            top_works.append(name)
                    if top_works:
                        heat_str += f" | 代表作: {', '.join(top_works)}"
            else:
                heat_str = ""

            distance_pct = max(0, int((1 - r.cosine_distance) * 100))
            snippet = (
                r.chunk_text[:150] + "..." if len(r.chunk_text) > 150 else r.chunk_text
            )

            lines.append(
                f"{i}. {icon} {display_name} ｜ 匹配度 {distance_pct}%\n"
                f"   {heat_str}\n"
                f"   简介：{snippet}"
            )
        except Exception:
            lines.append(f"{i}. （该条结果格式化失败，已跳过）")

    lines.append("\n── 数据来源：本地 RAG 索引，基于语义匹配和热度排序 ──")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
# 动态工具注册表
# ═══════════════════════════════════════════════════════════════════


def get_agent_tools() -> list:
    """根据当前配置动态返回 Agent 可用工具列表。

    工具注册策略：
    - **无条件注册**（无需 Access Token，11 个）：``search_bangumi_subject``、
      ``get_bangumi_subject_detail``、``get_character_detail``、``get_person_detail``、
      ``get_calendar``、``get_trending_subjects``、``get_hot_topics``、
      ``get_subject_opinions``、``get_subject_episodes``、
      ``get_subject_characters``、``search_local_bangumi``。
    - **条件注册**（需要 ``BANGUMI_ACCESS_TOKEN``，3 个）：``get_user_timeline``、
      ``get_user_profile``、``get_blog``。

    使用方式::

        from tools.bgm_tools import get_agent_tools

        tools = get_agent_tools()
        # tools 现在可以直接传入 LangGraph Agent 的 ToolNode

    Returns:
        LangChain Tool 对象列表。
    """
    tools: list = [
        search_bangumi_subject,
        get_bangumi_subject_detail,
        get_character_detail,
        get_person_detail,
        get_calendar,
        get_trending_subjects,
        get_hot_topics,
        get_episode_comments,
        get_subject_opinions,
        get_subject_episodes,
        get_entity_comments,
        get_subject_characters,
        search_local_bangumi,
    ]

    token = get_settings().BANGUMI_ACCESS_TOKEN
    if token:
        tools.append(get_user_timeline)
        tools.append(get_user_profile)
        tools.append(get_blog)
        logger.info(
            "已启用全部 %d 个 Agent Tools（含需认证的 3 个）",
            len(tools),
        )
    else:
        logger.info(
            "已启用 %d 个 Agent Tools（用户时光机、用户画像、日志因未配置 BANGUMI_ACCESS_TOKEN 而禁用）",
            len(tools),
        )

    return tools
