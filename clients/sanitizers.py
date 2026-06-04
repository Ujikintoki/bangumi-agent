"""
数据清洗器（Data Sanitizer）

纯函数集合，职责：
  1. 白名单字段提取（仅保留工具文档约定的字段）
  2. 类型转换（魔术数字 → 自然语言）
  3. 文本硬截断（summary 500 字、评论 200 字）
  4. 噪音过滤（短评 < 4 字符、纯数字/日期）
  5. 聚合摘要（daily_summary / rating_distribution）

设计原则：
  - 纯函数：不依赖 self，不修改输入，不读写外部状态。
  - 白名单优先：显式声明"要什么"，而非"丢什么"。
  - 兜底值：任意字段缺失都返回默认值而非崩溃。
"""

from __future__ import annotations

import re
from typing import Optional

# ═══════════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════════

_SUBJECT_TYPES = {1: "书籍", 2: "动画", 3: "音乐", 4: "游戏", 6: "三次元"}
_CHARACTER_ROLES = {1: "主角", 2: "配角", 3: "客串"}
_COLLECTION_TYPES: dict[int, str] = {
    1: "想看",
    2: "看过",
    3: "在看",
    4: "搁置",
    5: "抛弃",
}


def _cn_name(name: str, name_cn: Optional[str]) -> str:
    """优先返回中文名，回退原名。"""
    return name_cn or name


def _truncate(text: str, max_len: int = 500) -> str:
    """硬截断，优先在句号处断开。"""
    if len(text) <= max_len:
        return text
    cut = text.rfind("。", 0, max_len)
    cut = cut if cut > max_len // 2 else max_len
    return text[:cut] + "..."


def _is_noise(text: str) -> bool:
    """判断是否为无价值短评。"""
    if len(text) < 4:
        return True
    if re.fullmatch(r"[\d\s\-\/:年月日\.]+", text):
        return True
    return False


# ═══════════════════════════════════════════════════════════════════
# Subject（条目）相关
# ═══════════════════════════════════════════════════════════════════


def sanitize_subject_search(raw: list[dict]) -> list[dict]:
    """搜索结果瘦身 → id/name/name_cn/type + score/rank（SlimSubject 自带）。

    保留 ``type_id`` 整数供 filtering，``type`` 为中文标签供 LLM 阅读。
    """
    results: list[dict] = []
    for item in raw:
        rating = item.get("rating", {}) or {}
        type_id = item.get("type", 0)
        results.append(
            {
                "id": item.get("id", 0),
                "name": item.get("name", ""),
                "name_cn": item.get("name_cn", ""),
                "type_id": type_id,
                "type": _SUBJECT_TYPES.get(type_id, "未知"),
                "score": rating.get("score", 0),
                "rank": rating.get("rank", 0),
            }
        )
    return results


def sanitize_subject_detail(raw: dict) -> dict:
    """条目详情瘦身 → 白名单字段提取 + meta_info 摘要。"""
    rating = raw.get("rating", {}) or {}
    images = raw.get("images", {}) or {}
    return {
        "id": raw.get("id", 0),
        "name": raw.get("name", ""),
        "name_cn": raw.get("name_cn", ""),
        "type": _SUBJECT_TYPES.get(raw.get("type", 0), "未知"),
        "summary": _truncate(raw.get("summary", "") or "", 500),
        "score": rating.get("score", 0),
        "rank": rating.get("rank", 0),
        "total_rating_count": rating.get("total", 0),
        "eps": raw.get("eps", 0),
        "tags": [
            {"name": t.get("name", ""), "count": t.get("count", 0)}
            for t in (raw.get("tags", []) or [])[:10]
        ],
        "image": (images.get("common") or images.get("medium") or ""),
    }


def sanitize_calendar(raw: list[dict]) -> dict:
    """日历数据瘦身 → 聚合 daily_summary + 按 watchers 降序的 items。

    注：weekday 过滤和 limit 截断由 bgm_client 调用方控制，
    此函数接收已过滤的单日列表，负责清洗与聚合。
    """
    if not raw:
        return {"daily_summary": "今日无番剧放送", "items": []}

    items: list[dict] = []
    for entry in raw:
        subject = entry.get("subject", {}) or {}
        rating = subject.get("rating", {}) or {}
        items.append(
            {
                "id": subject.get("id", 0),
                "name": _cn_name(subject.get("name", ""), subject.get("name_cn")),
                "name_cn": subject.get("name_cn", ""),
                "score": rating.get("score", 0),
                "watchers": entry.get("watchers", 0),
            }
        )

    # 按 watchers 降序
    items.sort(key=lambda x: x["watchers"], reverse=True)

    # 生成摘要
    top_names = [it["name"] for it in items[:3]]
    summary = f"今日热门：{'、'.join(top_names)}" if top_names else "今日有番剧放送"

    return {"daily_summary": summary, "items": items}


