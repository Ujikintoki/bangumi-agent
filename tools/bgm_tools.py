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
    GetSubjectDiscussionInput,
    GetTrendingInput,
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


def _format_search_results(
    results: list[dict],
    total: int,
    keyword: str,
    entity_type: str,
    intent: str = "unknown",
) -> str:
    """将搜索结果格式化为易读文本，避免裸 JSON 进入 LLM 上下文。

    根据 entity_type 自适应展示不同字段：
    - subject: 评分、排名、类型图标
    - character: 角色类型、NSFW 标记
    - person: 职业

    discovery 模式下缩短引导语，减少 token 噪音。
    """
    if not results:
        return (
            f"未找到与「{keyword}」相关的"
            f"{'条目' if entity_type == 'subject' else '角色' if entity_type == 'character' else '人物'}，"
            f"请尝试更换关键词。"
        )

    lines: list[str] = [f"🔍 「{keyword}」的搜索结果（共 {total} 条）：\n"]

    for i, item in enumerate(results, 1):
        item_id = item.get("id", 0)
        name = item.get("name_cn") or item.get("name", "未知")
        orig_name = item.get("name", "")
        display = f"{name}（{orig_name}）" if (name != orig_name and orig_name) else name

        etype = item.get("entity_type", entity_type)

        if etype == "subject":
            type_icon = _TYPE_ICONS.get(item.get("type_id", 0), "📌")
            score = item.get("score", 0)
            rank = item.get("rank", 0)
            extras: list[str] = []
            if score:
                extras.append(f"评分 {score:.1f}")
            if rank:
                extras.append(f"排名 #{rank}")
            extra_str = f" — {' | '.join(extras)}" if extras else ""
            lines.append(f"{i}. {type_icon} {display}{extra_str}  [ID: {item_id}]")

        elif etype == "character":
            role = item.get("role", "")
            role_str = f"（{role}）" if role and role != "未知" else ""
            nsfw_tag = " 🔞" if item.get("nsfw") else ""
            lines.append(f"{i}. 🧑 {display}{role_str}{nsfw_tag}  [ID: {item_id}]")

        elif etype == "person":
            career = item.get("career", "")
            career_str = f" — {career}" if career else ""
            lines.append(f"{i}. 🎤 {display}{career_str}  [ID: {item_id}]")

        else:
            lines.append(f"{i}. {display}  [ID: {item_id}]")

    # discovery 模式：引导语缩短（~8 tokens vs ~25 tokens）
    if intent == "discovery":
        lines.append("\n── 使用详情工具获取更多信息 ──")
    else:
        lines.append(
            f"\n── 使用详情工具（get_bangumi_subject_detail / get_entity_comments"
            f"{' / get_subject_characters' if entity_type == 'subject' else ''}）"
            f"获取完整信息 ──"
        )
    return "\n".join(lines)


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


def _format_subject_detail(detail: dict, intent: str = "unknown") -> str:
    """将条目详情格式化为易读文本，按意图分化输出。

    - **discovery**：极简模式（~45 tokens/部）——仅保留比较筛选必需字段
    - **其余 intent**：全量模式——评分分布、收藏、信号、简介、标签
    """
    if intent == "discovery":
        return _format_subject_detail_discovery(detail)
    return _format_subject_detail_full(detail)


def _format_subject_detail_full(detail: dict) -> str:
    """全量条目详情（lookup/factual/realtime 等精确场景）。"""
    name = detail.get("name_cn") or detail.get("name", "未知")
    orig_name = detail.get("name", "")
    display = f"{name}（{orig_name}）" if (name != orig_name and orig_name) else name

    lines: list[str] = [f"📺 {display}"]

    # ── 评分行 ──────────────────────────────────────────────
    score = detail.get("score", 0)
    rank = detail.get("rank", 0)
    total_ratings = detail.get("total_rating_count", 0)
    meta: list[str] = []
    if score:
        meta.append(f"评分 {score:.1f}")
    if rank:
        meta.append(f"排名 #{rank}")
    if total_ratings:
        meta.append(f"{total_ratings} 人评")
    if meta:
        lines.append(f"{' | '.join(meta)}")

    # ── 评分分布（判断口碑是否两极化）──────────────────────
    rating_count = detail.get("rating_count", [])
    if rating_count and sum(rating_count) > 0:
        compact = " ".join(
            f"{i+1}分:{c}" for i, c in enumerate(rating_count) if c > 0
        )
        lines.append(f"评分分布：{compact}")

    # ── 收藏分布（识别冷门神作 vs 过誉热门）─────────────────
    collection = detail.get("collection", {})
    if collection:
        labels = {1: "想看", 2: "看过", 3: "在看", 4: "搁置", 5: "抛弃"}
        parts = [f"{labels.get(int(k), k)}:{v}" for k, v in sorted(collection.items())]
        lines.append(f"收藏分布：{' | '.join(parts)}")

    # ── 派生信号（完成率 / 口碑集中度 / 热度评分比）────────
    signals = _compute_subject_signals(
        rating_count=rating_count if rating_count else [],
        collection=collection if collection else {},
        score=score,
    )
    if signals:
        lines.append(f"📊 信号：{'；'.join(signals)}")

    # ── 类型/集数行 ──────────────────────────────────────────
    type_name = detail.get("type", "")
    eps = detail.get("eps", 0)
    sub: list[str] = []
    if type_name:
        sub.append(f"类型: {type_name}")
    if eps:
        sub.append(f"集数: {eps}")
    if sub:
        lines.append(f"{' | '.join(sub)}")

    # ── 简介 ────────────────────────────────────────────────
    summary = detail.get("summary", "")
    if summary:
        lines.append(f"\n简介：{summary}")

    # ── 标签 ────────────────────────────────────────────────
    tags = detail.get("tags", [])
    if tags:
        tag_strs = [
            f"{t['name']}{'(' + str(t['count']) + ')' if t.get('count') else ''}"
            for t in tags[:10]
        ]
        lines.append(f"\n标签：{', '.join(tag_strs)}")

    subject_id = detail.get("id", 0)
    lines.append(f"\n── 条目 {subject_id} 详情 ──")
    return "\n".join(lines)


