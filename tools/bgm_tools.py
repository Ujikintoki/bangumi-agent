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

import json
import logging
from typing import Optional

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

    # HATEOAS: 告诉 LLM 下一步可以做什么
    if result.get("results") and entity_type == "subject":
        ids = [r.get("id") for r in result["results"][:3] if r.get("id")]
        result["_next"] = (
            f"拿到 subject_id 后必须调 get_bangumi_subject_detail({ids[0]}) "
            f"获取完整详情。search 只给评分/排名，detail 才有导演/简介/标签。"
        )

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

    # HATEOAS: 告诉 LLM 可选的下游工具
    sid = result.get("id", subject_id)
    result["_next"] = (
        f"如需口碑数据调 get_subject_opinions({sid})；"
        f"如需角色列表调 get_subject_characters({sid})；"
        f"如需集数列表调 get_subject_episodes({sid})"
    )
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

    何时不用：用户问的是声优/导演/作者 → 用 get_person_detail；
              角色 ID 未知 → 先 get_subject_characters 或 search_bangumi_subject；
              不需要完整设定、只需知道"这部番有哪些角色"→ get_subject_characters 已足够。

    典型场景：
    - "阿尔托莉雅这个角色有什么背景故事？"
    - "这个角色在 Bangumi 上有多受欢迎？"

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

    何时不用：用户问的是虚拟角色（如阿尔托莉雅）→ 用 get_character_detail；
              人物 ID 未知 → 先 search_bangumi_subject(entity_type="person")；
              用户只想知道"这部番的导演是谁"→ get_bangumi_subject_detail 的 infobox 字段已包含导演名。

    典型场景：
    - "花泽香菜的个人简介和代表作？"
    - "新房昭之导演过哪些知名作品？"

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

    何时使用：用户问流行趋势、热度排名、大家都在看什么 → 用此工具。
    何时不用：用户问社区讨论/争议话题 → 用 get_hot_topics；
              用户问特定作品的评分 → 用 search_bangumi_subject；本工具用于发现，不用于查单个作品。

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

    何时使用：用户问社区讨论热点、争议话题、大家都在争论什么 → 用此工具。
    何时不用：用户问流行作品排名 → 用 get_trending_subjects；
              用户问特定条目的评价 → 用 get_subject_opinions。

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

    同时拉取单集元数据（集数、标题、简介、所属条目）和社区吐槽。

    何时使用：用户明确指向某一集 → 用此工具。
    何时不用：用户问整体口碑 → 用 get_subject_opinions；
              episode_id 未知 → 先 get_subject_episodes 查集数列表。

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

    何时使用：用户问条目的整体评价、口碑、风评 → 用此工具。
    何时不用：用户问特定单集的评论 → 用 get_episode_comments；
              用户问角色/声优的社区讨论 → 用 get_entity_comments；
              用户只想要评分/排名/基本信息 → search_bangumi_subject 已足够，不需要此工具。

    典型场景：
    - "大家对《进击的巨人》总体评价怎么样？"
    - "这部番口碑如何？两极吗？"
    - "看看有没有深度分析这篇作品的"

    下一步：短评中提到特定单集 → get_episode_comments；长评 id → get_blog。

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

    # HATEOAS: 引导查看单集评论
    if result.get("items"):
        ep_id = result["items"][0].get("id")
        if ep_id:
            result["_next"] = (
                f"每条有 episode id。用户询问单集评价时可调 "
                f"get_episode_comments({ep_id}) 查看吐槽箱"
            )
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
    通过 entity_type 区分。返回实体名称和清洗后的评论列表。

    何时使用：用户问角色（character）或声优/导演（person）的社区评价 → 用此工具。
    何时不用：用户问的是条目的口碑 → 用 get_subject_opinions；
              用户问的是单集评论 → 用 get_episode_comments；
              角色/人物 ID 未知 → 先 search_bangumi_subject(entity_type=...)

    典型场景：
    - "大家怎么评价阿尔托莉雅这个角色？"
    - "花泽香菜在社区的讨论热度怎么样？"

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

    # HATEOAS: 告诉 LLM 如何深入查看角色/声优详情
    if result.get("characters"):
        char_ids = [c.get("character_id") for c in result["characters"] if c.get("character_id")][:3]
        if char_ids:
            result["_next"] = f"可查看角色详情如 get_character_detail({char_ids[0]})；声优可用名字搜索 person_id 后调 get_person_detail"

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

