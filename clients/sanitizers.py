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
from datetime import datetime
from typing import Optional

# ═══════════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════════

_SUBJECT_TYPES = {1: "书籍", 2: "动画", 3: "音乐", 4: "游戏", 6: "三次元"}
_CHARACTER_ROLES = {1: "角色", 2: "机体", 3: "舰船", 4: "组织机构"}
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
    if len(text) < 2:
        return True
    if re.fullmatch(r"[\d\s\-\/:年月日\.]+", text):
        return True
    if len(set(text)) == 1 and len(text) > 1:
        # "hhhhh"、"。。。。" 等纯重复字符
        return True
    return False


def _strip_bbcode(text: str) -> str:
    """去除 BBCode 标签，保留语义标签的中文标注。

    Bangumi 的日志、评论、讨论帖等用户生成内容大量使用 BBCode 格式。
    此函数：
      - 去掉纯视觉标签（[b][i][u][s][size][color][font][align]）但保留内容
      - 将语义标签转为 LLM 可读的中文标注（[mask]→【剧透】, [quote]→【引用】）
      - 展平 [url] 为 \"文字(链接)\" 格式
      - 删除 [img] 标签（LLM 无法识别图片）
    """
    if not text:
        return text

    # 去除开标签: [b][size=20][color=red][font=X][align=center][left]等
    text = re.sub(
        r'\[(?:[bius]|size=[^\]]*|color=[^\]]*|font=[^\]]*|align=[^\]]*|left|center|right)\]',
        '', text,
    )
    # 去除闭标签: [/b][/size][/color][/font][/align][/left]等
    text = re.sub(
        r'\[/(?:[bius]|size|color|font|align|left|center|right)\]',
        '', text,
    )

    # [mask] / [spoiler] → 【剧透】...【/剧透】
    text = re.sub(
        r'\[mask\](.*?)\[/mask\]', r'【剧透】\1【/剧透】', text,
        flags=re.DOTALL,
    )
    text = re.sub(
        r'\[spoiler\](.*?)\[/spoiler\]', r'【剧透】\1【/剧透】', text,
        flags=re.DOTALL,
    )

    # [quote=作者]内容[/quote] → 【引用 作者】内容【/引用】
    text = re.sub(
        r'\[quote(?:=([^\]]*))?\](.*?)\[/quote\]',
        lambda m: f'【引用 {m.group(1)}】{m.group(2)}【/引用】' if m.group(1) else f'【引用】{m.group(2)}【/引用】',
        text, flags=re.DOTALL,
    )

    # [url=https://...]文字[/url] → 文字(https://...)
    text = re.sub(
        r'\[url=([^\]]+)\](.*?)\[/url\]', r'\2(\1)', text,
    )

    # [img]...[/img] → 删除（LLM 看不懂图片）
    text = re.sub(r'\[img[^\]]*\].*?\[/img\]', '', text, flags=re.DOTALL)

    # 清理多余的空行
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


# ═══════════════════════════════════════════════════════════════════
# Subject（条目）相关
# ═══════════════════════════════════════════════════════════════════


def sanitize_subject_search(raw: list[dict]) -> list[dict]:
    """Subject 搜索结果 → L1 摘要级（~25 tokens/条）。

    意图：用户自然语言查询 → 实体 ID 映射。LLM 用于确认存在、
    消歧同名不同媒介（漫画 vs 动画 vs 游戏）、按评分/排名排序推荐。

    字段决策（A/B/C/D 方法论，2026-07-24）：
      A. 保留 — id（工具链）、name/name_cn（识别）、type→中文（筛选）、
         score/rank（排序）、info（消歧核心——同一关键词的漫画/动画/OAD/游戏全靠它）
      D. 丢弃 — rating.count[10]（~30t，评分分布是 L2 数据，详情工具负责）、
         rating.total（评分人数是 L2）、images（~88t/条，53% 体积，LLM 零价值）、
         metaTags（与 type+info 重复且数据质量不稳定）、locked（管理字段）、
         nsfw（subject 无此概念）
    """
    results: list[dict] = []
    for item in raw:
        rating = item.get("rating", {}) or {}
        type_id = item.get("type", 0)
        results.append(
            {
                "id": item.get("id", 0),
                "name": item.get("name", ""),
                "name_cn": item.get("nameCN", ""),
                "type": _SUBJECT_TYPES.get(type_id, "未知"),
                "score": rating.get("score", 0),
                "rank": rating.get("rank", 0),
                "info": item.get("info", ""),
            }
        )
    return results


# Infobox 黑名单：与主字段重复 或 事实查询类数据（用户可自己查 Bangumi 页面）
_INFOBOX_DROP_KEYS = {
    # 重复主字段
    "中文名", "话数", "放送开始", "别名", "平台",
    # 播放/电视台（事实查询）
    "放送星期", "播放电视台", "其他电视台", "播放结束", "在线播放平台",
    # URL
    "官方网站", "website",
    # 价格/日期/ISBN（事实查询）
    "售价", "价格", "发行日期", "其他发行日期", "游玩人数",
    "ISBN", "页数", "册数",
    # 法律/其他
    "Copyright",
}