def _format_subject_detail_discovery(detail: dict) -> str:
    """发现模式条目详情：极简一行，仅保留比较和筛选必需字段。

    输出格式（~45 tokens/部）：::

        进击的巨人 | ★8.5 | #2 | 动画 25集 | id:8 [机战,科幻,热血]

    刻意砍掉的字段及理由：
    - 原文名：比较阶段用中文名够用
    - 评分分布/收藏分布/派生信号：比较阶段不需要统计细节
    - 简介：top-3 标签已提供类型线索，无需百字长文
    - 评分人数：5 部横向对比的绝对数字信息密度低
    """
    name = detail.get("name_cn") or detail.get("name", "??")
    score = detail.get("score", 0)
    rank = detail.get("rank", 0)
    type_name = detail.get("type", "")
    eps = detail.get("eps", 0)
    item_id = detail.get("id", 0)

    # top-3 标签作为类型/风格线索
    tags = detail.get("tags", [])
    tag_str = ""
    if tags:
        top_tags = [t["name"] for t in tags[:3] if t.get("name")]
        if top_tags:
            tag_str = f" [{'/'.join(top_tags)}]"

    parts: list[str] = [name]
    if score:
        parts.append(f"★{score:.1f}")
    if rank:
        parts.append(f"#{rank}")
    fmt = f"{type_name} {eps}集" if eps else type_name
    parts.append(fmt)
    parts.append(f"id:{item_id}")

    return " | ".join(parts) + tag_str


def _format_character_detail(detail: dict) -> str:
    """将角色详情格式化为易读文本，避免裸 JSON 进入 LLM 上下文。"""
    name = detail.get("name_cn") or detail.get("name", "未知角色")
    orig_name = detail.get("name", "")
    display = f"{name}（{orig_name}）" if (name != orig_name and orig_name) else name

    lines: list[str] = [f"🧑 {display}"]

    # ── 基本信息 ──────────────────────────────────────────────
    role = detail.get("role", "")
    nsfw_tag = " 🔞" if detail.get("nsfw") else ""
    info = detail.get("info", "")
    meta: list[str] = []
    if role:
        meta.append(f"类型: {role}")
    collects = detail.get("collects", 0)
    if collects:
        meta.append(f"{collects} 人收藏")
    if meta:
        lines.append(f"{' | '.join(meta)}{nsfw_tag}")
    if info:
        lines.append(f"简介：{info}")

    # ── 详细背景 ────────────────────────────────────────────────
    summary = detail.get("summary", "")
    if summary:
        lines.append(f"\n背景：{summary}")

    character_id = detail.get("id", 0)
    lines.append(f"\n── 角色 {character_id} 详情 ──")
    return "\n".join(lines)