def sanitize_trending(raw: dict, subject_type: str) -> dict:
    """热门条目瘦身 → 聚合趋势摘要 + items 列表。

    raw 为 trending/subjects 返回体：{"data": [{"subject": {...}, "count": N}], "total": N}
    """
    data: list[dict] = raw.get("data", []) or []
    if not data:
        return {"summary": f"当前暂无{subject_type or '条目'}热门数据", "items": []}

    type_label = {
        "anime": "动画",
        "book": "书籍",
        "music": "音乐",
        "game": "游戏",
        "real": "三次元",
    }.get(subject_type, subject_type or "条目")

    items: list[dict] = []
    for entry in data:
        subject = entry.get("subject", {}) or {}
        rating = subject.get("rating", {}) or {}
        items.append(
            {
                "id": subject.get("id", 0),
                "name": _cn_name(subject.get("name", ""), subject.get("name_cn")),
                "name_cn": subject.get("name_cn", ""),
                "type": subject.get("type", 0),
                "score": rating.get("score", 0),
                "trending_score": entry.get("count", 0),
            }
        )

    top_names = [it["name"] for it in items[:3]]
    summary = f"当前{type_label}趋势 Top {len(items)}：{'、'.join(top_names)}"

    return {"summary": summary, "items": items, "total": raw.get("total", len(items))}


# ═══════════════════════════════════════════════════════════════════
# Comment（吐槽/评论）相关
# ═══════════════════════════════════════════════════════════════════


def sanitize_comments(raw: list[dict], limit: int) -> list[str]:
    """评论列表瘦身 → 压扁为纯文本列表 + 噪音过滤。

    通用评论清洗器，用于 episode / character / person 评论。
    每条格式: "({likes}赞) {content} 【回复: {replies}】"
    """
    if not raw:
        return []

    result: list[str] = []
    for c in raw:
        content = (c.get("comment") or c.get("content") or "").strip()
        if not content or _is_noise(content):
            continue

        # 计算 reactions 总 likes
        reactions = c.get("reactions", []) or []
        likes = sum(len(r.get("users", [])) for r in reactions)

        replies = c.get("replies", 0) or 0
        text = f"({likes}赞) {_truncate(content, 200)}"
        if replies:
            text += f" 【回复: {replies}条】"

        result.append(text)
        if len(result) >= limit:
            break

    return result


def sanitize_subject_comments(raw: list[dict], limit: int) -> dict:
    """条目评论瘦身 → 含评分分布聚合。

    每条评论格式: "[{rate}星] {content}"
    额外聚合 rating_distribution: {"1-3": N, "4-6": N, "7-8": N, "9-10": N}
    """
    if not raw:
        return {"comments": [], "rating_distribution": {}, "comment_count": 0}

    comments: list[str] = []
    rating_dist: dict[str, int] = {"1-3": 0, "4-6": 0, "7-8": 0, "9-10": 0}

    for c in raw:
        content = (c.get("comment") or c.get("content") or "").strip()
        if not content or _is_noise(content):
            continue

        rate = c.get("rate", 0) or 0
        if 1 <= rate <= 3:
            rating_dist["1-3"] += 1
        elif 4 <= rate <= 6:
            rating_dist["4-6"] += 1
        elif 7 <= rate <= 8:
            rating_dist["7-8"] += 1
        elif 9 <= rate <= 10:
            rating_dist["9-10"] += 1

        rate_label = f"{rate}星" if rate else "未评分"
        text = f"[{rate_label}] {_truncate(content, 200)}"
        comments.append(text)

        if len(comments) >= limit:
            break

    # 移除值为 0 的分段以节省 token
    rating_dist = {k: v for k, v in rating_dist.items() if v > 0}

    return {
        "comments": comments,
        "rating_distribution": rating_dist,
        "comment_count": len(comments),
    }


def sanitize_episode_detail(raw: dict) -> dict:
    """单集详情瘦身 → 仅保留集数/标题/截断 500 字的 desc + 所属条目摘要。"""
    subject = raw.get("subject", {}) or {}
    return {
        "id": raw.get("id", 0),
        "ep_sort": raw.get("sort", 0),
        "ep_name": _cn_name(raw.get("name", ""), raw.get("name_cn")),
        "name_cn": raw.get("name_cn", ""),
        "airdate": raw.get("airdate", ""),
        "duration": raw.get("duration", ""),
        "desc": _truncate(raw.get("description") or raw.get("desc") or "", 500),
        "subject_id": subject.get("id", 0),
        "subject_name": _cn_name(subject.get("name", ""), subject.get("name_cn")),
        "subject_name_cn": subject.get("name_cn", ""),
    }


# ═══════════════════════════════════════════════════════════════════
# Entity（角色/人物）相关
# ═══════════════════════════════════════════════════════════════════