def _clean_infobox(raw: list[dict], drop_keys: set[str] | None = None) -> dict[str, str]:
    """清洗 infobox：[{key, values:[{v}]}] → {key: value}，黑名单过滤噪音。

    保留策略：黑名单模式——只删明确噪音，其余全保留。
    删 4 类：
      1. 空值
      2. key 在黑名单中
      3. value 含 URL
      4. value 含价格符号/ISBN/Copyright
    """
    if drop_keys is None:
        drop_keys = _INFOBOX_DROP_KEYS

    result: dict[str, str] = {}
    for item in raw:
        key = item.get("key", "")
        vals = [v.get("v", "").strip() for v in (item.get("values") or [])]
        vals = [v for v in vals if v]
        if not vals:
            continue
        combined = " / ".join(vals)

        if key in drop_keys:
            continue
        if "http" in combined:
            continue
        if any(p in combined for p in ("¥", "円", "ISBN", "©", "Copyright")):
            continue

        result[key] = combined
    return result


def sanitize_subject_detail(raw: dict) -> dict:
    """条目详情 → L2 详情级（~525 tokens），1 次 API 调用。

    意图：输入 subject ID → 输出该条目完整画像。LLM 用它回答评分口碑、
    制作人员、类型标签、故事简介、收藏热度等核心问题，无需再调其他工具
    （除非用户追问角色名单/评论区等 L3 列表数据）。

    字段决策（A/B/C/D 方法论，2026-07-24）：
      A. 保留 — id/name/name_cn/type/info/eps/volumes/series/series_entry/nsfw（核心标识）、
         tags 全部 30 条（同质数据 ~240t，LLM 批量处理）、
         rating_count[10]（LLM 自行分析口碑集中度，替代硬编码 _compute_subject_signals）
      B. 扁平化 — rating.{score,rank,total}→顶层、collection key 数字→中文、
         airtime.date→date 字符串
      C. 压缩 — summary 截断 300 字、infobox 黑名单过滤（去空值/重复/URL/事实查询）
      D. 丢弃 — images 全部 5 尺寸（LLM 零价值）、platform（wiki 元数据）、
         metaTags（与 type+tags 重复）、locked/redirect（管理字段）
    """
    rating = raw.get("rating", {}) or {}
    collection_raw = raw.get("collection", {}) or {}
    airtime = raw.get("airtime", {}) or {}

    return {
        # ── 核心标识 ──
        "id": raw.get("id", 0),
        "name": raw.get("name", ""),
        "name_cn": raw.get("nameCN", ""),
        "type": _SUBJECT_TYPES.get(raw.get("type", 0), "未知"),
        "info": raw.get("info", ""),
        "date": airtime.get("date", ""),
        "eps": raw.get("eps", 0),
        "volumes": raw.get("volumes", 0),
        "series": raw.get("series", False),
        "series_entry": raw.get("seriesEntry", False),
        "nsfw": raw.get("nsfw", False),
        # ── 文本 ──
        "summary": _truncate(raw.get("summary", "") or "", 300),
        "score": rating.get("score", 0),
        "rank": rating.get("rank", 0),
        "rating_total": rating.get("total", 0),
        "rating_count": rating.get("count", []),
        # ── 收藏（key 数字→中文）──
        "collection": {
            _COLLECTION_TYPES.get(int(k), k): v
            for k, v in collection_raw.items()
        } if collection_raw else {},
        # ── 标签（全量 30 条）──
        "tags": [
            {"name": t.get("name", ""), "count": t.get("count", 0)}
            for t in (raw.get("tags", []) or [])
        ],
        # ── Infobox（黑名单过滤 + 扁平化）──
        "infobox": _clean_infobox(raw.get("infobox", []) or []),
    }