def _format_person_detail(detail: dict) -> str:
    """将人物详情格式化为易读文本，避免裸 JSON 进入 LLM 上下文。"""
    name = detail.get("name_cn") or detail.get("name", "未知人物")
    orig_name = detail.get("name", "")
    display = f"{name}（{orig_name}）" if (name != orig_name and orig_name) else name

    lines: list[str] = [f"🎤 {display}"]

    # ── 基本信息 ──────────────────────────────────────────────
    person_type = detail.get("type", "")
    career = detail.get("career", "")
    nsfw_tag = " 🔞" if detail.get("nsfw") else ""
    meta: list[str] = []
    if person_type:
        meta.append(f"类型: {person_type}")
    if career:
        meta.append(f"职业: {career}")
    collects = detail.get("collects", 0)
    if collects:
        meta.append(f"{collects} 人收藏")
    if meta:
        lines.append(f"{' | '.join(meta)}{nsfw_tag}")

    info = detail.get("info", "")
    if info:
        lines.append(f"简介：{info}")

    # ── 详细背景 ────────────────────────────────────────────────
    summary = detail.get("summary", "")
    if summary:
        lines.append(f"\n背景：{summary}")

    person_id = detail.get("id", 0)
    lines.append(f"\n── 人物 {person_id} 详情 ──")
    return "\n".join(lines)


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
) -> str:
    """搜索 Bangumi 条目/角色/人物，返回精简结果列表（JSON 格式字符串）。

    当用户想要查找动画、书籍、音乐、游戏、角色或声优时调用此工具。
    返回结果包含 ID，便于后续调用详情类工具进行深度查询。

    典型场景：
    - "帮我搜一下《进击的巨人》"
    - "花泽香菜配过哪些角色？"
    - "推荐几部评分高的科幻动画"
    - "查一下有没有叫'阿尔托莉雅'的角色"

    Args:
        keyword: 搜索关键词，支持日语、中文、英文等多种语言。
        entity_type: 搜索的实体类型。``subject``=番剧/书籍/音乐/游戏条目，
            ``character``=虚拟角色，``person``=现实人物（声优、导演等）。
            默认 ``subject``。
        limit: 返回结果的最大条数，默认 5。
        subject_type: 【仅 entity_type=subject 时生效】条目类型过滤：
            1=书籍, 2=动画, 3=音乐, 4=游戏, 6=真人。留空则不限制类型。
        nsfw: 【仅 entity_type=character 时生效】是否包含 NSFW 角色。
            留空由 API 默认行为决定。

    Returns:
        JSON 格式字符串。成功时为一个包含 results 数组的对象；
        失败时返回自然语言错误提示。
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
        return f"系统提示：搜索失败。{result['_error']}"

    results = result.get("results", [])
    total = result.get("total", len(results))
    if not results:
        return (
            f"未找到与「{keyword}」相关的"
            f"{'条目' if entity_type == 'subject' else '角色' if entity_type == 'character' else '人物'}，"
            f"请尝试更换关键词或调整搜索条件。"
        )

    intent = _get_intent()
    return _format_search_results(results, total, keyword, entity_type, intent=intent)


# ═══════════════════════════════════════════════════════════════════
# 条目详情
# ═══════════════════════════════════════════════════════════════════


@tool(args_schema=GetSubjectDetailInput)
async def get_bangumi_subject_detail(subject_id: int) -> str:
    """获取 Bangumi 单个条目的完整详细信息（JSON 格式字符串）。

    当用户需要了解某个条目的完整信息时调用此工具，通常在
    ``search_bangumi_subject`` 之后使用。在用户已明确知道条目 ID
    时也可直接调用。

    典型场景：
    - "帮我看看编号 12345 这个番的详情"
    - "这部动画有多少集？什么时候播出的？"
    - "查一下这个条目的评分和收藏情况"

    返回的 JSON 中包含该条目的：
    - 基本信息和评分（name、name_cn、score、rank）
    - 播出/发售日期
    - 章节总数、简介、标签
    - 收藏统计

    Args:
        subject_id: 条目 ID，即 Bangumi 条目详情页 URL 中的数字编号。
            例如 ``https://bgm.tv/subject/8`` 对应的 ``subject_id`` 为 ``8``。

    Returns:
        JSON 格式字符串。成功时为一个包含完整条目信息的对象；
        失败时返回自然语言错误提示。
    """
    async with BangumiClient() as client:
        result = await client.get_subject_detail(subject_id=subject_id)

    if "_error" in result:
        return f"系统提示：获取条目详情失败。{result['_error']}"

    intent = _get_intent()
    return _format_subject_detail(result, intent=intent)


# ═══════════════════════════════════════════════════════════════════
# 角色/人物详情
# ═══════════════════════════════════════════════════════════════════


@tool(args_schema=GetCharacterDetailInput)
async def get_character_detail(character_id: int) -> str:
    """获取 Bangumi 虚拟角色的完整详细信息（JSON 格式字符串）。

    当用户想了解某个角色的完整设定、背景故事、收藏热度时调用此工具。
    通常在 ``search_bangumi_subject(entity_type="character")`` 定位角色后使用。

    典型场景：
    - "阿尔托莉雅这个角色有什么背景故事？"
    - "帮我看看角色 12345 的详细信息"
    - "这个角色在 Bangumi 上有多受欢迎？"
    - "了解一下这个角色的设定"

    Args:
        character_id: 角色 ID，可通过 search_bangumi_subject(keyword=角色名, entity_type="character") 搜索获得。

    Returns:
        JSON 格式字符串。成功时包含角色简介、背景故事、收藏数等完整信息；
        失败时返回自然语言错误提示。
    """
    async with BangumiClient() as client:
        result = await client.get_character_detail(character_id=character_id)

    if "_error" in result:
        return f"系统提示：获取角色详情失败。{result['_error']}"

    return _format_character_detail(result)


@tool(args_schema=GetPersonDetailInput)
async def get_person_detail(person_id: int) -> str:
    """获取 Bangumi 现实人物（声优、导演、作者等）的完整详细信息。

    当用户想了解某位声优/导演/作者的职业背景、代表作列表时调用此工具。
    通常在 ``search_bangumi_subject(entity_type="person")`` 定位人物后使用。

    典型场景：
    - "花泽香菜的个人简介和代表作？"
    - "新房昭之导演过哪些知名作品？"
    - "帮我看看人物 12345 的详细信息"
    - "这位声优配过哪些代表作？"

    Args:
        person_id: 人物 ID，可通过 search_bangumi_subject(keyword=人物名, entity_type="person") 搜索获得。

    Returns:
        JSON 格式字符串。成功时包含人物简介、职业标签、代表作列表、收藏数等；
        失败时返回自然语言错误提示。
    """
    async with BangumiClient() as client:
        result = await client.get_person_detail(person_id=person_id)

    if "_error" in result:
        return f"系统提示：获取人物详情失败。{result['_error']}"

    return _format_person_detail(result)


# ═══════════════════════════════════════════════════════════════════
# 番组表（放送排期）
# ═══════════════════════════════════════════════════════════════════


@tool(args_schema=GetCalendarInput)
async def get_calendar(weekday: str = "today", limit_per_day: int = 10) -> str:
    """获取 Bangumi 每日放送排期，展示当日热门番剧列表。

    从 Bangumi 番组表中提取当日或指定日期的放送安排，
    按关注人数降序排列，帮助用户了解"今天有什么番可以看"。

    典型场景：
    - "今天有什么新番更新？"
    - "这周五有哪些番放送？"
    - "看看这周的放送安排"

    Args:
        weekday: 目标星期。``today``=今天（系统日期自动推断），
            ``mon``~``sun``=指定星期几，``all``=整周全部数据。默认 ``today``。
        limit_per_day: 每天最多返回的番剧条目数量，默认 10。

    Returns:
        纯文本格式的放送排期摘要，包含番剧名称、评分和关注人数。
    """
    async with BangumiClient() as client:
        result = await client.get_calendar(
            GetCalendarInput(weekday=weekday, limit_per_day=limit_per_day)
        )

    if "_error" in result:
        return f"系统提示：获取放送排期失败。{result['_error']}"

    items = result.get("items", [])
    summary = result.get("daily_summary", "")

    if not items:
        return summary or "当前没有放送数据。"

    weekday_labels = {
        "mon": "周一", "tue": "周二", "wed": "周三", "thu": "周四",
        "fri": "周五", "sat": "周六", "sun": "周日",
    }
    label = weekday_labels.get(weekday, "") if weekday != "today" and weekday != "all" else ""

    lines: list[str] = [f"📅 {summary}\n" if label else f"📅 {summary}\n"]
    for i, item in enumerate(items, 1):
        name = item.get("name", "未知")
        score = item.get("score", 0)
        watchers = item.get("watchers", 0)
        score_str = f"评分 {score:.1f}" if score else "暂无评分"
        lines.append(f"{i}. {name} — {score_str} | {watchers} 人想看")

    lines.append(f"\n── 以上为{'今日' if weekday == 'today' else label + '的' if label else ''}放送排期 TOP {len(items)} ──")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
# 热门趋势
# ═══════════════════════════════════════════════════════════════════


@tool(args_schema=GetTrendingInput)
async def get_trending_topics(
    category: str = "both",
    subject_type: Optional[str] = None,
    limit: int = 10,
) -> str:
    """获取 Bangumi 全站热门条目和/或讨论帖风向标。

    从全站热门趋势中提取条目名称、评分、讨论标题等关键信息，
    帮助 Agent 感知社区当前讨论热度最高的作品和话题。

    典型场景：
    - "最近什么番最火？"
    - "这季度大家都在追什么？"
    - "现在社区热度最高的动画有哪些？"
    - "看看最近热议的话题"

    Args:
        category: 热门维度。``subjects``=热门条目排行，``topics``=热门讨论帖排行，
            ``both``=两者都拉取。默认 ``both``。
        subject_type: 【仅 category 含 subjects 时生效】条目类型过滤：
            ``anime``=动画, ``book``=书籍, ``music``=音乐, ``game``=游戏, ``real``=真人。
            留空则不限制类型。
        limit: 每个维度返回的最大条数，默认 10。

    Returns:
        纯文本格式的热门趋势摘要，包含条目名称、评分或讨论帖信息。
    """
    async with BangumiClient() as client:
        result = await client.get_trending(
            GetTrendingInput(
                category=category, subject_type=subject_type, limit=limit
            )
        )

    if "_error" in result:
        return f"系统提示：获取热门趋势失败。{result['_error']}"

    lines: list[str] = []

    # ── 热门条目 ──────────────────────────────────────────────
    subjects_data = result.get("subjects", {})
    if isinstance(subjects_data, dict) and "_error" not in subjects_data:
        items = subjects_data.get("items", [])
        summary = subjects_data.get("summary", "")
        if items:
            lines.append(f"🔥 {summary}\n")
            for i, item in enumerate(items, 1):
                name = item.get("name", "未知作品")
                score = item.get("score", 0)
                icon = _TYPE_ICONS.get(item.get("type", 0), "📌")
                score_str = f"评分 {score:.1f}" if score else "暂无评分"
                lines.append(f"{i}. {icon} {name} — {score_str}")
    elif isinstance(subjects_data, dict) and "_error" in subjects_data:
        lines.append(f"⚠️ 热门条目获取失败：{subjects_data['_error']}")

    # ── 热门讨论 ──────────────────────────────────────────────
    topics_data = result.get("topics", {})
    if isinstance(topics_data, dict) and "_error" not in topics_data:
        items = topics_data.get("items", [])
        if items:
            if lines:
                lines.append("")
            lines.append(f"💬 热门讨论帖（共 {len(items)} 条）：\n")
            for i, item in enumerate(items, 1):
                title = item.get("title", "无标题")
                replies = item.get("reply_count", 0)
                creator = item.get("creator_name", "匿名")
                lines.append(f"{i}. {title} — {replies} 回复 | 作者: {creator}")
    elif isinstance(topics_data, dict) and "_error" in topics_data:
        lines.append(f"⚠️ 热门讨论获取失败：{topics_data['_error']}")

    if not lines:
        return "当前没有热门趋势数据，请稍后再试。"

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
# 单集讨论
# ═══════════════════════════════════════════════════════════════════


@tool(args_schema=GetEpisodeDiscussionInput)
async def get_episode_comments(episode_id: int, comments_limit: int = 30) -> str:
    """获取 Bangumi 单集详情与吐槽箱评论。

    同时拉取单集的元数据（集数、标题、简介）和社区吐槽，
    帮助 Agent 理解特定单集的内容和观众反应。

    典型场景：
    - "海贼王第 1088 集的吐槽箱里大家都说了什么？"
    - "帮我看看《芙莉莲》第 10 集观众的反应"
    - "这一集风评怎么样？"

    Args:
        episode_id: 单集 ID，可通过 get_subject_discussion 的 episodes 列表获得，
            或从 Bangumi 条目详情页获取。
        comments_limit: 吐槽箱评论的最大拉取条数，默认 30，最大 200。

    Returns:
        纯文本格式的单集信息和吐槽摘要。若无吐槽或 API 异常，返回对应的自然语言提示。
    """
    async with BangumiClient() as client:
        result = await client.get_episode_discussion(
            GetEpisodeDiscussionInput(
                episode_id=episode_id, comments_limit=comments_limit
            )
        )

    if "_error" in result:
        return f"系统提示：获取单集讨论失败。{result['_error']}"

    # ── 单集信息 ──────────────────────────────────────────────
    episode = result.get("episode", {})
    ep_name = episode.get("ep_name", "") or f"单集 {episode_id}"
    subject_name = episode.get("subject_name", "")
    airdate = episode.get("airdate", "")

    header = f"📺 {ep_name}"
    if subject_name:
        header += f"（{subject_name}）"
    if airdate:
        header += f" — {airdate} 播出"

    # ── 评论 ──────────────────────────────────────────────────
    comments: list[str] = result.get("comments", [])
    comment_count: int = result.get("comment_count", 0)
    comments_error = result.get("comments_error", "")

    lines: list[str] = [header]

    if comments_error:
        lines.append(f"\n⚠️ 吐槽箱获取失败：{comments_error}")
    elif not comments:
        lines.append(f"\n该单集目前还没有吐槽评论，来做第一个吐槽的人吧！")
    else:
        lines.append(f"\n💬 吐槽箱（共 {comment_count} 条）：\n")
        for i, text in enumerate(comments[:comments_limit], 1):
            lines.append(f"{i}. {text}")
        lines.append(f"\n── 以上为最近 {min(len(comments), comments_limit)} 条吐槽 ──")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
# 条目讨论全景
# ═══════════════════════════════════════════════════════════════════


@tool(args_schema=GetSubjectDiscussionInput)
async def get_subject_discussion(
    subject_id: int,
    data_types: list[str] = ["comments", "reviews"],
    limit: int = 10,
) -> str:
    """获取条目多维度社区讨论数据，全面了解一部作品的社区评价。

    四个维度的数据各有侧重——
    comments 反映口碑温度，reviews 提供深度观点，topics 展示讨论热点，
    episodes 帮助 LLM 定位关键集数。LLM 可按需选择拉取哪些维度的数据。

    典型场景：
    - "大家对《进击的巨人》总体评价怎么样？"
    - "看看这部番的长评都说了什么"
    - "最新一季有哪些讨论热点？"

    Args:
        subject_id: Bangumi 条目 ID，可通过 search_bangumi_subject 搜索番剧名称获得。
        data_types: 需要拉取的数据维度列表。``comments``=吐槽箱（短评+评分），
            ``reviews``=长篇评测（深度分析），``topics``=讨论帖（社区热点），
            ``episodes``=剧集列表（帮助定位单集）。默认 ``["comments", "reviews"]``。
        limit: 每个数据维度最多拉取的条数，默认 10。

    Returns:
        纯文本格式的多维度讨论数据摘要。
    """
    async with BangumiClient() as client:
        result = await client.get_subject_discussion(
            GetSubjectDiscussionInput(
                subject_id=subject_id, data_types=data_types, limit=limit
            )
        )

    if "_error" in result:
        return f"系统提示：获取条目讨论失败。{result['_error']}"

    lines: list[str] = [f"📊 条目 {subject_id} 的社区讨论全景：\n"]

    dimension_config: dict[str, tuple[str, str]] = {
        "comments": ("💬 吐槽箱", "comments_error"),
        "reviews": ("📝 长篇评测", "reviews_error"),
        "topics": ("🔥 讨论帖", "topics_error"),
        "episodes": ("📺 剧集列表", "episodes_error"),
    }

    for dt in data_types:
        if dt not in dimension_config:
            continue

        icon_label, error_key = dimension_config[dt]
        error_msg = result.get(error_key, "")
        data = result.get(dt, [])

        if error_msg:
            lines.append(f"{icon_label}：⚠️ 获取失败 — {error_msg}\n")
            continue

        if dt == "comments":
            # comments 返回 {"comments": [...], "rating_distribution": {...}, "comment_count": N}
            comments_data = data if isinstance(data, dict) else {}
            comment_list = comments_data.get("comments", [])
            comment_count = comments_data.get("comment_count", 0)
            rating_dist = comments_data.get("rating_distribution", {})

            if comment_list:
                lines.append(f"{icon_label}（共 {comment_count} 条）：")
                if rating_dist:
                    dist_parts = []
                    for k, v in rating_dist.items():
                        dist_parts.append(f"{k}分: {v}条")
                    lines.append(f"  评分分布：{' | '.join(dist_parts)}")
                for i, c in enumerate(comment_list[:limit], 1):
                    lines.append(f"  {i}. {c}")
                lines.append("")
            else:
                lines.append(f"{icon_label}：暂无评论\n")

        elif dt == "reviews":
            items = data.get("items", []) if isinstance(data, dict) else []
            total = data.get("total", len(items)) if isinstance(data, dict) else len(items)
            if items:
                lines.append(f"{icon_label}（共 {total} 篇）：")
                for i, r in enumerate(items[:limit], 1):
                    title = r.get("title", "无标题")
                    summary = r.get("summary", "")
                    user = r.get("user_name", "匿名")
                    summary_str = f" — {summary}" if summary else ""
                    lines.append(f"  {i}. {title}{summary_str}（作者: {user}）")
                lines.append("")
            else:
                lines.append(f"{icon_label}：暂无评测\n")

        elif dt == "topics":
            items = data.get("items", []) if isinstance(data, dict) else []
            total = data.get("total", len(items)) if isinstance(data, dict) else len(items)
            if items:
                lines.append(f"{icon_label}（共 {total} 条）：")
                for i, t in enumerate(items[:limit], 1):
                    title = t.get("title", "无标题")
                    replies = t.get("reply_count", 0)
                    creator = t.get("creator_name", "匿名")
                    lines.append(f"  {i}. {title} — {replies} 回复（作者: {creator}）")
                lines.append("")
            else:
                lines.append(f"{icon_label}：暂无讨论帖\n")

        elif dt == "episodes":
            items = data.get("items", []) if isinstance(data, dict) else []
            total = data.get("total", len(items)) if isinstance(data, dict) else len(items)
            if items:
                lines.append(f"{icon_label}（共 {total} 集）：")
                for i, ep in enumerate(items[:limit], 1):
                    sort = ep.get("sort", "?")
                    name = ep.get("name", "未命名")
                    name_cn = ep.get("name_cn", "")
                    display = f"{name}（{name_cn}）" if name_cn else name
                    airdate = ep.get("airdate", "")
                    airdate_str = f" — {airdate}" if airdate else ""
                    comments = ep.get("comment_count", 0)
                    comments_str = f" [{comments} 条吐槽]" if comments else ""
                    lines.append(f"  {i}. 第{sort}集 {display}{airdate_str}{comments_str}")
                lines.append("")
            else:
                lines.append(f"{icon_label}：暂无剧集信息\n")

    if len(lines) == 1:
        return f"条目 {subject_id} 暂无选择的讨论数据（{', '.join(data_types)}）。"

    lines.append(f"── 以上为条目 {subject_id} 的讨论数据摘要 ──")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
# 角色/人物评论
# ═══════════════════════════════════════════════════════════════════


@tool(args_schema=GetEntityCommentsInput)
async def get_entity_comments(
    entity_type: str,
    entity_id: int,
    limit: int = 20,
) -> str:
    """获取虚拟角色或现实人物的社区评论。

    角色和人物的评论接口结构完全一致，统一为一个 Tool，
    通过 entity_type 区分。LLM 可据此分析特定角色/人物在
    社区中的讨论热度和舆论倾向。

    典型场景：
    - "大家怎么评价阿尔托莉雅这个角色？"
    - "花泽香菜在社区的讨论热度怎么样？"
    - "看看大家对这位声优的评价"

    Args:
        entity_type: 实体类型。``character``=虚拟角色（如'阿尔托莉雅'），
            ``person``=现实人物（如'花泽香菜'、'新房昭之'）。
        entity_id: 角色或人物的 Bangumi ID，可通过
            search_bangumi_subject 以对应的 entity_type 搜索名称获得。
        limit: 拉取的评论最大条数，默认 20。

    Returns:
        纯文本格式的评论列表摘要。
    """
    async with BangumiClient() as client:
        result = await client.get_entity_comments(
            GetEntityCommentsInput(
                entity_type=entity_type, entity_id=entity_id, limit=limit
            )
        )

    if "_error" in result:
        return f"系统提示：获取{'角色' if entity_type == 'character' else '人物'}评论失败。{result['_error']}"

    entity_name = result.get("entity_name", "") or f"{'角色' if entity_type == 'character' else '人物'} {entity_id}"
    comments: list[str] = result.get("comments", [])
    comment_count: int = result.get("comment_count", 0)
    icon = "🧑" if entity_type == "character" else "🎤"

    if not comments:
        return f"{icon} {entity_name} 目前还没有社区评论。"

    lines: list[str] = [
        f"{icon} {entity_name} 的社区评论（共 {comment_count} 条）：\n"
    ]
    for i, text in enumerate(comments[:limit], 1):
        lines.append(f"{i}. {text}")

    lines.append(f"\n── 以上为最近 {min(len(comments), limit)} 条评论 ──")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
# 条目角色
# ═══════════════════════════════════════════════════════════════════


@tool(args_schema=GetSubjectCharactersInput)
async def get_subject_characters(subject_id: int) -> str:
    """获取一部作品的全部登场角色及其声优/演员信息。

    返回角色列表，包含角色名、出演类型（主角/配角/客串）、
    饰演者（声优/演员）名称。这是回答"主角是谁？""声优是谁？"
    的核心数据源。

    典型场景：
    - "《进击的巨人》有哪些主要角色？"
    - "鲁路修的声优是谁？"
    - "这部番的配音阵容怎么样？"
    - "列出这部作品的角色和对应的CV"

    Args:
        subject_id: Bangumi 条目 ID，可通过 search_bangumi_subject 搜索名称获得。

    Returns:
        纯文本格式的角色列表，每行包含角色名、角色类型和声优信息。
    """
    async with BangumiClient() as client:
        result = await client.get_subject_characters(subject_id=subject_id)

    if "_error" in result:
        return f"系统提示：获取条目角色失败。{result['_error']}"

    characters: list[dict] = result.get("characters", [])
    if not characters:
        return f"条目 {subject_id} 暂无角色信息。"

    lines: list[str] = [f"🎭 条目 {subject_id} 的角色列表（共 {len(characters)} 位）：\n"]

    for i, ch in enumerate(characters, 1):
        name = ch.get("name_cn") or ch.get("name", "未知角色")
        role_id = ch.get("role", 1)
        role_label = _ROLE_MAP.get(role_id, f"类型{role_id}")

        casts: list[dict] = ch.get("casts", [])
        if casts:
            cv_names: list[str] = []
            for cast in casts:
                person_name = cast.get("person_name_cn") or cast.get("person_name", "")
                relation = cast.get("relation", "")
                if person_name:
                    cv_names.append(f"{person_name}" + (f"（{relation}）" if relation and relation != "CV" else ""))
            cv_str = "、".join(cv_names) if cv_names else "暂无"
        else:
            cv_str = "暂无"

        lines.append(f"{i}. {name}（{role_label}）— {cv_str}")

    lines.append(f"\n── 以上为条目 {subject_id} 的全部角色 ──")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
# 用户画像（需要 Access Token）
# ═══════════════════════════════════════════════════════════════════


@tool(args_schema=GetUserProfileInput)
async def get_user_profile(
    username: str,
    collections_limit: int = 50,
    include_blogs: bool = True,
    include_characters: bool = False,
    include_persons: bool = False,
) -> str:
    """获取 Bangumi 用户的多维度画像数据。

    一次调用返回多维度数据：用户基本信息 + 条目收藏 +（可选）角色收藏 +
    人物收藏 + 日志列表。LLM 可据此分析用户的评分偏好、类型倾向、
    角色审美及内容产出风格。

    **认证要求**：需要系统配置有效的 Bangumi Access Token。
    如果 Token 未配置，将返回引导用户提供公开信息的提示。

    典型场景：
    - "分析一下用户 deepseek_jiang 的看番品味"
    - "这个用户喜欢什么类型的动画？"
    - "某用户的评分习惯是怎样的？"

    Args:
        username: Bangumi 用户名（个人主页 URL 中的用户名部分）。
        collections_limit: 收藏条目拉取的最大数量，默认 50。
        include_blogs: 是否拉取该用户的日志列表。需要 Access Token，默认 True。
        include_characters: 是否拉取该用户收藏的虚拟角色列表，默认 False。
        include_persons: 是否拉取该用户收藏的现实人物列表，默认 False。

    Returns:
        纯文本格式的用户画像摘要，或 Token 未配置时的引导提示。
    """
    token = get_settings().BANGUMI_ACCESS_TOKEN
    if not token:
        return (
            "系统提示：系统未配置 Bangumi Access Token，无法获取用户画像。\n"
            "您可以尝试以下替代方案：\n"
            f"1. 直接访问该用户的 Bangumi 主页查看公开信息：https://bgm.tv/user/{username}\n"
            "2. 使用搜索工具查找该用户公开评价过的条目。\n"
            "3. 如果您是该系统的管理员，请设置环境变量 BANGUMI_ACCESS_TOKEN 以启用此功能。"
        )

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
        return f"系统提示：获取用户画像失败。{result['_error']}"

    lines: list[str] = []

    # ── 用户基本信息 ──────────────────────────────────────────
    user = result.get("user", {})
    if user:
        nickname = user.get("nickname", username)
        sign = user.get("sign", "")
        lines.append(f"👤 用户画像：{nickname}（@{username}）")
        if sign:
            lines.append(f"   签名：{sign}")
        lines.append("")

    # ── 条目收藏 ──────────────────────────────────────────────
    collections = result.get("collections", {})
    if isinstance(collections, dict):
        stats = collections.get("collection_stats", {})
        coll_list = collections.get("collections", [])
        total = collections.get("total", 0)

        if stats or coll_list:
            lines.append(f"📂 条目收藏（共 {total} 条）：")

            # 统计摘要
            type_dist = stats.get("type_distribution", {})
            if type_dist:
                dist_str = " | ".join(f"{k}: {v}" for k, v in type_dist.items())
                lines.append(f"   收藏分布：{dist_str}")
            avg = stats.get("avg_score")
            if avg is not None:
                lines.append(f"   平均评分：{avg} / 最高：{stats.get('max_score', '-')} / 最低：{stats.get('min_score', '-')}")

            # 评分分布
            score_dist = stats.get("score_dist", {})
            if score_dist:
                sd_str = " | ".join(f"{k}分: {v}部" for k, v in score_dist.items())
                lines.append(f"   评分分布：{sd_str}")

            # 收藏列表摘要
            if coll_list:
                lines.append(f"\n   最近收藏：")
                for i, c in enumerate(coll_list[:10], 1):
                    name = c.get("name", "未知")
                    rate = c.get("rate", 0)
                    coll_type = c.get("collection_type", "")
                    rate_str = f" → {rate}分" if rate else ""
                    lines.append(f"   {i}. {name}（{coll_type}{rate_str}）")
            lines.append("")

    # ── 角色收藏 ──────────────────────────────────────────────
    if "characters" in result:
        chars = result["characters"]
        if chars:
            lines.append(f"🧑 角色收藏（共 {len(chars)} 位）：")
            for i, c in enumerate(chars[:10], 1):
                lines.append(f"   {i}. {c.get('name', '未知')}")
            lines.append("")

    # ── 人物收藏 ──────────────────────────────────────────────
    if "persons" in result:
        persons = result["persons"]
        if persons:
            lines.append(f"🎤 人物收藏（共 {len(persons)} 位）：")
            for i, p in enumerate(persons[:10], 1):
                career = p.get("career", "")
                career_str = f"（{career}）" if career else ""
                lines.append(f"   {i}. {p.get('name', '未知')}{career_str}")
            lines.append("")

    # ── 日志 ──────────────────────────────────────────────────
    if "blogs" in result:
        blogs = result["blogs"]
        if blogs:
            lines.append(f"📝 最近日志（共 {len(blogs)} 篇）：")
            for i, b in enumerate(blogs[:5], 1):
                title = b.get("title", "无标题")
                replies = b.get("replies", 0)
                lines.append(f"   {i}. {title} — {replies} 回复")
            lines.append("")

    # ── 错误信息 ──────────────────────────────────────────────
    for key in ["user_error", "collections_error", "characters_error", "persons_error", "blogs_error"]:
        if key in result:
            section = key.replace("_error", "")
            lines.append(f"⚠️ {section}数据获取失败：{result[key]}")

    if len(lines) == 0:
        return f"用户 {username} 暂无公开数据，或该用户设置了隐私保护。"

    lines.append(f"── 以上为用户 {username} 的画像摘要 ──")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
# 日志分析（需要 Access Token）
# ═══════════════════════════════════════════════════════════════════


@tool(args_schema=GetBlogInput)
async def get_blog(
    entry_id: int,
    include_comments: bool = True,
    include_subjects: bool = True,
) -> str:
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
        纯文本格式的日志分析视图，或 Token 未配置时的引导提示。
    """
    token = get_settings().BANGUMI_ACCESS_TOKEN
    if not token:
        return (
            "系统提示：系统未配置 Bangumi Access Token，无法获取日志内容。\n"
            "您可以尝试以下替代方案：\n"
            f"1. 直接访问日志页面查看：https://bgm.tv/blog/{entry_id}\n"
            "2. 如果您是该系统的管理员，请设置环境变量 BANGUMI_ACCESS_TOKEN 以启用此功能。"
        )

    async with BangumiClient(access_token=token) as client:
        result = await client.get_blog(
            GetBlogInput(
                entry_id=entry_id,
                include_comments=include_comments,
                include_subjects=include_subjects,
            )
        )

    if "_error" in result:
        return f"系统提示：获取日志失败。{result['_error']}"

    lines: list[str] = []

    # ── 日志正文 ──────────────────────────────────────────────
    blog = result.get("blog", {})
    if blog:
        title = blog.get("title", "无标题")
        author = blog.get("user_name", "匿名")
        tags = blog.get("tags", [])
        content = blog.get("content", "")
        replies = blog.get("replies", 0)
        created_at = blog.get("created_at", "")

        lines.append(f"📝 {title}")
        lines.append(f"   作者：{author}" + (f" | 发布时间：{created_at}" if created_at else ""))
        if tags:
            lines.append(f"   标签：{', '.join(tags)}")
        if content:
            lines.append(f"\n   {content}")
        lines.append(f"\n   共 {replies} 条回复")
        lines.append("")
    elif "blog_error" in result:
        lines.append(f"⚠️ 日志正文获取失败：{result['blog_error']}\n")

    # ── 评论 ──────────────────────────────────────────────────
    if include_comments and "comments" in result:
        comments = result["comments"]
        if comments:
            lines.append(f"💬 评论摘要（共 {len(comments)} 条）：")
            for i, c in enumerate(comments[:10], 1):
                lines.append(f"   {i}. {c}")
            lines.append("")
    elif "comments_error" in result:
        lines.append(f"⚠️ 评论获取失败：{result['comments_error']}\n")

    # ── 关联条目 ──────────────────────────────────────────────
    if include_subjects and "subjects" in result:
        subjects = result["subjects"]
        if subjects:
            lines.append(f"🔗 关联条目（共 {len(subjects)} 部）：")
            type_labels = {1: "📚", 2: "📺", 3: "🎵", 4: "🎮", 6: "🎬"}
            for i, s in enumerate(subjects[:10], 1):
                name = s.get("name", "未知")
                score = s.get("score", 0)
                icon = type_labels.get(s.get("type", 0), "📌")
                score_str = f" — 评分 {score:.1f}" if score else ""
                lines.append(f"   {i}. {icon} {name}{score_str}")
            lines.append("")
    elif "subjects_error" in result:
        lines.append(f"⚠️ 关联条目获取失败：{result['subjects_error']}\n")

    if len(lines) == 0:
        return f"日志 {entry_id} 暂无可用数据。"

    lines.append(f"── 以上为日志 {entry_id} 的分析视图 ──")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