import re as _re

_YEAR_RE = _re.compile(r"(?<!\d)(19|20)\d{2}(?!\d)")
_MIN_SCORE_RE = _re.compile(r"(\d+(?:\.\d+)?)\s*分(?:\s*以上)?")
# tags 只存在于 subject 实体上：实测 subject 1030/1030 有 tags 字段，
# person 0/120、character 0/350（meta_info 里根本没有这个键）。
# 给非 subject 查询一个 tag 要求 = 逐条 AND 一个永不匹配的条件 → 必然返回 0 条
# （实测 "声优" 查 person：带 tags 返回 0 条，去掉后 career 过滤返回 55 条 = GT）。
# 工具 schema 亦已声明 tags「仅 entity_type=subject 时可用」，此处与之对齐。
# "all" 不在豁免之列：不加实体过滤，subject 也在结果里，tags 仍然成立。
_TAGS_APPLICABLE = frozenset({"subject", "all"})

_CAREER_MAP = {
    "声优": "seiyu",
    "歌手": "artist",
    "动画制作人": "producer",
    "制作人": "producer",
    "导演": "director",
    "漫画家": "manga_artist",
    "作家": "writer",
    "音乐人": "musician",
}


def _drop_substring_tags(matched: list[str]) -> list[str]:
    """丢弃被同一批更长命中标签包含的短标签。

    词表匹配是【子串】匹配，所以 query="TRIGGER" 会连带命中 "GE"、"IG"，
    "CloverWorks" 会连带命中 "love"。而 keyword_search 对每个标签是逐条
    AND（jsonb contains），每多一个冗余标签就收紧一次结果集 —— 实测：

        ['TRIGGER', 'GE', 'IG']            → 0 条
        ['TRIGGER']                        → 14 条
        ['CloverWorks', 'love']            → 0 条（GT 24 条）
        ['CloverWorks']                    → 24 条

    短标签是长标签的子串时，它没有携带额外信息，只把结果集 AND 空。
    """
    return [t for t in matched if not any(t != o and t in o for o in matched)]


def _extract_keyword_filters(
    query: str,
    tags: Optional[list[str]] = None,
    year: Optional[int] = None,
    min_score: Optional[float] = None,
    entity_type: str = "subject",
) -> dict:
    """从 query 文本中提取结构化关键词参数，LLM 传参优先，规则兜底。

    提取策略（每种字段独立补缺，LLM 已传的值不被覆盖）：
      - tags: LLM 传参优先 → 规则：load_tag_vocabulary() 子串匹配
              （长度≥2, 去子串冗余, 上限5）
              **仅 entity_type ∈ _TAGS_APPLICABLE 时产出**，见该常量
      - year: LLM 传参优先 → 规则：正则 \\b(19|20)\\d{2}\\b
      - min_score: LLM 传参优先 → 规则：正则 "X.X 分"
      - career: 硬编码 _CAREER_MAP 子串扫描

    Args:
        query: 用户原始查询文本。
        tags: LLM 传入的标签列表。
        year: LLM 传入的年份。
        min_score: LLM 传入的评分下限。
        entity_type: 本次搜索的实体类型，决定 tags 是否适用。

    Returns:
        仅包含非空字段的过滤参数 dict。
    """
    filters: dict = {}

    # ── tags: LLM 优先 → 标签词表子串兜底（仅 subject 适用） ──
    if entity_type in _TAGS_APPLICABLE:
        final_tags = list(tags) if tags else []
        if not final_tags:
            try:
                from rag._tag_dict import load_tag_vocabulary

                vocab = load_tag_vocabulary()
                matched = [t for t in vocab if len(t) >= 2 and t in query]
                # 去冗余：子串匹配会连带命中短标签，而逐条 AND 会把结果集收紧到空
                matched = _drop_substring_tags(matched)
                # 确定性排序：长度降序（长标签更具体）→ 名称升序。
                # frozenset 迭代顺序受哈希随机化影响，直接切片会取到不确定的 5 个。
                final_tags = sorted(matched, key=lambda t: (-len(t), t))[:5]
            except Exception:
                pass
        if final_tags:
            filters["required_tags"] = final_tags

    # ── year: LLM 优先 → 正则兜底 ──
    final_year = year
    if final_year is None:
        ym = _YEAR_RE.search(query)
        if ym:
            final_year = int(ym.group())
    if final_year is not None:
        filters["year"] = final_year

    # ── score: LLM 优先 → 正则兜底 ──
    final_score = min_score
    if final_score is None:
        sm = _MIN_SCORE_RE.search(query)
        if sm:
            final_score = float(sm.group(1))
    if final_score is not None:
        filters["min_score"] = final_score

    # ── career: 硬编码子串扫描 ──
    for cn, key in _CAREER_MAP.items():
        if cn in query:
            filters["career"] = key
            break

    return filters


