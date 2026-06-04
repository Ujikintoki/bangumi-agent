"""
Bangumi API 客户端（外观类）

职责：
  1. 继承 BaseClient 的 HTTP 基础设施
  2. 提供每个 Tool 对应的 API 方法
  3. 调用 sanitizers 清洗响应数据
  4. 返回 LLM 可直接消费的字典
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from clients import sanitizers
from clients.base import BaseClient
from schemas.tools_input import (GetBlogInput, GetCalendarInput,
                                 GetEntityCommentsInput,
                                 GetEpisodeDiscussionInput,
                                 GetSubjectDiscussionInput, GetTrendingInput,
                                 GetUserProfileInput, SearchBangumiInput)

logger = logging.getLogger("bgm-agent.client.bangumi")

_WEEKDAY_MAP: dict[str, int] = {
    "mon": 1,
    "tue": 2,
    "wed": 3,
    "thu": 4,
    "fri": 5,
    "sat": 6,
    "sun": 7,
}


class BangumiClient(BaseClient):
    """Bangumi p1 API 客户端。

    继承 BaseClient 的 HTTP 基础设施，提供面向 Tool 层的业务方法。
    每个方法遵循统一模板：构造请求 → 调用 _get/_post → 清洗响应 → 返回 dict。
    """

    # ═══════════════════════════════════════════════════════════
    # 搜索
    # ═══════════════════════════════════════════════════════════

    async def search(self, input: SearchBangumiInput) -> dict:
        """搜索条目/角色/人物 → id 映射。

        根据 entity_type 分发到不同搜索端点，返回精简后的结果列表。
        """
        entity = input.entity_type
        path = f"/p1/search/{entity}s"

        # 构建请求体
        body: dict = {"keyword": input.keyword, "limit": input.limit}
        if entity == "subject" and input.subject_type is not None:
            body.setdefault("filter", {})["type"] = [input.subject_type]

        raw = await self._post(path, json=body)
        if "_error" in raw:
            return raw

        # 分发清洗
        if entity == "subject":
            return sanitizers.sanitize_search_subjects(raw)
        elif entity == "character":
            data = raw if isinstance(raw, list) else (raw.get("results") or raw.get("data") or [])
            return {
                "results": sanitizers.sanitize_search_characters(data),
                "total": raw.get("total", len(data)) if isinstance(raw, dict) else len(data),
            }
        else:  # person
            data = raw if isinstance(raw, list) else (raw.get("results") or raw.get("data") or [])
            return {
                "results": sanitizers.sanitize_search_persons(data),
                "total": raw.get("total", len(data)) if isinstance(raw, dict) else len(data),
            }

    # ═══════════════════════════════════════════════════════════
    # 日历
    # ═══════════════════════════════════════════════════════════

    async def get_calendar(self, input: GetCalendarInput) -> dict:
        """获取每日放送排期。

        API 按星期几分组返回整周数据，内置 weekday 过滤逻辑。
        """
        raw = await self._get("/p1/calendar")
        if "_error" in raw:
            return raw

        # 确定目标星期几
        weekday = input.weekday
        if weekday == "today":
            target_day = datetime.now().isoweekday()  # 1=Mon, 7=Sun
        elif weekday == "all":
            target_day = None
        else:
            target_day = _WEEKDAY_MAP.get(weekday.lower(), 1)

        # 提取对应日期的放送数据
        if target_day is not None:
            # API 返回的 key 是数字 1-7 的字符串或整数
            day_data = (
                raw.get(str(target_day), [])
                or raw.get(target_day, [])
                or []
            )
        else:
            # "all" 模式：展平所有天的数据
            day_data = []
            for d in range(1, 8):
                day_data.extend(
                    raw.get(str(d), []) or raw.get(d, []) or []
                )

        # 截断 limit
        day_data = day_data[: input.limit_per_day]
        return sanitizers.sanitize_calendar(day_data)

    # ═══════════════════════════════════════════════════════════
    # 热门趋势
    # ═══════════════════════════════════════════════════════════

    # SubjectType → p1 API 整数
    _TRENDING_TYPE_MAP: dict[str, int] = {
        "anime": 2,
        "book": 1,
        "music": 3,
        "game": 4,
        "real": 6,
    }

    async def get_trending(self, input: GetTrendingInput) -> dict:
        """获取热门条目/话题趋势。"""
        category = input.category
        subject_type = input.subject_type or ""

        # 构建并行任务
        tasks: dict[str, asyncio.Task] = {}
        if category in ("subjects", "both"):
            type_id = self._TRENDING_TYPE_MAP.get(subject_type, 2) if subject_type else 2
            params: dict = {"limit": input.limit, "type": type_id}
            tasks["subjects"] = asyncio.create_task(
                self._get("/p1/trending/subjects", params=params)
            )
        if category in ("topics", "both"):
            tasks["topics"] = asyncio.create_task(
                self._get("/p1/trending/subjects/topics", params={"limit": input.limit})
            )

        # 等待所有请求
        results: dict = {}
        for key, task in tasks.items():
            try:
                raw = await task
            except Exception:
                raw = {"_error": f"获取热门{key}失败"}

            if "_error" in raw:
                results[key] = raw
            elif key == "subjects":
                results[key] = sanitizers.sanitize_trending(raw, subject_type)
            else:  # topics
                # topics 精简：id, title, replyCount, creator.nickname, subject 摘要
                data = raw.get("data", []) or []
                topics = []
                for t in data:
                    creator = t.get("creator", {}) or {}
                    subj = t.get("subject", {}) or {}
                    topics.append({
                        "id": t.get("id", 0),
                        "title": t.get("title", ""),
                        "reply_count": t.get("reply_count", 0) or t.get("replyCount", 0),
                        "creator_name": creator.get("nickname", ""),
                        "subject_name": subj.get("name_cn") or subj.get("name", ""),
                        "created_at": t.get("created_at", "") or t.get("createdAt", ""),
                    })
                results[key] = {"items": topics, "total": raw.get("total", len(topics))}

        return results

    # ═══════════════════════════════════════════════════════════
    # 单集讨论
    # ═══════════════════════════════════════════════════════════

    async def get_episode_discussion(self, input: GetEpisodeDiscussionInput) -> dict:
        """获取单集详情 + 社区吐槽。

        并发请求 episode 详情和评论，任一失败不影响另一部分。
        """
        ep_id = input.episode_id
        limit = input.comments_limit

        # 并发请求
        ep_task = asyncio.create_task(self._get(f"/p1/episodes/{ep_id}"))
        comments_task = asyncio.create_task(
            self._get(f"/p1/episodes/{ep_id}/comments?limit={limit}")
        )

        ep_raw = await ep_task
        comments_raw = await comments_task

        result: dict = {}

        # 处理 episode 详情
        if "_error" in ep_raw:
            result["_error"] = ep_raw["_error"]
            result["episode"] = {}
        else:
            result["episode"] = sanitizers.sanitize_episode_detail(ep_raw)

        # 处理评论
        if "_error" in comments_raw:
            result["comments"] = []
            result["comment_count"] = 0
            result["comments_error"] = comments_raw["_error"]
        else:
            # episode comments 返回纯数组
            comments_list = (
                comments_raw if isinstance(comments_raw, list)
                else comments_raw.get("data", comments_raw.get("results", []))
            )
            cleaned = sanitizers.sanitize_episode_comments(comments_list, limit)
            result.update(cleaned)

        return result

    # ═══════════════════════════════════════════════════════════
    # 条目讨论全景
    # ═══════════════════════════════════════════════════════════

    async def get_subject_discussion(self, input: GetSubjectDiscussionInput) -> dict:
        """获取条目多维度讨论数据。

        支持 comments / reviews / topics / episodes 四种维度，并发拉取。
        """
        sid = input.subject_id
        limit = input.limit
        data_types = input.data_types

        # 并发请求所有选定维度
        tasks: dict[str, asyncio.Task] = {}
        endpoint_map = {
            "comments": f"/p1/subjects/{sid}/comments?limit={limit}",
            "reviews": f"/p1/subjects/{sid}/reviews?limit={limit}",
            "topics": f"/p1/subjects/{sid}/topics?limit={limit}",
            "episodes": f"/p1/subjects/{sid}/episodes?limit={limit}",
        }
        for dt in data_types:
            if dt in endpoint_map:
                tasks[dt] = asyncio.create_task(self._get(endpoint_map[dt]))

        # 收集结果并清洗
        result: dict = {"subject_id": sid}
        for dt, task in tasks.items():
            try:
                raw = await task
            except Exception:
                raw = {"_error": f"获取{dt}失败"}

            if "_error" in raw:
                result[f"{dt}_error"] = raw["_error"]
                result[dt] = [] if dt != "comments" else {}
            elif dt == "comments":
                comments_list = (
                    raw if isinstance(raw, list)
                    else raw.get("data", raw.get("results", []))
                )
                result["comments"] = sanitizers.sanitize_subject_comments(
                    comments_list, limit
                )
            elif dt == "reviews":
                data = raw.get("data", []) or []
                reviews = []
                for r in data:
                    entry = r.get("entry", {}) or {}
                    user = r.get("user", {}) or {}
                    reviews.append({
                        "id": r.get("id", 0),
                        "title": entry.get("title", ""),
                        "summary": sanitizers._truncate(
                            entry.get("summary", "") or "", 200
                        ),
                        "created_at": entry.get("created_at", "") or entry.get("createdAt", ""),
                        "user_name": user.get("nickname", ""),
                    })
                result["reviews"] = {"items": reviews, "total": raw.get("total", len(reviews))}
            elif dt == "topics":
                data = raw.get("data", []) or []
                topics = []
                for t in data:
                    creator = t.get("creator", {}) or {}
                    topics.append({
                        "id": t.get("id", 0),
                        "title": t.get("title", ""),
                        "reply_count": t.get("reply_count", 0) or t.get("replyCount", 0),
                        "creator_name": creator.get("nickname", ""),
                        "created_at": t.get("created_at", "") or t.get("createdAt", ""),
                    })
                result["topics"] = {"items": topics, "total": raw.get("total", len(topics))}
            elif dt == "episodes":
                data = raw.get("data", []) or []
                eps = []
                for e in data:
                    if e.get("type", 0) != 0:
                        continue  # 只保留主线剧集 (type=0)
                    eps.append({
                        "id": e.get("id", 0),
                        "sort": e.get("sort", 0),
                        "name": sanitizers._cn_name(
                            e.get("name", ""), e.get("name_cn")
                        ),
                        "name_cn": e.get("name_cn", ""),
                        "airdate": e.get("airdate", ""),
                        "comment_count": e.get("comment", 0),
                    })
                result["episodes"] = {"items": eps, "total": raw.get("total", len(eps))}

        return result

    # ═══════════════════════════════════════════════════════════
    # 角色/人物评论
    # ═══════════════════════════════════════════════════════════

    async def get_entity_comments(self, input: GetEntityCommentsInput) -> dict:
        """获取角色或人物的社区评论。

        角色和人物共享同一接口结构，通过 entity_type 区分。
        """
        path = f"/p1/{input.entity_type}s/{input.entity_id}/comments"
        raw = await self._get(f"{path}?limit={input.limit}")
        if "_error" in raw:
            return raw

        # API 返回纯数组
        comments_list = (
            raw if isinstance(raw, list)
            else raw.get("data", raw.get("results", []))
        )
        return sanitizers.sanitize_entity_comments(
            comments_list, input.limit, input.entity_type
        )

    # ═══════════════════════════════════════════════════════════
    # 用户画像
    # ═══════════════════════════════════════════════════════════

    async def get_user_profile(self, input: GetUserProfileInput) -> dict:
        """获取用户多维度画像。

        并发拉取：基本信息 + 收藏统计 +（可选）角色/人物/日志。
        """
        username = input.username
        limit = input.collections_limit

        # 必选请求
        tasks: dict[str, asyncio.Task] = {
            "user": asyncio.create_task(self._get(f"/p1/users/{username}")),
            "collections": asyncio.create_task(
                self._get(f"/p1/users/{username}/collections/subjects", params={"limit": limit})
            ),
        }

        if input.include_characters:
            tasks["characters"] = asyncio.create_task(
                self._get(f"/p1/users/{username}/collections/characters", params={"limit": limit})
            )
        if input.include_persons:
            tasks["persons"] = asyncio.create_task(
                self._get(f"/p1/users/{username}/collections/persons", params={"limit": limit})
            )
        if input.include_blogs:
            tasks["blogs"] = asyncio.create_task(
                self._get(f"/p1/users/{username}/blogs", params={"limit": 20})
            )

        # 等待所有请求
        result: dict = {"username": username}

        for key, task in tasks.items():
            try:
                raw = await task
            except Exception:
                raw = {"_error": f"获取{key}失败"}

            if "_error" in raw:
                result[f"{key}_error"] = raw["_error"]
                continue

            if key == "user":
                result["user"] = {
                    "id": raw.get("id", 0),
                    "nickname": raw.get("nickname", ""),
                    "username": raw.get("username", ""),
                    "sign": (raw.get("sign", "") or "")[:200],
                    "avatar": raw.get("avatar", {}).get("large", "") if raw.get("avatar") else "",
                }
            elif key == "collections":
                data = raw if isinstance(raw, list) else (raw.get("data") or [])
                result["collections"] = sanitizers.sanitize_user_collections(data, limit)
            elif key == "characters":
                data = raw if isinstance(raw, list) else (raw.get("data") or [])
                result["characters"] = [
                    {
                        "id": c.get("subject_id") or c.get("id", 0),
                        "name": sanitizers._cn_name(
                            (c.get("subject") or {}).get("name", ""),
                            (c.get("subject") or {}).get("name_cn"),
                        ),
                    }
                    for c in data[:30]
                ]
            elif key == "persons":
                data = raw if isinstance(raw, list) else (raw.get("data") or [])
                persons: list[dict] = []
                for p in data[:30]:
                    subject = p.get("subject") or {}
                    career_raw = subject.get("career", [])
                    career_str = ", ".join(career_raw) if isinstance(career_raw, list) else str(career_raw or "")
                    persons.append({
                        "id": p.get("subject_id") or p.get("id", 0),
                        "name": sanitizers._cn_name(
                            subject.get("name", ""), subject.get("name_cn"),
                        ),
                        "career": career_str,
                    })
                result["persons"] = persons
            elif key == "blogs":
                data = raw if isinstance(raw, list) else (raw.get("data") or [])
                result["blogs"] = [
                    {
                        "id": b.get("id", 0),
                        "title": b.get("title", ""),
                        "summary": sanitizers._truncate(b.get("summary") or "", 150),
                        "created_at": b.get("created_at", "") or b.get("createdAt", ""),
                        "replies": b.get("replies", 0),
                    }
                    for b in data[:20]
                ]

        return result

    # ═══════════════════════════════════════════════════════════
    # 日志
    # ═══════════════════════════════════════════════════════════

    async def get_blog(self, input: GetBlogInput) -> dict:
        """获取日志正文 + 评论 + 关联条目。

        并发请求三个端点，组装为完整分析视图。
        """
        eid = input.entry_id

        tasks: dict[str, asyncio.Task] = {
            "blog": asyncio.create_task(self._get(f"/p1/blogs/{eid}")),
        }
        if input.include_comments:
            tasks["comments"] = asyncio.create_task(
                self._get(f"/p1/blogs/{eid}/comments", params={"limit": 30})
            )
        if input.include_subjects:
            tasks["subjects"] = asyncio.create_task(
                self._get(f"/p1/blogs/{eid}/subjects")
            )

        result: dict = {"entry_id": eid}

        for key, task in tasks.items():
            try:
                raw = await task
            except Exception:
                raw = {"_error": f"获取{key}失败"}

            if "_error" in raw:
                result[f"{key}_error"] = raw["_error"]
                continue

            if key == "blog":
                user = raw.get("user", {}) or {}
                result["blog"] = {
                    "id": raw.get("id", 0),
                    "title": raw.get("title", ""),
                    "content": sanitizers._truncate(raw.get("content") or raw.get("text") or "", 300),
                    "tags": raw.get("tags", []),
                    "created_at": raw.get("created_at", "") or raw.get("createdAt", ""),
                    "replies": raw.get("replies", 0),
                    "user_name": user.get("nickname", ""),
                }
            elif key == "comments":
                data = raw if isinstance(raw, list) else (raw.get("data") or [])
                result["comments"] = sanitizers.sanitize_comments(data, 30)
            elif key == "subjects":
                data = raw if isinstance(raw, list) else (raw.get("data") or [])
                result["subjects"] = [
                    {
                        "id": s.get("id", 0),
                        "name": sanitizers._cn_name(s.get("name", ""), s.get("name_cn")),
                        "type": s.get("type", 0),
                        "score": (s.get("rating") or {}).get("score", 0),
                    }
                    for s in data
                ]

        return result

    # ═══════════════════════════════════════════════════════════
    # 条目详情
    # ═══════════════════════════════════════════════════════════

    async def get_subject_detail(self, subject_id: int) -> dict:
        """获取单个条目完整详情（p1 API）。

        GET /p1/subjects/{subject_id}

        Args:
            subject_id: Bangumi 条目 ID。

        Returns:
            清洗后的条目详情字典，或 ``{"_error": ...}``。
        """
        raw = await self._get(f"/p1/subjects/{subject_id}")
        if "_error" in raw:
            return raw
        return sanitizers.sanitize_subject_detail(raw)

    # ═══════════════════════════════════════════════════════════
    # 用户时光机
    # ═══════════════════════════════════════════════════════════

    async def get_user_timeline(self, username: str, limit: int = 20) -> dict:
        """获取用户时光机动态（p1 API）。

        GET /p1/users/{username}/timeline

        Args:
            username: Bangumi 用户名。
            limit: 返回动态条数上限。

        Returns:
            包含 ``data`` 列表的字典，或 ``{"_error": ...}``。
        """
        raw = await self._get(
            f"/p1/users/{username}/timeline", params={"limit": limit}
        )
        return raw

    # ═══════════════════════════════════════════════════════════
    # 条目角色
    # ═══════════════════════════════════════════════════════════

    # CharacterCastType 枚举 → 人类可读标签
    _CAST_RELATION_MAP: dict[int, str] = {
        0: "CV",
        1: "Dub",
        2: "Actor",
        3: "中配",
        4: "日配",
        5: "英配",
        6: "韩配",
    }

    async def get_subject_characters(self, subject_id: int) -> dict:
        """获取条目角色列表（p1 API）。

        GET /p1/subjects/{subject_id}/characters

        Args:
            subject_id: Bangumi 条目 ID。

        Returns:
            清洗后的角色列表字典 ``{"subject_id": ..., "characters": [...]}``，
            或 ``{"_error": ...}``。
        """
        raw = await self._get(f"/p1/subjects/{subject_id}/characters")
        if "_error" in raw:
            return raw

        # API 返回纯数组
        data = raw if isinstance(raw, list) else raw.get("data", raw.get("results", []))
        if not data:
            return {"subject_id": subject_id, "characters": []}

        characters: list[dict] = []
        for item in data:
            character = item.get("character") or {}
            casts_raw = item.get("casts") or []
            casts: list[dict] = []
            for cast in casts_raw:
                person = cast.get("person") or {}
                relation_id = cast.get("relation", 0)
                casts.append({
                    "person_id": person.get("id", 0),
                    "person_name": person.get("name", ""),
                    "person_name_cn": person.get("nameCN", ""),
                    "relation": self._CAST_RELATION_MAP.get(relation_id, f"类型{relation_id}"),
                })

            characters.append({
                "character_id": character.get("id", 0),
                "name": character.get("name", ""),
                "name_cn": character.get("nameCN", ""),
                "role": character.get("role", 1),
                "casts": casts,
            })

        return {"subject_id": subject_id, "characters": characters}
