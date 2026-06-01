"""
AI Agent 工具函数层

将底层 BangumiClient 与 p1 API 包装为 LLM 可直接调用的异步工具函数。
每个函数附带详尽的 Google Style 中文 Docstring，帮助大模型
理解工具用途、参数含义及最佳调用时机。

架构约束：
  - 纯读操作：仅 GET 请求，绝无 PUT/POST/DELETE。
  - 认证透明化：access_token 绝不暴露给 LLM Schema。
  - 优雅降级：所有异常捕获后返回自然语言字符串。
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Any, Optional

import httpx
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from clients.bgm_client import BangumiClient

logger = logging.getLogger("bgm-agent.tools")

# ═══════════════════════════════════════════════════════════════════
# 全局配置
# ═══════════════════════════════════════════════════════════════════

P1_BASE_URL: str = "https://next.bgm.tv/p1"
"""p1 private API 基底 URL，用于单集吐槽、热门趋势、用户时光机等接口。"""

USER_AGENT: str = "BangumiAgent/1.0 (https://github.com/Ujikintoki/bangumi-agent)"
"""统一的 User-Agent 头，遵循 Bangumi 社区规范。"""


@lru_cache
def _get_access_token() -> Optional[str]:
    """从环境变量安全获取 Bangumi Access Token（带缓存）。

    Token 仅在此函数内读取一次，后续调用命中 LRU 缓存，
    避免重复读取环境变量。返回 None 表示未配置。

    Returns:
        Bangumi Bearer Token 字符串，或 None。
    """
    import os

    return os.getenv("BGM_ACCESS_TOKEN")


# ═══════════════════════════════════════════════════════════════════
# 原有的 v0 API 工具（保留不变）
# ═══════════════════════════════════════════════════════════════════


async def search_bangumi_subject(
    keyword: str,
    subject_type: int = 2,
    sort: str = "score",
) -> str:
    """搜索 Bangumi 条目，返回符合指定类型的精简条目列表（JSON 格式字符串）。

    当用户想要查找动画、书籍、音乐、游戏等条目时调用此工具。
    典型场景：
    - "帮我搜一下《进击的巨人》"
    - "推荐几部评分高的科幻动画"
    - "查一下 2024 年有什么新番"

    本工具会按照 `subject_type` 对搜索结果进行二次过滤，确保返回结果
    的类型与用户需求一致。返回的 JSON 中包含每个条目的 ID、名称、
    中文名、评分、排名、标签和简介，便于后续后续调用
    ``get_bangumi_subject_detail`` 获取完整详情。

    Args:
        keyword: 搜索关键词，支持日语、中文、英文等多种语言。例如
            "進撃の巨人"、"命运石之门"、"Steins;Gate"。
        subject_type: 条目类型 ID，默认为 ``2``（动画）。
            可用的类型：
            - ``1``: 书籍（漫画、小说、画集等）
            - ``2``: 动画（TV、OVA、剧场版等）
            - ``3``: 音乐（专辑、单曲等）
            - ``4``: 游戏（主机、PC、桌游等）
            - ``6``: 三次元（日剧、欧美剧、电影、综艺等）
        sort: 排序方式，默认为 ``"score"``（按评分从高到低）。
            可选值：
            - ``"match"``: 按匹配程度排序（MeiliSearch 默认）
            - ``"heat"``: 按收藏人数排序（热度）
            - ``"rank"``: 按排名由高到低排序
            - ``"score"``: 按评分从高到低排序

    Returns:
        JSON 格式字符串。成功时为一个包含 ``SlimSubjectResponse``
        对象的数组，每个对象包含 ``id``、``type``、``name``、
        ``name_cn``、``score``、``rank``、``tags`` 等字段；
        失败时返回 ``{"error": "错误描述", "status_code": ...}``。
    """
    async with BangumiClient() as client:
        result: list[Any] | dict[str, Any] = await client.search_subjects(
            keyword=keyword,
            sort=sort,
            limit=30,
        )

    # ── 错误分支：Client 返回了错误字典 ──
    if isinstance(result, dict):
        return json.dumps(result, ensure_ascii=False)

    # ── 按 subject_type 过滤，确保返回结果类型一致 ──
    filtered = [item for item in result if item.type == subject_type]

    # ── 序列化为 JSON 字符串 ──
    return json.dumps(
        [item.model_dump() for item in filtered],
        ensure_ascii=False,
    )


async def get_bangumi_subject_detail(subject_id: int) -> str:
    """获取 Bangumi 单个条目的完整详细信息（JSON 格式字符串）。

    当用户需要了解某个条目的完整信息时调用此工具，通常在
    ``search_bangumi_subject`` 之后使用。在用户已明确知道条目 ID
    （如从搜索结果中看到）时也可直接调用。

    典型场景：
    - "帮我看看编号 12345 这个番的详情"
    - "这部动画有多少集？什么时候播出的？"
    - "查一下这个条目的评分和收藏情况"

    返回的 JSON 中包含该条目的：
    - 基本信息和评分（name、name_cn、score、rank）
    - 播出/发售日期（date）
    - 平台信息（platform，如 TV / Web / Movie 等）
    - 章节总数（total_episodes，动画/剧集特有）
    - 集数/册数（eps / volumes）
    - 简介（short_summary）
    - 标签（tags）
    - 收藏统计（collection.wish / doing / collect）

    Args:
        subject_id: 条目 ID，即 Bangumi 条目详情页 URL 中的
            数字编号。例如 ``https://bgm.tv/subject/8`` 对应的
            ``subject_id`` 为 ``8``。

    Returns:
        JSON 格式字符串。成功时为一个包含完整条目信息的
        ``DetailedSubjectResponse`` 对象；失败时返回
        ``{"error": "错误描述", "status_code": ...}``。
    """
    async with BangumiClient() as client:
        result: Any | dict[str, Any] = await client.get_subject(
            subject_id=subject_id,
        )

    # ── 错误分支 ──
    if isinstance(result, dict) and "error" in result:
        return json.dumps(result, ensure_ascii=False)

    # ── 序列化为 JSON 字符串 ──
    return result.model_dump_json()


# ═══════════════════════════════════════════════════════════════════
# 新增 p1 API 工具（M4-Step2）
# ═══════════════════════════════════════════════════════════════════


# ── 辅助函数 ──────────────────────────────────────────────────────


def _build_headers(require_auth: bool = False) -> dict[str, str]:
    """构建 p1 API 请求头，可选注入 Bearer Token。

    Args:
        require_auth: 是否需要附带 Authorization 头。

    Returns:
        HTTP 请求头字典。
    """
    headers: dict[str, str] = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    if require_auth:
        token = _get_access_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
    return headers


def _safe_extract(data: Any, *keys: str, default: Any = "") -> Any:
    """安全地从嵌套字典中逐层提取值，任一层缺失时返回 default。"""
    for key in keys:
        if isinstance(data, dict):
            data = data.get(key, default)
        else:
            return default
    return data


async def _get_p1_json(
    path: str,
    params: dict[str, Any] | None = None,
    require_auth: bool = False,
) -> dict[str, Any]:
    """对 p1 API 发起 GET 请求并返回 JSON，所有异常均已内部捕获。

    Args:
        path: API 路径，如 "/episodes/123/comments"。
        params: URL 查询参数。
        require_auth: 是否需要认证。

    Returns:
        API 返回的 JSON 字典，或包含 "_error" 键的错误字典。
    """
    url = f"{P1_BASE_URL}{path}"
    headers = _build_headers(require_auth=require_auth)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as client:
            resp = await client.get(url, headers=headers, params=params)
            resp.raise_for_status()
            return resp.json()
    except httpx.TimeoutException:
        logger.warning("p1 API 超时: %s", url)
        return {"_error": f"请求超时：Bangumi 服务器响应过慢（{url}），请稍后重试。"}
    except httpx.HTTPStatusError as exc:
        logger.warning("p1 API HTTP 错误 %d: %s", exc.response.status_code, url)
        if exc.response.status_code == 401:
            return {
                "_error": "认证失败：Access Token 无效或已过期，请联系管理员更新凭证。"
            }
        if exc.response.status_code == 404:
            return {"_error": "未找到请求的资源，请检查 ID 是否正确。"}
        return {"_error": f"Bangumi API 返回错误 (HTTP {exc.response.status_code})。"}
    except httpx.HTTPError as exc:
        logger.warning("p1 API 网络异常: %s — %s", url, exc)
        return {"_error": f"网络连接异常，无法访问 Bangumi 服务器。原因：{exc}"}
    except Exception:
        logger.exception("p1 API 未知异常: %s", url)
        return {"_error": "系统内部异常，请稍后重试。"}


# ═══════════════════════════════════════════════════════════════════
# Pydantic Input Schema（LangChain Tool 专用）
# ═══════════════════════════════════════════════════════════════════


class EpisodeCommentsInput(BaseModel):
    """单集吐槽箱查询参数。"""

    episode_id: int = Field(..., description="单集 ID，可从 Bangumi 条目详情页获取。")
    limit: int = Field(
        default=20, ge=1, le=50, description="返回吐槽条数上限，默认 20。"
    )


class TrendingTopicsInput(BaseModel):
    """热门趋势查询参数。"""

    limit: int = Field(
        default=10, ge=1, le=20, description="返回热门条目数上限，默认 10。"
    )


class UserTimelineInput(BaseModel):
    """用户时光机查询参数。"""

    username: str = Field(..., description="Bangumi 用户名，如 'deepseek_jiang'。")
    limit: int = Field(
        default=20, ge=1, le=50, description="返回动态条数上限，默认 20。"
    )


class LocalSearchInput(BaseModel):
    """本地 RAG 语义检索参数。"""

    query: str = Field(..., description="自然语言查询，如 '80年代评分最高的机战番'。")
    entity_type: str = Field(
        default="all",
        description="实体类型过滤: subject / character / person / all，默认 all。",
    )
    limit: int = Field(default=5, ge=1, le=20, description="返回结果数上限，默认 5。")
    nsfw: bool = Field(
        default=False, description="是否包含 R18 内容，默认 False（安全护栏）。"
    )


# ═══════════════════════════════════════════════════════════════════
# p1 API 工具函数
# ═══════════════════════════════════════════════════════════════════


@tool(args_schema=EpisodeCommentsInput)
async def get_episode_comments(episode_id: int, limit: int = 20) -> str:
    """获取 Bangumi 单集吐槽箱的评论内容。

    从社区实时评论中提取用户昵称和吐槽正文，拼接为易读的纯文本摘要。
    当用户想了解"某一集大家怎么看"、"最新一集的社区反应"时调用此工具。

    典型场景：
    - "海贼王第 1088 集的吐槽箱里大家都说了什么？"
    - "帮我看看《芙莉莲》第 10 集观众的反应"
    - "这一集风评怎么样？"

    Args:
        episode_id: 单集 ID，可通过 Bangumi 条目详情页获取。
        limit: 返回吐槽条数上限，默认 20，最大 50。

    Returns:
        纯文本格式的吐槽摘要，包含每条吐槽的用户昵称和正文内容。
        若无吐槽或 API 异常，返回对应的自然语言提示。
    """
    # ── Step 1: 请求 p1 API ────────────────────────────────────
    result = await _get_p1_json(
        path=f"/episodes/{episode_id}/comments",
        params={"limit": limit},
        require_auth=False,
    )

    # ── Step 2: 错误分支 ───────────────────────────────────────
    if "_error" in result:
        return f"系统提示：获取单集吐槽失败。{result['_error']}"

    # ── Step 3: 提取 data 列表 ─────────────────────────────────
    data: list[dict[str, Any]] = result.get("data", [])
    if not data:
        return f"该单集（ID: {episode_id}）目前还没有吐槽评论，来做第一个吐槽的人吧！"

    # ── Step 4: 拼接为自然语言摘要 ─────────────────────────────
    lines: list[str] = [f"📺 单集 {episode_id} 的吐槽箱（共 {len(data)} 条）：\n"]
    for i, comment in enumerate(data[:limit], 1):
        try:
            nickname = _safe_extract(comment, "user", "nickname", default="匿名用户")
            text = _safe_extract(comment, "text", default="（无内容）")
            # 截断过长文本
            display_text = (
                text[:200] + "..."
                if isinstance(text, str) and len(text) > 200
                else text
            )
            lines.append(f"{i}. 【{nickname}】: {display_text}")
        except Exception:
            lines.append(f"{i}. （该条吐槽解析失败，已跳过）")

    lines.append(f"\n── 以上为最近 {min(len(data), limit)} 条吐槽 ──")
    return "\n".join(lines)


@tool(args_schema=TrendingTopicsInput)
async def get_trending_topics(limit: int = 10) -> str:
    """获取 Bangumi 全站热门条目风向标。

    从全站热门趋势中提取条目名称、评分、中文名等关键信息，
    帮助 Agent 感知社区当前讨论热度最高的作品。

    典型场景：
    - "最近什么番最火？"
    - "这季度大家都在追什么？"
    - "现在社区热度最高的动画有哪些？"

    Args:
        limit: 返回热门条目数上限，默认 10，最大 20。

    Returns:
        纯文本格式的热门条目列表，包含名称、评分、排名等。
    """
    result = await _get_p1_json(
        path="/trending/subjects",
        params={"limit": limit},
        require_auth=False,
    )

    if "_error" in result:
        return f"系统提示：获取热门趋势失败。{result['_error']}"

    data: list[dict[str, Any]] = result.get("data", [])
    if not data:
        return "当前没有热门条目数据，请稍后再试。"

    lines: list[str] = [f"🔥 Bangumi 全站热门风向标（TOP {min(len(data), limit)}）：\n"]
    for i, subject in enumerate(data[:limit], 1):
        try:
            name = subject.get("name", "未知作品")
            name_cn = subject.get("name_cn", "")
            display_name = f"{name}（{name_cn}）" if name_cn else name

            rating = subject.get("rating", {})
            score = rating.get("score", 0) if isinstance(rating, dict) else 0
            rank = rating.get("rank", 0) if isinstance(rating, dict) else 0

            score_str = f"评分 {score}" if score else "暂无评分"
            rank_str = f" | 排名 #{rank}" if rank else ""

            subject_type = subject.get("type", 0)
            type_map = {1: "📚", 2: "📺", 3: "🎵", 4: "🎮", 6: "🎬"}
            icon = type_map.get(subject_type, "📌")

            lines.append(f"{i}. {icon} {display_name} — {score_str}{rank_str}")
        except Exception:
            lines.append(f"{i}. （该条数据解析失败，已跳过）")

    return "\n".join(lines)


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
    # ── Auth 拦截：无 Token 时直接返回引导提示 ──────────────────
    token = _get_access_token()
    if not token:
        return (
            "系统提示：系统未配置 Bangumi Access Token，无法获取用户时光机。\n"
            "您可以尝试以下替代方案：\n"
            "1. 直接访问该用户的 Bangumi 主页查看公开收藏：https://bgm.tv/user/{username}\n"
            "2. 使用搜索工具查找该用户公开评价过的条目。\n"
            "3. 如果您是该系统的管理员，请设置环境变量 BGM_ACCESS_TOKEN 以启用此功能。"
        )

    result = await _get_p1_json(
        path=f"/users/{username}/timeline",
        params={"limit": limit},
        require_auth=True,
    )

    if "_error" in result:
        return f"系统提示：获取用户时光机失败。{result['_error']}"

    data: list[dict[str, Any]] = result.get("data", [])
    if not data:
        return f"用户 {username} 暂无公开动态，或该用户设置了隐私保护。"

    lines: list[str] = [
        f"🕐 用户 {username} 的时光机动态（最近 {min(len(data), limit)} 条）：\n"
    ]

    for i, event in enumerate(data[:limit], 1):
        try:
            event_type = event.get("type", 0)
            # 常见 type 映射: 1=吐槽, 2=收藏, 6=评分, 9=进度
            type_labels = {
                1: "💬 吐槽",
                2: "📂 收藏",
                6: "⭐ 评分",
                8: "📝 进度",
                9: "📝 进度",
            }
            label = type_labels.get(event_type, f"📌 动态(type={event_type})")

            # 尝试提取关联条目
            subject = event.get("subject", {})
            subject_name = ""
            if isinstance(subject, dict):
                subject_name = subject.get("name", "") or subject.get("name_cn", "")

            # 提取评分
            rating = event.get("rating", None)
            rating_str = ""
            if isinstance(rating, (int, float)) and rating > 0:
                rating_str = f" → {rating} 分"
            elif isinstance(rating, dict):
                score_val = rating.get("score", 0)
                if score_val:
                    rating_str = f" → {score_val} 分"

            # 提取吐槽/正文
            text = event.get("text", "") or event.get("content", "")
            text_str = ""
            if isinstance(text, str) and text.strip():
                short_text = text[:100] + "..." if len(text) > 100 else text
                text_str = f"：「{short_text}」"

            # 拼接
            subject_str = f"《{subject_name}》" if subject_name else ""
            line = f"{i}. {label}{subject_str}{rating_str}{text_str}"
            lines.append(line)
        except Exception:
            lines.append(f"{i}. （该条动态解析失败，已跳过）")

    lines.append(f"\n── 以上为 {username} 的最近动态 ──")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
# 本地 RAG 检索工具
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
        from core.config import get_settings
        from database.engine import engine
        from rag.retriever import RagEntityRetriever
    except ImportError as exc:
        logger.error("RAG 模块导入失败: %s", exc)
        return f"系统提示：本地搜索引擎模块加载失败。错误：{exc}"

    try:
        settings = get_settings()
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
                rating_total = meta.get("rating_total", 0)
                heat_str = f"评分 {score:.1f}" if score else ""
                if rating_total:
                    heat_str += f" | {rating_total}人评"
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
                career = meta.get("career", "")
                heat_str = f"收藏 {collects}" if collects else ""
                if career:
                    heat_str += f" | 职业: {career}"
                works = meta.get("works", [])
                if isinstance(works, list) and works:
                    top_works = [
                        w.get("subject_name", "")
                        for w in works[:3]
                        if w.get("subject_name")
                    ]
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
    - **无条件注册**：``search_local_bangumi``、``get_episode_comments``、
      ``get_trending_topics``（无需认证，始终可用）。
    - **条件注册**：``get_user_timeline`` 仅在 ``BGM_ACCESS_TOKEN``
      环境变量已配置时注册。

    使用方式::

        from tools.bgm_tools import get_agent_tools

        tools = get_agent_tools()
        # tools 现在可以直接传入 LangGraph Agent 的 ToolNode

    Returns:
        LangChain Tool 对象列表。
    """
    tools: list = [
        search_local_bangumi,
        get_episode_comments,
        get_trending_topics,
    ]

    token = _get_access_token()
    if token:
        tools.append(get_user_timeline)
        logger.info(
            "已启用全部 %d 个 Agent Tools（含需认证的用户时光机）",
            len(tools),
        )
    else:
        logger.info(
            "已启用 %d 个 Agent Tools（用户时光机因未配置 BGM_ACCESS_TOKEN 而禁用）",
            len(tools),
        )

    return tools