# 用户时光机（需要 Access Token）
# ═══════════════════════════════════════════════════════════════════


@tool(args_schema=UserTimelineInput)
async def get_user_timeline(username: str, limit: int = 20) -> str:
    """获取指定用户的时光机动态（收藏、评分、吐槽等）。

    从用户时光机中提取收藏变更、评分、吐槽等动态，
    帮助 Agent 理解用户的追番偏好和鉴赏风格。

    **认证要求**：需要系统配置有效的 Bangumi Access Token。
    如果 Token 未配置，将返回引导用户提供公开信息的提示。

    典型场景：
    - "看看 deepseek_jiang 最近在追什么番"
    - "这个用户给哪些番打了高分？"
    - "分析一下某用户的看番品味"

    Args:
        username: Bangumi 用户名（即个人主页 URL 中的用户名部分）。
        limit: 返回动态条数上限，默认 20，最大 50。

    Returns:
        纯文本格式的用户动态摘要，或 Token 未配置时的引导提示。
    """
    token = get_settings().BANGUMI_ACCESS_TOKEN
    if not token:
        return (
            "系统提示：系统未配置 Bangumi Access Token，无法获取用户时光机。\n"
            "您可以尝试以下替代方案：\n"
            f"1. 直接访问该用户的 Bangumi 主页查看公开收藏：https://bgm.tv/user/{username}\n"
            "2. 使用搜索工具查找该用户公开评价过的条目。\n"
            "3. 如果您是该系统的管理员，请设置环境变量 BANGUMI_ACCESS_TOKEN 以启用此功能。"
        )

    async with BangumiClient(access_token=token) as client:
        result = await client.get_user_timeline(username=username, limit=limit)

    if "_error" in result:
        return f"系统提示：获取用户时光机失败。{result['_error']}"

    data: list[dict[str, Any]] = result.get("data", [])
    if not data:
        return f"用户 {username} 暂无公开动态，或该用户设置了隐私保护。"

    lines: list[str] = [
        f"🕐 用户 {username} 的时光机动态（最近 {min(len(data), limit)} 条）：\n"
    ]

    type_labels = {
        1: "💬 吐槽",
        2: "📂 收藏",
        6: "⭐ 评分",
        8: "📝 进度",
        9: "📝 进度",
    }

    for i, event in enumerate(data[:limit], 1):
        try:
            event_type = event.get("type", 0)
            label = type_labels.get(event_type, f"📌 动态(type={event_type})")

            subject = event.get("subject", {})
            subject_name = ""
            if isinstance(subject, dict):
                subject_name = subject.get("name", "") or subject.get("name_cn", "")

            rating = event.get("rating", None)
            rating_str = ""
            if isinstance(rating, (int, float)) and rating > 0:
                rating_str = f" → {rating} 分"
            elif isinstance(rating, dict):
                score_val = rating.get("score", 0)
                if score_val:
                    rating_str = f" → {score_val} 分"

            text = event.get("text", "") or event.get("content", "")
            text_str = ""
            if isinstance(text, str) and text.strip():
                short_text = text[:100] + "..." if len(text) > 100 else text
                text_str = f"：「{short_text}」"

            subject_str = f"《{subject_name}》" if subject_name else ""
            line = f"{i}. {label}{subject_str}{rating_str}{text_str}"
            lines.append(line)
        except Exception:
            lines.append(f"{i}. （该条动态解析失败，已跳过）")

    lines.append(f"\n── 以上为 {username} 的最近动态 ──")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
# 本地 RAG 语义检索
# ═══════════════════════════════════════════════════════════════════


@tool(args_schema=LocalSearchInput)
def search_local_bangumi(
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
      ``get_calendar``、``get_trending_topics``、``get_episode_comments``、
      ``get_subject_discussion``、``get_entity_comments``、
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
        get_trending_topics,
        get_episode_comments,
        get_subject_discussion,
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