def sanitize_calendar(raw: list[dict]) -> dict:
    """日历数据瘦身 → L1 摘要级（~15t/条目）。

    意图：输入单日 raw 列表 → 按关注数降序排列的条目摘要。
    这是"今天/周X有什么番可以看"的发现工具。LLM 拿到 id 后
    调 get_bangumi_subject_detail 获取详情。

    字段决策（A/B/C/D 方法论，2026-07-24）：
      A. 保留 — subject.id（工具链）、subject.name CN 优先（识别）、
         rating.score（排序信号）、watchers（热度信号）
      B. 扁平化 — rating.score→顶层 score
      D. 丢弃 — name_cn（与 name 重复——name 已 CN 优先）、
         subject.type（日历仅动画=2）、subject.info（~80 chars，L2 数据）、
         metaTags/images/locked/nsfw/rating.rank/rating.count/rating.total（L2 或噪音）
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
                "name": _cn_name(subject.get("name", ""), subject.get("nameCN")),
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
    """热门条目瘦身 → L1 摘要级（~20t/条目）。

    意图：全站热门条目发现。LLM 拿到 id 后调 get_bangumi_subject_detail。

    字段决策（A/B/C/D 方法论，2026-07-24）：
      A. 保留 — subject.id（工具链）、subject.name CN 优先（识别）、
         count→trending_score（热度排序信号）
      B. 扁平化 — rating.score→顶层 score、subject.type int→中文
      D. 丢弃 — name_cn（与 name 重复）、subject.info/metaTags/images/
         rating.rank/rating.count/rating.total/locked/nsfw（L2 或噪音）
    """
    data: list[dict] = raw.get("data", []) or []
    if not data:
        return {"summary": f"当前暂无{subject_type or '条目'}热门数据", "items": [], "total": 0}

    type_label = {
        "anime": "动画", "book": "书籍", "music": "音乐",
        "game": "游戏", "real": "三次元",
    }.get(subject_type, subject_type or "条目")

    items: list[dict] = []
    for entry in data:
        subject = entry.get("subject", {}) or {}
        rating = subject.get("rating", {}) or {}
        items.append(
            {
                "id": subject.get("id", 0),
                "name": _cn_name(subject.get("name", ""), subject.get("nameCN")),
                "type": _SUBJECT_TYPES.get(subject.get("type", 0), "未知"),
                "score": rating.get("score", 0),
                "trending_score": entry.get("count", 0),
            }
        )

    top_names = [it["name"] for it in items[:3]]
    summary = f"当前{type_label}趋势 Top {len(items)}：{'、'.join(top_names)}"

    return {"summary": summary, "items": items, "total": raw.get("total", len(items))}


def sanitize_trending_topics(raw: dict) -> dict:
    """热门讨论帖瘦身 → L1 摘要级（~25t/帖）。

    意图：全站热门讨论发现。"社区在热议什么？"——
    仅保留帖子标题和指向条目的引用，引导 LLM 调
    get_bangumi_subject_detail 了解相关作品。

    字段决策（A/B/C/D 方法论，2026-07-24）：
      A. 保留 — title（内容）、replyCount→reply_count（热度信号）、
         subject.id→subject_id（工具链）、subject.name CN 优先→subject_name（上下文）
      D. 丢弃 — id（无下游工具消费 group post）、
         creatorID/creator.*（无下游工具消费用户详情，且需 token）、
         parentID/createdAt/updatedAt/state/display（元数据）、
         subject.* 全部（完整条目对象含 images/rating）、
         replies（回复树）
    """
    data: list[dict] = raw.get("data", []) or []
    if not data:
        return {"items": [], "total": 0}

    items: list[dict] = []
    for t in data:
        subj = t.get("subject", {}) or {}
        items.append({
            "title": t.get("title", ""),
            "reply_count": t.get("reply_count", 0) or t.get("replyCount", 0),
            "subject_name": _cn_name(subj.get("name", ""), subj.get("nameCN")),
            "subject_id": subj.get("id", 0),
        })

    return {"items": items, "total": raw.get("total", len(items))}


# ═══════════════════════════════════════════════════════════════════
# Comment（吐槽/评论）相关
# ═══════════════════════════════════════════════════════════════════


def sanitize_comments(raw: list[dict], limit: int) -> list[str]:
    """评论列表瘦身 → 压扁纯文本 + 噪音过滤 + 去重 + 时间序。

    通用评论清洗器，用于 episode / character / person 评论。
    Bangumi 的 engagement 信号（emoji 回应/回复数）几乎为零，
    放弃伪排序，改为：去噪→去重→时间倒序（最新在后）。
    格式: "{content}"，仅在有回复时追加 "【N条回复】"。

    噪音过滤规则：
      - 空字符串或纯 BBCode
      - 纯数字/日期格式
      - 全重复字符 ("hhhh"、"1111")
      - 前后已出现的完全重复内容（去重保留首次）
    """
    if not raw:
        return []

    seen: set[str] = set()
    cleaned: list[str] = []

    for c in raw:
        content = (c.get("comment") or c.get("content") or "").strip()
        content = _strip_bbcode(content).strip()
        if not content or _is_noise(content):
            continue

        # 去重：完全相同的内容只保留首次
        key = content.lower()
        if key in seen:
            continue
        seen.add(key)

        text = _truncate(content, 200)
        replies = c.get("replies", 0)
        if isinstance(replies, list):
            reply_count = len(replies)
        elif isinstance(replies, (int, float)):
            reply_count = int(replies)
        else:
            reply_count = 0
        if reply_count > 0:
            text += f" 【{reply_count}条回复】"

        cleaned.append(text)

    # 时间倒序：API 返回最新在前，反转为自然阅读顺序（旧→新）
    cleaned.reverse()

    return cleaned[:limit]


def sanitize_subject_comments(raw: list[dict], limit: int) -> dict:
    """条目评论瘦身 → 评分分布聚合 + 去重 + 时间序。

    与通用评论不同：条目评论附带用户评分（1-10），这是真实信号。
    保留评分标注 + 评分分布聚合。排序改为时间倒序（最新在后），
    因为 Bangumi 的 emoji 回应信号（avg=0.0）无区分度。

    格式: "[N星] {content}"
    额外聚合 rating_distribution: {"1-3": N, "4-6": N, "7-8": N, "9-10": N}
    """
    if not raw:
        return {"comments": [], "rating_distribution": {}, "comment_count": 0}

    real_total = len(raw)
    rating_dist: dict[str, int] = {"1-3": 0, "4-6": 0, "7-8": 0, "9-10": 0}
    seen: set[str] = set()
    cleaned: list[str] = []

    for c in raw:
        content = (c.get("comment") or c.get("content") or "").strip()
        content = _strip_bbcode(content).strip()
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

        # 去重
        key = content.lower()
        if key in seen:
            continue
        seen.add(key)

        rate_label = f"{rate}星" if rate else "未评分"
        text = f"[{rate_label}] {_truncate(content, 200)}"
        cleaned.append(text)

    # 时间倒序：API 返回最新在前，反转为自然阅读顺序
    cleaned.reverse()

    rating_dist = {k: v for k, v in rating_dist.items() if v > 0}

    return {
        "comments": cleaned[:limit],
        "rating_distribution": rating_dist,
        "comment_count": real_total,
    }


def sanitize_episode_detail(raw: dict) -> dict:
    """单集详情瘦身 → L2 详情级（~30-200t，取决于 desc 长度）。

    意图：输入 episode ID → 单集元数据 + 所属条目引用。
    与评论数据合并返回，LLM 获得单集完整视图。

    字段决策（A/B/C/D 方法论，2026-07-24）：
      A. 保留 — id（工具链）、sort→sort（集数）、name CN 优先（标题）、
         subject.id→subject_id（工具链）、subject.name CN 优先→subject_name（所属条目）
      C. 压缩 — airdate/duration（元数据，各 ~10 chars）、
         desc 截断 500 字、comment→comment_count（评论数信号）
      D. 丢弃 — name_cn/subject_name_cn（与 name 重复）、
         type/disc/subjectID（冗余或无用）、
         subject.* 全部（完整条目对象含 images/rating/metaTags）
    """
    subject = raw.get("subject", {}) or {}
    return {
        "id": raw.get("id", 0),
        "sort": raw.get("sort", 0),
        "name": _cn_name(raw.get("name", ""), raw.get("nameCN")),
        "airdate": raw.get("airdate", ""),
        "duration": raw.get("duration", ""),
        "desc": _truncate(raw.get("description") or raw.get("desc") or "", 500),
        "comment_count": raw.get("comment", 0),
        "subject_id": subject.get("id", 0),
        "subject_name": _cn_name(subject.get("name", ""), subject.get("nameCN")),
    }


# ═══════════════════════════════════════════════════════════════════
# Entity（角色/人物）相关
# ═══════════════════════════════════════════════════════════════════


def sanitize_entity_search(raw: list[dict], entity_type: str) -> list[dict]:
    """角色/人物搜索结果 → L1 摘要级（~15 tokens/条）。

    意图：用户搜索角色/人物名称 → 拿到 ID，消歧同名不同人，
    后续调 get_character_detail / get_person_detail / get_entity_comments。

    字段决策（A/B/C/D 方法论，2026-07-24）：
      A. 保留 — id（工具链）、name/name_cn（识别）、role→中文（角色）/ type+career（人物）、
         info（消歧——性别/生日等）、nsfw（过滤）
      D. 丢弃 — images（~50t/条，LLM 零价值）、comment（评论数是 L3 数据）、
         lock（管理字段）
    """
    results: list[dict] = []
    for item in raw:
        entry: dict = {
            "id": item.get("id", 0),
            "name": item.get("name", ""),
            "name_cn": item.get("nameCN", ""),
            "info": item.get("info", ""),
        }
        if entity_type == "character":
            entry["role"] = _CHARACTER_ROLES.get(item.get("role", 0), "未知")
            entry["nsfw"] = item.get("nsfw", False)
        if entity_type == "person":
            entry["type"] = {1: "个人", 2: "公司", 3: "组合"}.get(item.get("type", 0), "未知")
            career_raw = item.get("career", [])
            entry["career"] = career_raw if isinstance(career_raw, list) else ([career_raw] if career_raw else [])
            entry["nsfw"] = item.get("nsfw", False)
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


def sanitize_reviews(raw: dict) -> dict:
    """条目长评瘦身 → L2 详情级（~80t/篇）。

    意图：条目维度的深度评测。每条 review 是 blog entry，
    可通过 get_blog 获取完整正文+评论+关联条目。

    字段决策（A/B/C/D 方法论，2026-07-24）：
      A. 保留 — id（工具链→get_blog）、entry.title（标题）、
         entry.summary（摘要）、user.nickname→user_name（归属）、
         entry.replies→reply_count（回应信号）
      C. 压缩 — summary 截断 200 字、created_at 保留（时间上下文）
      D. 丢弃 — user.avatar/sign/group/joinedAt（用户画像膨胀）、
         entry.icon/uid/public/updatedAt/type（元数据）
    """
    data: list[dict] = raw.get("data", []) or []
    if not data:
        return {"items": [], "total": 0}

    items: list[dict] = []
    for r in data:
        entry = r.get("entry", {}) or {}
        user = r.get("user", {}) or {}
        items.append({
            "id": r.get("id", 0),
            "title": entry.get("title", ""),
            "summary": _truncate(entry.get("summary", "") or "", 200),
            "user_name": user.get("nickname", ""),
            "reply_count": entry.get("replies", 0) or 0,
            "created_at": entry.get("created_at", "") or entry.get("createdAt", ""),
        })

    return {"items": items, "total": raw.get("total", len(items))}


def sanitize_discussion_topics(raw: dict) -> dict:
    """条目讨论帖瘦身 → L1 摘要级（~30t/帖）。

    意图：围绕该条目的社区讨论帖。与 trending topics 不同——
    这些帖子限定于特定条目，无需重复 subject 引用。

    字段决策（A/B/C/D 方法论，2026-07-24）：
      A. 保留 — title（内容）、replyCount→reply_count（热度信号）、
         creator.nickname→creator_name（归属）
      C. 压缩 — id 保留（虽无下游工具，但未来可能加 get_group_topic）
      D. 丢弃 — creatorID/parentID/createdAt/updatedAt/state/display（元数据）、
         creator.avatar/sign/group（用户画像）
    """
    data: list[dict] = raw.get("data", []) or []
    if not data:
        return {"items": [], "total": 0}

    items: list[dict] = []
    for t in data:
        creator = t.get("creator", {}) or {}
        items.append({
            "id": t.get("id", 0),
            "title": t.get("title", ""),
            "reply_count": t.get("reply_count", 0) or t.get("replyCount", 0),
            "creator_name": creator.get("nickname", ""),
        })

    return {"items": items, "total": raw.get("total", len(items))}


def sanitize_subject_episodes(raw: dict) -> dict:
    """条目剧集列表瘦身 → L3 列表级（~25-80t/集，取决于 desc 长度）。

    意图：条目→单集 ID 映射。LLM 按 sort（集数）排序后定位目标集，
    调 get_episode_comments 获取单集详情+吐槽。

    字段决策（A/B/C/D 方法论，2026-07-24）：
      A. 保留 — id（工具链→get_episode_comments）、sort（集数）、
         name CN 优先（标题）、comment→comment_count（是否有吐槽）
      C. 压缩 — airdate（播出日期 ~10 chars）、desc 截断 200 字（定位用）
      D. 丢弃 — name_cn（与 name 重复）、type/disc/subjectID/duration（冗余）、
         subject 嵌套对象（与 subject detail 重复）
    """
    data: list[dict] = raw.get("data", []) or []
    if not data:
        return {"items": [], "total": 0}

    items: list[dict] = []
    for e in data:
        if e.get("type", 0) != 0:
            continue  # 只保留主线剧集
        items.append({
            "id": e.get("id", 0),
            "sort": e.get("sort", 0),
            "name": _cn_name(e.get("name", ""), e.get("nameCN")),
            "airdate": e.get("airdate", ""),
            "desc": _truncate(e.get("desc", "") or e.get("description", "") or "", 200),
            "comment_count": e.get("comment", 0),
        })

    # 按集数升序（API 可能乱序）
    items.sort(key=lambda x: x["sort"])

    return {"items": items, "total": raw.get("total", len(items))}


def sanitize_entity_comments(
    raw: list[dict],
    limit: int,
    entity_type: str,
    entity_id: int,
    entity_detail: dict | None = None,
) -> dict:
    """角色/人物评论清洗 → 压扁 + 实体信息包装。

    entity_detail 来自 /p1/{entity_type}s/{entity_id} 并发请求，
    提供 nameCN/name 用于精确归属。失败时传入 None，entity_name 为空字符串。

    返回: {
        "entity_type": "character"|"person",
        "entity_id": int,
        "entity_name": "...",
        "comments": [...],
        "comment_count": N,
    }
    """
    # 从 entity_detail 提取实体名（CN 优先，fallback JP）
    entity_name = ""
    if entity_detail and "_error" not in entity_detail:
        entity_name = _cn_name(
            entity_detail.get("name", ""),
            entity_detail.get("nameCN", ""),
        )

    comments = sanitize_comments(raw, limit)
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "entity_name": entity_name,
        "comments": comments,
        "comment_count": len(comments),
    }


# ═══════════════════════════════════════════════════════════════════
# 条目角色列表清洗
# ═══════════════════════════════════════════════════════════════════

_CAST_RELATIONS: dict[int, str] = {
    0: "CV",
    1: "Dub",
    2: "Actor",
    3: "中配",
    4: "日配",
    5: "英配",
    6: "韩配",
}

_CHARACTER_TYPES: dict[int, str] = {
    1: "主角",
    2: "配角",
    3: "客串",
}


def sanitize_subject_characters(raw: list[dict], subject_id: int) -> dict:
    """条目角色列表 → 极致索引（~15-60t/角色，取决于声优数量）。

    意图：这是二分图的边集——subject→character（char_type 边）和
    character→person（relation 边）。仅保留边的端点和类型，
    节点属性（info/summary/comment/images）由 get_character_detail /
    get_person_detail 负责。

    LLM 使用模式：
      - "鲁路修的声优是谁？" → 找到角色，看 casts 字符串
      - "有哪些主角？" → 按 char_type 过滤
      - "配音阵容怎么样？" → 扫描全部 casts
      - 拿 character_id 调 get_character_detail 看详情
      - 拿声优名字调 search_bangumi_subject(entity_type="person") 找回 person_id

    字段决策（A/B/C/D 方法论，2026-07-24）：
      A. 保留 — character.id→character_id（工具链端点）、
         character.nameCN||name→name（最少识别信息）
      B. 扁平化 — item.type int→char_type（边的类型：主角/配角/客串）、
         relation int→中文标签
      C. 压缩 — casts 压为字符串 "name(relation), name(relation)"，CV 省略标签
      D. 丢弃 — role/info/comment/order（属于 character detail）、
         person_id/person_name_cn（LLM 可用名字搜索找回）、
         character.images/lock、person.* 全部节点属性、
         casts[].summary（始终为空）
    """
    if not raw:
        return {"subject_id": subject_id, "characters": []}

    characters: list[dict] = []
    for item in raw:
        ch = item.get("character") or {}
        casts_raw = item.get("casts") or []

        cast_strs: list[str] = []
        for cast in casts_raw:
            person = cast.get("person") or {}
            pname = person.get("nameCN") or person.get("name", "")
            if not pname:
                continue
            rel = _CAST_RELATIONS.get(cast.get("relation", 0), "")
            if rel and rel != "CV":
                cast_strs.append(f"{pname}({rel})")
            else:
                cast_strs.append(pname)

        characters.append({
            "character_id": ch.get("id", 0),
            "name": ch.get("nameCN") or ch.get("name", ""),
            "char_type": _CHARACTER_TYPES.get(item.get("type", 1), "未知"),
            "casts": ", ".join(cast_strs),
        })

    return {"subject_id": subject_id, "characters": characters}


# ═══════════════════════════════════════════════════════════════════
# 角色 / 人物详情清洗
# ═══════════════════════════════════════════════════════════════════


# 角色 infobox 黑名单：重复主字段 + 空值常态 + URL
_CHARACTER_INFOBOX_DROP_KEYS = {
    "简体中文名", "别名",             # dup nameCN/name
    "性别", "身高", "体重", "BWH",    # dup info
    "生日", "血型",                    # mostly empty, trivia
    "引用来源",                        # URL
}


def sanitize_character_detail(raw: dict) -> dict:
    """角色详情 → L2 详情级（~100-170 tokens）。

    意图：输入角色 ID → 角色完整画像。LLM 用它回答角色背景故事、
    性格特点、人气热度、NSFW 状态等问题。

    字段决策（A/B/C/D 方法论，2026-07-24）：
      A. 保留 — id/name/name_cn/role→中文/info（核心标识）、
         comment/collects（人气指标）、nsfw（过滤）
      C. 压缩 — summary 截断 200 字、infobox 去重+滤空（角色 infobox
         key 高度一致：简体中文名/别名/性别/身高/体重/BWH 均与主字段重复，
         URL/生日/血型 为空值或事实查询）
      D. 丢弃 — images（LLM 零价值，~74t）、lock/redirect（管理字段）
    """
    return {
        "id": raw.get("id", 0),
        "name": raw.get("name", ""),
        "name_cn": raw.get("nameCN", ""),
        "role": _CHARACTER_ROLES.get(raw.get("role", 0), "未知"),
        "info": raw.get("info", ""),
        "summary": _truncate(raw.get("summary", "") or "", 200),
        "infobox": _clean_infobox(raw.get("infobox", []) or [], drop_keys=_CHARACTER_INFOBOX_DROP_KEYS),
        "comment": raw.get("comment", 0),
        "collects": raw.get("collects", 0),
        "nsfw": raw.get("nsfw", False),
    }


def sanitize_person_detail(raw: dict) -> dict:
    """人物详情 → L2 详情级（~100-200 tokens）。

    意图：输入人物 ID → 人物完整画像。LLM 用它回答声优/导演/作者
    的职业背景、代表作、个人信息、人气热度等问题。

    字段决策（A/B/C/D 方法论，2026-07-24）：
      A. 保留 — id/name/name_cn/type→中文/career→list/info（核心标识）、
         comment/collects（人气指标）、nsfw（过滤）
      C. 压缩 — summary 截断 200 字、infobox 去重+滤空（与角色同模板：
         简体中文名/别名/性别/生日/血型/身高/体重/BWH/引用来源 均与主字段重复，
         官网/Twitter/Weibo 等含 URL 自动过滤，性格/趣味/学历/星座等保留）
      D. 丢弃 — images（~80t）、lock/redirect
    """
    career_raw = raw.get("career", [])
    return {
        "id": raw.get("id", 0),
        "name": raw.get("name", ""),
        "name_cn": raw.get("nameCN", ""),
        "type": {1: "个人", 2: "公司", 3: "组合"}.get(raw.get("type", 0), "未知"),
        "career": career_raw if isinstance(career_raw, list) else ([career_raw] if career_raw else []),
        "info": raw.get("info", ""),
        "summary": _truncate(raw.get("summary", "") or "", 200),
        "infobox": _clean_infobox(raw.get("infobox", []) or [], drop_keys=_CHARACTER_INFOBOX_DROP_KEYS),
        "comment": raw.get("comment", 0),
        "collects": raw.get("collects", 0),
        "nsfw": raw.get("nsfw", False),
    }


# ═══════════════════════════════════════════════════════════════════
# 辅助：用户/日志相关清洗
# ═══════════════════════════════════════════════════════════════════


def sanitize_user_collections(raw: list[dict], limit: int, api_total: int = 0) -> dict:
    """用户收藏清洗 → 展平 subject 信息 + 评分统计 + 个人短评/标签。

    展示列表截断至 15 条以节省 token。评分统计基于采样数据，
    全量收藏总数和各状态分布由 user endpoint 的 stats 提供。

    Args:
        raw: 收藏条目原始列表
        limit: 采样条数上限
        api_total: 收藏 API 返回的真实 total（全量条目数），0=从 raw 推断
    """
    _DISPLAY_CAP = 15

    if not raw:
        return {"collections": [], "collection_stats": {}, "total": 0}

    collections: list[dict] = []
    scores: list[float] = []

    for entry in raw[:limit]:
        subject = entry.get("subject", {}) or {}
        rating = subject.get("rating", {}) or {}
        interest = entry.get("interest", {}) or {}
        coll_type = _COLLECTION_TYPES.get(interest.get("type", 0), "未知")
        rate = interest.get("rate", 0) or 0

        if rate > 0:
            scores.append(float(rate))

        # 时间戳转日期
        updated_ts = interest.get("updatedAt", 0)
        updated_at = (
            datetime.fromtimestamp(updated_ts).strftime("%Y-%m-%d")
            if updated_ts else ""
        )

        # 用户短评（有则截断 100 字，无则省略 key -> LLM 省 token）
        comment_raw = (interest.get("comment") or "").strip()
        comment = _truncate(comment_raw, 100) if comment_raw else ""

        # 发行信息（紧凑一行："2024-12-23 / 亜月ねね / 講談社"）
        info = (subject.get("info") or "").strip()

        # 类型标签（["日本", "漫画", "原创"] — 有则保留）
        meta_tags = subject.get("metaTags") or []

        item: dict = {
            "subject_id": subject.get("id", 0),
            "name": _cn_name(subject.get("name", ""), subject.get("name_cn")),
            "type": _SUBJECT_TYPES.get(subject.get("type", 0), "未知"),
            "score": rating.get("score", 0),
            "rate": rate,
            "collection_type": coll_type,
            "updated_at": updated_at,
        }
        if comment:
            item["comment"] = comment
        if info:
            item["info"] = info
        if meta_tags:
            item["meta_tags"] = meta_tags

        collections.append(item)

    stats: dict = {}
    if scores:
        stats["avg_score"] = round(sum(scores) / len(scores), 2)
        stats["max_score"] = max(scores)
        stats["min_score"] = min(scores)
        stats["score_dist"] = {
            "1-3": sum(1 for s in scores if 1 <= s <= 3),
            "4-6": sum(1 for s in scores if 4 <= s <= 6),
            "7-8": sum(1 for s in scores if 7 <= s <= 8),
            "9-10": sum(1 for s in scores if 9 <= s <= 10),
        }

    return {
        "collections": collections[:_DISPLAY_CAP],
        "collection_stats": stats,
        "total": api_total if api_total > 0 else len(collections),
    }


def sanitize_user_stats(raw_stats: dict) -> dict:
    """将 user endpoint 的整数 code stats 转译为人类可读标签。

    API 原始格式::

        {"subject": {"2": {"3": 5, "2": 20}}, "mono": {"character": 10}}

    输出::

        {"anime": {"看过": 20, "在看": 5}, "角色": 10}

    只提取有非零值的维度，空类型/状态不输出以节省 token。
    """
    _TYPE_LABELS: dict[int, str] = {1: "书籍", 2: "动画", 3: "音乐", 4: "游戏", 6: "三次元"}
    _STATUS_LABELS: dict[int, str] = {1: "想看", 2: "看过", 3: "在看", 4: "搁置", 5: "抛弃"}

    if not raw_stats:
        return {}

    result: dict = {}

    # subject stats: {type_id: {status_id: count}}
    subject_stats = raw_stats.get("subject", {})
    if subject_stats:
        by_type: dict[str, dict[str, int]] = {}
        for type_key, statuses in subject_stats.items():
            type_label = _TYPE_LABELS.get(int(type_key), f"类型{type_key}")
            by_status: dict[str, int] = {}
            for status_key, count in statuses.items():
                if count > 0:
                    by_status[_STATUS_LABELS.get(int(status_key), f"状态{status_key}")] = count
            if by_status:
                by_type[type_label] = by_status
        if by_type:
            result["by_type"] = by_type

    # mono stats: {character: N, person: N}
    mono_stats = raw_stats.get("mono", {})
    if mono_stats:
        char_count = mono_stats.get("character", 0)
        person_count = mono_stats.get("person", 0)
        if char_count:
            result["角色"] = char_count
        if person_count:
            result["人物"] = person_count

    # blog count
    blog_count = raw_stats.get("blog", 0)
    if blog_count:
        result["日志"] = blog_count

    return result


# ═══════════════════════════════════════════════════════════════════
# 用户时光机
# ═══════════════════════════════════════════════════════════════════


def sanitize_timeline_events(raw: list[dict], limit: int) -> dict:
    """从嵌套 memo 结构中展平提取时光机事件。

    API 原始结构把事件数据埋在 ``event.memo.*`` 深层嵌套中，
    且不同事件类型的数据位置不同。此函数按类型分发提取，
    丢弃无分析价值的每日签到事件（type=2）。

    Args:
        raw: API 返回的原始事件列表
        limit: 事件条数上限

    Returns:
        ``{"events": [...], "total": N}``
    """
    _EVENT_LABELS: dict[int, str] = {
        0: "进度",
        1: "日志",
        9: "收藏",
        10: "角色收藏",
        12: "人物收藏",
    }

    events: list[dict] = []

    for event in raw:
        if not isinstance(event, dict):
            continue

        t = event.get("type", -1)
        # 丢弃每日签到/互动事件，无分析价值
        if t == 2:
            continue

        label = _EVENT_LABELS.get(t, f"动态(type={t})")
        memo = event.get("memo", {})
        if not isinstance(memo, dict):
            continue

        ts = event.get("createdAt", 0)
        created_at = (
            datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
            if ts else ""
        )

        item: dict = {"type": label, "created_at": created_at}

        # ── 收藏事件（type=9 条目 / 10 角色 / 12 人物） ──
        if "subject" in memo:
            subj_list = memo.get("subject", [])
            if isinstance(subj_list, list) and subj_list:
                s = subj_list[0]
                if isinstance(s, dict):
                    subj = s.get("subject", {}) or {}
                    item["subject_name"] = _cn_name(
                        subj.get("name", ""), subj.get("nameCN")
                    )
                    item["subject_id"] = subj.get("id", 0)
                    rate = s.get("rate", 0) or 0
                    if rate:
                        item["rate"] = rate
                    comment = (s.get("comment") or "").strip()
                    if comment:
                        item["comment"] = _truncate(comment, 100)

        # ── 观看进度事件（type=0） ──
        elif "progress" in memo:
            prog = memo.get("progress", {})
            if isinstance(prog, dict):
                batch = prog.get("batch", {}) or {}
                if isinstance(batch, dict):
                    subj = batch.get("subject", {}) or {}
                    item["subject_name"] = _cn_name(
                        subj.get("name", ""), subj.get("nameCN")
                    )
                    item["subject_id"] = subj.get("id", 0)
                    eps = batch.get("epsTotal", "")
                    if eps:
                        item["eps_total"] = eps

        # ── 日志发布事件（type=1） ──
        elif "blog" in memo:
            blog = memo.get("blog", {})
            if isinstance(blog, dict):
                item["blog_id"] = blog.get("id", 0)
                item["title"] = blog.get("title", "")
                summary = (blog.get("summary") or "").strip()
                if summary:
                    item["summary"] = _truncate(
                        _strip_bbcode(summary), 150
                    )
                item["replies"] = blog.get("replies", 0)

        events.append(item)
        if len(events) >= limit:
            break

    return {"events": events, "total": len(events)}