def _unified_search(
    retriever,
    query: str,
    entity_type: str,
    subject_type: Optional[int],
    limit: int,
    exclude_nsfw: bool,
    keyword_filters: dict,
) -> list:
    """三通道合并检索：关键词精确 > trigram 名称 > 向量语义。

    每通道独立 try/except，一个通道失败不影响其他通道。
    按 entity_id 去重（后续通道不覆盖先前通道已命中的结果）。

    Args:
        retriever: RagEntityRetriever 实例。
        query: 用户查询文本。
        entity_type: 实体类型。
        subject_type: Subject 子类型。
        limit: 返回数量上限。
        exclude_nsfw: 是否排除 NSFW。
        keyword_filters: _extract_keyword_filters 的输出。

    Returns:
        RagSearchResult 列表（最多 limit 条）。
    """
    merged: list = []
    seen: set[str] = set()

    # 通道 1: 关键词精确匹配
    if keyword_filters:
        try:
            keyword_results = retriever.keyword_search(
                entity_type=entity_type,
                subject_type=subject_type,
                limit=limit,
                exclude_nsfw=exclude_nsfw,
                **keyword_filters,
            )
            for r in keyword_results:
                if r.entity_id not in seen:
                    merged.append(r)
                    seen.add(r.entity_id)
        except Exception as exc:
            logger.warning("关键词通道失败（降级继续）: %s", exc)

    # 通道 2: trigram 名称匹配
    if len(merged) < limit:
        try:
            name_results = retriever.name_search(
                query=query,
                entity_type=entity_type,
                subject_type=subject_type,
                limit=limit,
                exclude_nsfw=exclude_nsfw,
            )
            for r in name_results:
                if r.entity_id not in seen:
                    merged.append(r)
                    seen.add(r.entity_id)
        except Exception as exc:
            logger.warning("名称通道失败（降级继续）: %s", exc)

    # 通道 3: 向量语义匹配
    if len(merged) < limit:
        try:
            vector_results = retriever.hybrid_search(
                query=query,
                entity_type=entity_type,
                subject_type=subject_type,
                limit=limit,
                exclude_nsfw=exclude_nsfw,
            )
            for r in vector_results:
                if r.entity_id not in seen:
                    merged.append(r)
                    seen.add(r.entity_id)
        except Exception as exc:
            logger.warning("向量通道失败（降级返回已有结果）: %s", exc)

    return merged[:limit]