def sanitize_entity_search(raw: list[dict], entity_type: str) -> list[dict]:
    """角色/人物搜索结果瘦身 → 仅保留 id/name/name_cn 等核心字段。"""
    results: list[dict] = []
    for item in raw:
        entry: dict = {
            "id": item.get("id", 0),
            "name": item.get("name", ""),
            "name_cn": item.get("name_cn", ""),
            "entity_type": entity_type,
        }
        # 角色额外字段
        if entity_type == "character":
            entry["role"] = _CHARACTER_ROLES.get(item.get("role", 0), "未知")
            entry["nsfw"] = item.get("nsfw", False)
        # 人物额外字段
        if entity_type == "person":
            career_raw = item.get("career", [])
            entry["career"] = ", ".join(career_raw) if isinstance(career_raw, list) else (career_raw or "")
        results.append(entry)
    return results


# ═══════════════════════════════════════════════════════════════════
# 组合清洗器（供 bgm_client 直接调用）
# ═══════════════════════════════════════════════════════════════════


def sanitize_search_subjects(raw: dict) -> dict:
    """Subject 搜索响应清洗 → 返回 {"results": [...], "total": N}。

    API 返回: {"results": [{id, name, name_cn, type, ...}], "total": N}
    """
    raw_results: list[dict] = raw.get("results", []) or raw.get("data", []) or []
    results = sanitize_subject_search(raw_results)
    return {
        "results": results,
        "total": raw.get("total", len(results)),
    }


def sanitize_search_characters(raw: list[dict]) -> list[dict]:
    """Character 搜索响应清洗 → 返回精简列表。"""
    return sanitize_entity_search(raw, "character")


def sanitize_search_persons(raw: list[dict]) -> list[dict]:
    """Person 搜索响应清洗 → 返回精简列表。"""
    return sanitize_entity_search(raw, "person")


def sanitize_episode_comments(raw: list[dict], limit: int) -> dict:
    """单集评论清洗 → 压扁 + 过滤噪音，返回 {comments: [...], comment_count: N}。

    与 sanitize_comments 的区别：返回 dict 而非纯 list，
    方便 bgm_client 合并 episode 信息。
    """
    comments = sanitize_comments(raw, limit)
    return {"comments": comments, "comment_count": len(comments)}


def sanitize_entity_comments(raw: list[dict], limit: int, entity_type: str) -> dict:
    """角色/人物评论清洗 → 压扁 + 包装 entity 元信息。

    返回: {"entity_type": "character"|"person", "entity_name": "...", "comments": [...], "comment_count": N}
    """
    if not raw:
        return {
            "entity_type": entity_type,
            "entity_name": "",
            "comments": [],
            "comment_count": 0,
        }

    # 从第一条评论中提取实体名称
    first = raw[0] if raw else {}
    subject_info = first.get("subject", {}) or {}
    entity_name = subject_info.get("name_cn") or subject_info.get("name") or ""

    comments = sanitize_comments(raw, limit)
    return {
        "entity_type": entity_type,
        "entity_name": entity_name,
        "comments": comments,
        "comment_count": len(comments),
    }


# ═══════════════════════════════════════════════════════════════════
# 辅助：用户/日志相关清洗
# ═══════════════════════════════════════════════════════════════════


def sanitize_user_collections(raw: list[dict], limit: int) -> dict:
    """用户收藏清洗 → 展平 subject 信息 + 聚合统计。"""
    if not raw:
        return {"collections": [], "collection_stats": {}, "total": 0}

    collections: list[dict] = []
    type_dist: dict[str, int] = {}
    scores: list[float] = []

    for entry in raw[:limit]:
        subject = entry.get("subject", {}) or {}
        rating = subject.get("rating", {}) or {}
        coll_type = _COLLECTION_TYPES.get(entry.get("type", 0), "未知")
        rate = entry.get("rate", 0) or 0

        type_dist[coll_type] = type_dist.get(coll_type, 0) + 1
        if rate > 0:
            scores.append(float(rate))

        collections.append(
            {
                "subject_id": subject.get("id", 0),
                "name": _cn_name(subject.get("name", ""), subject.get("name_cn")),
                "name_cn": subject.get("name_cn", ""),
                "type": _SUBJECT_TYPES.get(subject.get("type", 0), "未知"),
                "score": rating.get("score", 0),
                "rate": rate,
                "collection_type": coll_type,
            }
        )

    stats: dict = {"type_distribution": type_dist}
    if scores:
        stats["avg_score"] = round(sum(scores) / len(scores), 2)
        stats["max_score"] = max(scores)
        stats["min_score"] = min(scores)
        # 评分分布
        stats["score_dist"] = {
            "1-3": sum(1 for s in scores if 1 <= s <= 3),
            "4-6": sum(1 for s in scores if 4 <= s <= 6),
            "7-8": sum(1 for s in scores if 7 <= s <= 8),
            "9-10": sum(1 for s in scores if 9 <= s <= 10),
        }

    return {
        "collections": collections,
        "collection_stats": stats,
        "total": len(collections),
    }