@tool(args_schema=LocalSearchInput)
async def search_local_bangumi(
    query: str,
    entity_type: str = "all",
    limit: int = 5,
    nsfw: bool = False,
    subject_type: Optional[int] = None,
    tags: Optional[list[str]] = None,
    year: Optional[int] = None,
    min_score: Optional[float] = None,
) -> dict:
    """本地语义搜索引擎 — 三通道混合检索：关键词精确匹配 > trigram 名称 > 向量语义。

    三通道：
      1. 关键词精确：用 tags/year/score/career 做 JSONB 结构化匹配（最高优先级）
      2. trigram 名称：用 pg_trgm 模糊匹配作品名/人名
      3. 向量语义：用 embedding 做语义相似度匹配（兜底）

    何时使用（满足任一）：
      - 用户按标签/类型/年份/评分筛选作品（"芳文社的动画""2023年""评分 8 分以上"）
      - 用户描述氛围/风格/主题找作品（"治愈""热血""悬疑推理"）
      - 用户找"类似XX"的推荐

    何时不用——请用 search_bangumi_subject 代替：
      - 用户指定了精确作品名/人名且只需要基本信息（"EVA 评分"）
      - 用户需要实时数据（评分、排名、在播状态——本地索引可能过期）

    Args:
        query: 自然语言查询，如 "温馨的日常治愈番"。
        entity_type: subject / character / person / all。
        limit: 返回结果数上限，默认 5。
        nsfw: 是否包含 R18 内容，默认 False。
        subject_type: 条目子类型：1=书籍, 2=动画, 3=音乐, 4=游戏, 6=真人。
        tags: 必须同时包含的标签（AND 逻辑），如 ['芳文社', '原创']。
        year: 年份精确匹配，如 2023。
        min_score: 评分下限 (0-10)，如 8.5。

    Returns:
        结构化 dict: {"results": [dict, ...], "total": N}，
        每个 dict 格式对应 API detail 工具。
        无结果或出错时返回 {"_error": "..."}。
    """
    import asyncio
    return await asyncio.to_thread(
        _search_local_bangumi_sync,
        query, entity_type, limit, nsfw,
        subject_type, tags, year, min_score,
    )


def _search_local_bangumi_sync(
    query: str,
    entity_type: str = "all",
    limit: int = 5,
    nsfw: bool = False,
    subject_type: Optional[int] = None,
    tags: Optional[list[str]] = None,
    year: Optional[int] = None,
    min_score: Optional[float] = None,
) -> dict:
    """search_local_bangumi 的同步实现 — 三通道合并检索。"""
    try:
        from core.config import get_settings as _get_rag_settings
        from database.engine import engine
        from rag.retriever import RagEntityRetriever
    except ImportError as exc:
        logger.error("RAG 模块导入失败: %s", exc)
        return {"_error": f"本地搜索引擎模块加载失败。{exc}"}

    try:
        settings = _get_rag_settings()
        retriever = RagEntityRetriever(
            engine=engine,
            zhipu_api_key=settings.ZHIPU_API_KEY,
        )
    except Exception as exc:
        logger.exception("检索器初始化失败")
        return {"_error": f"本地搜索引擎初始化失败。{exc}"}

    # ── 关键词提取：LLM 参数优先 + 规则兜底 ──
    keyword_filters = _extract_keyword_filters(
        query=query, tags=tags, year=year, min_score=min_score,
        entity_type=entity_type,
    )

    # ── 三通道合并检索 ──
    try:
        results = _unified_search(
            retriever=retriever,
            query=query,
            entity_type=entity_type,
            subject_type=subject_type,
            limit=limit,
            exclude_nsfw=not nsfw,
            keyword_filters=keyword_filters,
        )
    except Exception as exc:
        logger.exception("RAG 检索执行失败")
        return {"_error": f"语义检索过程中发生异常。{exc}"}

    if not results:
        return {"_error": f"未找到与「{query}」相关的条目，建议尝试更宽泛的关键词。"}

    formatted: list[dict] = []
    for r in results:
        try:
            formatted.append(json.loads(r.rag_output))
        except (json.JSONDecodeError, TypeError):
            logger.warning("RAG rag_output JSON 解析失败: entity=%s id=%s", r.entity_type, r.entity_id)

    if not formatted:
        return {"_error": f"检索到 {len(results)} 条结果但解析均失败。"}

    return {"results": formatted, "total": len(formatted)}



# ═══════════════════════════════════════════════════════════════════
# 动态工具注册表
# ═══════════════════════════════════════════════════════════════════


def get_agent_tools() -> list:
    """根据当前配置动态返回 Agent 可用工具列表。

    工具注册策略：
    - **无条件注册**（无需 Access Token，13 个）：``search_bangumi_subject``、
      ``get_bangumi_subject_detail``、``get_character_detail``、``get_person_detail``、
      ``get_calendar``、``get_trending_subjects``、``get_hot_topics``、
      ``get_episode_comments``、``get_subject_opinions``、``get_subject_episodes``、
      ``get_entity_comments``、``get_subject_characters``、``search_local_bangumi``。
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
