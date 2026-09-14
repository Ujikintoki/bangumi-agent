"""形状 pin 测试 —— 记忆层压缩「按数据形状分派」的判据（HANDOFF 18 Step 0）。

本文件钉三样东西：
1. **判据** —— 16 个注册工具的返回形状各落哪个桶（`EXPECTED` 表）；
2. **记录列表桶的压缩输出** —— 8 个工具的字段存活表（`RECORD_FIELD_CASES`，Step 2 加）；
3. **三个桶的压缩输出与「名单消失」** —— 单体桶的字段存活 / 早退 / 预算
   （Step 4 加）、容器披露、以及「输出只由形状决定，与工具名无关」（Step 5 加）。

第 2、3 项放这里而不是 `test_phase5_l1.py`，是因为要用到上面这批**真实形状**夹具；
那些压缩测试喂的是自造的 search 形状，`hot_topics` 压出 10 行 `- ?` 也照样绿。

为什么需要这个文件：上一次「压缩丢字段」的缺陷能活很久，是因为两层各测各的、
中间没人测 —— 压缩测试喂的是自造的假形状，而真实产出侧没人喂进压缩入口。
这里的每一行都对应一个真实工具的**真实形状**。

取材（都不联网）：

· **真实**：``eval/results/frozen/reply_quality-20260914-013940-4a9b472.json``
  里 10 个工具的返回，逐条**缩水**（字符串截 30 字、列表截 2 条，**键一个不删**）。
  已逐条验证 48/48 条缩水前后落桶一致 —— 判据只看键集合 / 容器类型 / 标量计数 /
  容器是否非空，不看值的长度，所以缩水夹具与真实样本等价。
· **手造**：冻结样本缺的 6 个工具，raw 走 ``clients/sanitizers.py`` 的真实产出
  （签名照抄 HANDOFF §5，别凭印象改）；``get_blog`` 无法用 sanitizer 造（它是
  ``clients/client.py`` 手工拼的）→ 直接写三个入参组合的 dict。
· 另外补两个**成功形状**：``user_profile`` / ``user_timeline`` 的冻结样本恰好都是
  失败形状（``*_error`` / ``{"_error"}``），只测失败形状等于没测成功路径。

判据四条缺一不可，每条都对应真实工具（削弱任何一条，反向哨兵测试就会红）：

===========================  ============================================
条件                          挡住的真实形状
===========================  ============================================
非空顶层 list 恰好 1 个        ``get_bangumi_subject_detail``（rating_count + tags 两个）
顶层不许有 dict               ``get_blog``（顶部永远有 blog(dict)）、``get_user_profile``
元素必须是 dict               ``get_episode_comments``（comments 是 str 列表）
元素必须带 id                 ``get_bangumi_subject_detail`` 的 tags（无 id）
===========================  ============================================
"""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import ToolMessage

from agent.memory.short_term import (
    _RECORD_ID_KEYS,
    _classify,
    _has_record_id,
    _try_json,
)
from clients import sanitizers as S

# ═══════════════════════════════════════════════════════════════════
# 夹具 A：冻结样本的 10 个工具（缩水：字符串≤30 字、列表≤2 条，键全留）
# ═══════════════════════════════════════════════════════════════════

REAL_SHAPES: dict[str, dict] = {
    # 2026-09-14 冻结样本第 1 条。rating_count(int 列表) + tags(无 id 的 dict 列表)
    # 是两个非空顶层 list —— 「恰好 1 个」这条挡的就是它。
    "get_bangumi_subject_detail": {
        "id": 265,
        "name": "新世紀エヴァンゲリオン",
        "name_cn": "新世纪福音战士",
        "type": "动画",
        "info": "26话 / 1995年10月4日 / 庵野秀明 / GAIN…",
        "date": "1995-10-04",
        "eps": 26,
        "volumes": 0,
        "series": False,
        "series_entry": 0,
        "nsfw": False,
        "summary": "　　2000年，一个科学探险队在南极洲针对被称作“第一使徒”…",
        "score": 8.68,
        "rank": 23,
        "rating_total": 34342,
        "rating_count": [138, 48],
        # infobox 的键值对判据无关（dict 的存在性才重要），保留 2 键示意
        "collection": {"想看": 5517, "看过": 54336},
        "tags": [{"name": "EVA", "count": 10399}, {"name": "庵野秀明", "count": 7762}],
        "infobox": {"导演": "庵野秀明", "音乐": "鷺巣詩郎"},
        "_next": "如需口碑数据调 get_subject_opinions(2…",
    },
    # daily_summary 是 items[:3] 名字拼的摘要；顶层标量 0 个
    "get_calendar": {
        "daily_summary": "今日热门：碧蓝之海 第三季、与奔驰于透明之夜的你，谈一场看不…",
        "items": [
            {"id": 569116, "name": "碧蓝之海 第三季", "score": 7.13, "watchers": 7991},
            {"id": 607340, "name": "与奔驰于透明之夜的你，谈一场看不见的恋爱。",
             "score": 6.97, "watchers": 3478},
        ],
    },
    # ★ 元素没有 name/name_cn，只有 subject_name/title —— 压缩行取名字的候选链必须覆盖
    "get_hot_topics": {
        "items": [
            {"title": "押井守和神山健治是对的", "reply_count": 24,
             "subject_name": "攻壳机动队 THE GHOST IN THE SHELL", "subject_id": 496276},
            {"title": "《萤火虫的出嫁》百度百科、豆瓣被删除", "reply_count": 76,
             "subject_name": "萤火虫的出嫁", "subject_id": 621108},
        ],
        "total": 100,
    },
    # ★ 元素只有 character_id（没有 id）—— id 键集四键缺一不可的实证
    "get_subject_characters": {
        "subject_id": 400602,
        "characters": [
            {"character_id": 86246, "name": "芙莉莲", "char_type": "主角",
             "casts": "种崎敦美, 李蝉妃(中配), 李昀晴(中配)"},
            {"character_id": 86247, "name": "菲伦", "char_type": "主角",
             "casts": "市之濑加那, 万苏婉(中配), 林沛笭(中配)"},
        ],
        "_next": "可查看角色详情如 get_character_detail(…",
    },
    # ★ 顶层非空 list 数量为 0（comments/reviews 都是 dict）、标量只有 1 个
    #   → 既不是列表型也不是单体，落「保持现状」。这不是「还没想好」，是断言。
    "get_subject_opinions": {
        "subject_id": 265,
        "comments": {
            "comments": ["[10星] 全世界最对我电波的TV作品，无法客观给出评价。我…", "[7星] 橙汁"],
            "rating_distribution": {"4-6": 1, "7-8": 3, "9-10": 2},
            "comment_count": 8,
        },
        "reviews": {
            "items": [
                {"id": 333419, "title": "心情略微复杂", "summary": "2026.8.23看完…",
                 "user_name": "静听风语丶", "reply_count": 0, "created_at": 1787497318},
                {"id": 332883, "title": "到底是哪一步错了呢", "summary": "此日志仅为记录…",
                 "user_name": "kkxqmk", "reply_count": 0, "created_at": 1787069989},
            ],
            "total": 123,
        },
    },
    "get_trending_subjects": {
        "summary": "当前动画趋势 Top 10：Re：从零开始的异世界生活 第四…",
        "items": [
            {"id": 633836, "name": "Re：从零开始的异世界生活 第四季 夺还篇",
             "type": "动画", "score": 7.81, "trending_score": 3614},
            {"id": 622206, "name": "尼古喵喵", "type": "动画",
             "score": 7.31, "trending_score": 2455},
        ],
        "total": 1000,
    },
    # 冻结样本里这条恰好是**失败形状**（用户不存在 → 一串 *_error），0 个标量
    "get_user_profile(失败形状)": {
        "username": "茕兔",
        "user_error": "未找到资源 (path=/p1/users/茕兔)",
        "collections_error": "未找到资源 (path=/p1/users/茕兔/colle…",
        "characters_error": "未找到资源 (path=/p1/users/茕兔/colle…",
        "blogs_error": "未找到资源 (path=/p1/users/茕兔/blogs…",
    },
    "get_user_timeline(失败形状)": {"_error": "未找到资源 (path=/p1/users/茕兔/timel…)"},
    "search_bangumi_subject": {
        "results": [
            {"id": 316025, "name": "新世紀エヴァンゲリオン", "name_cn": "新世纪福音战士",
             "type": "游戏", "score": 8, "rank": 0, "info": "1996年3月1日 / SS / アドベンチャーゲーム"},
            {"id": 229845, "name": "新世纪福音战士:破晓", "name_cn": "新世纪福音战士:破晓",
             "type": "游戏", "score": 4.75, "rank": 0, "info": "2017年2月22日 / Android、iOS / FTG…"},
        ],
        "total": 248,
        "_next": "拿到 subject_id 后必须调 get_bangumi…",
    },
    # 这份冻结样本是**产出侧投影之前**的 rag_output 记录（有 summary/infobox/_source），
    # 键集比投影后的索引卡大 —— 两种形状都落 record，判据不看值。
    "search_local_bangumi": {
        "results": [
            {"id": 464376, "name": "負けヒロインが多すぎる！", "name_cn": "败犬女主太多了！",
             "type": "动画", "info": "12话 / 2024年7月13日 / 北村翔太郎 / 雨森た…",
             "date": "2024-07-13", "eps": 12, "score": 7.96, "rank": 259,
             "rating_total": 27953, "_source": "rag",
             "_next": "如需口碑数据调 get_subject_opinions(4…",
             "summary": "没能赢得心上人恋情的女孩——“败犬女主”。\n爱吃的青梅竹马系…",
             "tags": ["恋爱", "校园"], "infobox": {"导演": "北村翔太郎", "原作": "雨森たきび"},
             },
            {"id": 293049, "name": "かぐや様は告らせたい?", "name_cn": "辉夜大小姐想让我告白?",
             "type": "动画", "info": "12话 / 2020年4月11日 / 畠山守(小俣真一) /…",
             "date": "2020-04-11", "eps": 12, "score": 7.89, "rank": 313,
             "rating_total": 24652, "_source": "rag", "_next": "如需口碑数据调…",
             "summary": "人才云集的精英校·秀知院学园…",
             "tags": ["恋爱", "搞笑"], "infobox": {"导演": "畠山守(小俣真一)"},
             },
        ],
        "total": 10,
    },
    # 空搜索结果：results 是空 list → 不构成「非空顶层 list」；标量只有 total 1 个
    "search_bangumi_subject(无结果)": {"results": [], "total": 0},
    # 错误返回：0 个标量、0 个非空 list。agent 侧的工具失败一律走这个形状。
    "错误返回(_error)": {"_error": "搜索失败。连接超时"},
}

# ═══════════════════════════════════════════════════════════════════
# 夹具 B：冻结样本缺的 6 个工具 —— raw 走真实 sanitizer（签名照抄 HANDOFF §5）
# ═══════════════════════════════════════════════════════════════════

_EP_COMMENT = {"comment": "这集演出很棒" * 5, "replies": 2}

_BLOG_REAL = {
    "id": 1, "title": "标题", "content": "正文" * 50, "tags": ["随笔"],
    "created_at": "2026-09-01", "replies": 4, "views": 1200, "type": 1,
    "related": 0, "user_name": "alice",
}
"""``get_blog`` 的 ``blog`` 子字典 —— 逐字抄 ``clients/client.py:452-463``。

**别照印象删字段**：5 个标量是它会被封套机制当作「单体记录」的原因，
少写几个会让「get_blog 不受影响」这个结论反过来。
"""

# 时光机事件的三种真实原始形状（clients/sanitizers.py:1144-1240 的三个分支）：
#   type=9/10/12 收藏 → subject_id ；type=0 进度 → subject_id ；type=1 日志 → **只有 blog_id**
_TL_COLLECT = {"type": 9, "createdAt": 1787497318, "memo": {"subject": [
    {"subject": {"id": 265, "name": "新世紀エヴァンゲリオン", "nameCN": "新世纪福音战士"},
     "rate": 9, "comment": "神作" * 20}]}}
_TL_PROGRESS = {"type": 0, "createdAt": 1787069989, "memo": {"progress": {"batch": {
    "subject": {"id": 400602, "name": "葬送のフリーレン", "nameCN": "葬送的芙莉莲"},
    "epsTotal": "28"}}}}
_TL_BLOG = {"type": 1, "createdAt": 1787000000, "memo": {"blog": {
    "id": 333419, "title": "心情略微复杂", "summary": "2026.8.23看完《新世纪福音战士》。" * 3,
    "replies": 4}}}

SYNTH_SHAPES: dict[str, dict] = {
    # ★ 这里【不能】只抄 `sanitize_episode_comments` 的产出：真实工具返回是
    #   `{"episode": {...}, "comments": [...], "comment_count": N}` —— episode 是
    #   `client.py:187` 事后 merge 进去的 dict（失败时也塞 `{}`，仍是 dict）。
    #   2026-09-14 审计发现：照 sanitizer 裸产出造的夹具会落 text 桶，真实形状
    #   落 fallback，**两个都"对"，但钉住的是工具永远不会返回的形状**。
    #   这正是 test_sanitizers.py 那条跨层契约注释说的「两层各测各的」。
    #   照 HANDOFF §5 的「用 sanitizer 造样本」照做就会踩到这里。
    "get_episode_comments": {
        "episode": S.sanitize_episode_detail(
            {"id": 1, "sort": 1, "name": "第1话", "nameCN": "第1话",
             "airdate": "2024-01-01", "duration": "24m", "desc": "梗概" * 20,
             "comment": 8, "subject": {"id": 400602, "name": "葬送のフリーレン",
                                       "nameCN": "葬送的芙莉莲"}}
        ),
        **S.sanitize_episode_comments([_EP_COMMENT], 30),
    },
    "get_entity_comments": S.sanitize_entity_comments(
        [_EP_COMMENT], 30, "character", 1, {"id": 1, "name": "某人"}
    ),
    "get_subject_episodes": S.sanitize_subject_episodes(
        {"data": [{"id": 1, "type": 0, "sort": 1, "name": "第1话", "name_cn": "第1话",
                   "airdate": "2024-01-01", "desc": "梗概" * 20}]}
    ),
    # infobox 的**原始**形状是 ``[{"key":…, "values":[{"v":…}]}]``（sanitizers.py:185-191），
    # 不是 ``{"key":…, "value":…}``；``comment`` 是 **int** 不是 dict
    # （rag/corpus/processed/{characters,persons}.json 实测）。
    # 2026-09-14 Step 4 抓到：照印象写 ``{"value":…}`` 会被 `_clean_infobox`
    # 静默清成 ``{}`` —— 夹具照样绿，但「丢弃的容器要披露」那条路径根本没被测到。
    "get_character_detail": S.sanitize_character_detail(
        {"id": 1, "name": "角色", "role": 1, "summary": "简介" * 30,
         "infobox": [{"key": "性别", "values": [{"v": "女"}]}],
         "comment": 104, "collects": 695}
    ),
    "get_person_detail": S.sanitize_person_detail(
        {"id": 2, "name": "声优", "career": ["seiyu", "artist"],
         "summary": "简介" * 30, "comment": 122, "collects": 1026,
         "infobox": [{"key": "性别", "values": [{"v": "女"}]}]}
    ),
    # get_blog 是 clients/client.py 手工拼的，sanitizer 造不出来 → 直接写。
    # 形状随 include_comments / include_subjects 变（默认都 True），三个组合都要测。
    # ⚠️ ``blog`` 的**真实字段**逐字抄自 ``clients/client.py:452-463``：10 个键、
    # **5 个标量**（id/replies/views/type/related）+ tags(list)。
    # 2026-09-14 Step 6 抓到：这里原本只写了 ``{id,title,content}``（1 个标量），
    # 差点让人以为「blog 不够单体骨架，封套机制不会误伤 get_blog」——
    # 真实形状是**够的**。同类夹具事故第四次，记在此处。
    "get_blog": {"entry_id": 1, "blog": _BLOG_REAL,
                 "comments": ["评论一" * 10, "评论二" * 10],
                 "subjects": [{"id": 8, "name": "EVA"}]},
    "get_blog(无 subjects)": {"entry_id": 1, "blog": _BLOG_REAL,
                              "comments": ["评论一" * 10]},
    "get_blog(无 comments)": {"entry_id": 1, "blog": _BLOG_REAL,
                              "subjects": [{"id": 8, "name": "EVA"}]},
    # user_profile 的**成功形状**：1 个 str + 3 个 dict + 3 个 list
    # （键名取自 clients/client.py:338-378 的实际组装，不是印象）
    "get_user_profile(成功形状)": {
        "username": "alice",
        "user": {"id": 1, "nickname": "A", "username": "alice", "sign": "", "avatar": "",
                 "joined_at": "2015-01-01"},
        "user_stats": {"anime": {"collect": 100}},
        "collections": {"items": [{"id": 1, "name": "X"}], "total": 100},
        "characters": [{"id": 2, "name": "角色", "role": "主角"}],
        "persons": [{"id": 3, "name": "人物"}],
        "blogs": [{"id": 4, "title": "日志"}],
    },
    # ── user_timeline 的三种真实事件组合（走真实 sanitizer，别凭印象写）──
    # ★ 与 HANDOFF §2 陷阱 5 / §3 表的出入（2026-09-14 实测）：
    #   文档说 timeline「中位 24 tok、永远走不到压缩、落保持现状」——两条都不成立。
    #   那 24 tok 是冻结样本里的**失败返回**（{"_error": ...}），不是真实 timeline。
    #   20 条事件的真实 payload：仅日志 3,235 tok、收藏/进度 1,776 tok、混合 2,374 tok
    #   —— 全部远超 300 阈值，**一定会走到压缩函数**。
    "get_user_timeline(仅日志事件)": dict(
        S.sanitize_timeline_events([_TL_BLOG, _TL_BLOG], 50), username="alice"
    ),
    "get_user_timeline(收藏/进度事件)": dict(
        S.sanitize_timeline_events([_TL_COLLECT, _TL_PROGRESS], 50), username="alice"
    ),
    "get_user_timeline(混合事件)": dict(
        S.sanitize_timeline_events([_TL_COLLECT, _TL_PROGRESS, _TL_BLOG], 50), username="alice"
    ),
}

ALL_SHAPES = {**REAL_SHAPES, **SYNTH_SHAPES}

# ═══════════════════════════════════════════════════════════════════
# 落桶 pin 表（HANDOFF §3 的 18 行 + 边界行）
# ═══════════════════════════════════════════════════════════════════
# None = 「保持现状」（走 _compress_by_truncation，行为与改动前逐字节一致）。
# 这一档不是「还没想好」，是本任务「不引入新 bug」的落点。

EXPECTED: dict[str, tuple[str | None, str | None]] = {
    # ── 记录列表：非空顶层 list 1 个 + 无 dict 信封 + 元素是 dict + 元素有 id ──
    "search_bangumi_subject": ("record", "results"),
    "search_local_bangumi": ("record", "results"),
    "get_calendar": ("record", "items"),
    "get_trending_subjects": ("record", "items"),
    "get_hot_topics": ("record", "items"),
    "get_subject_episodes": ("record", "items"),
    "get_subject_characters": ("record", "characters"),
    # ── 文本列表：键名 comments + 元素是 str ──
    "get_entity_comments": ("text", "comments"),
    # ★ 与 HANDOFF §3 的表【不一致】，按真实形状钉（2026-09-14 审计）：
    #   文档说 episode_comments 也落文本列表桶，但它的真实返回顶层永远有
    #   `episode`(dict)（`clients/client.py:187`），被「顶层不许有 dict」挡在外面。
    #   那一行的夹具只抄了 sanitizer 裸产出，漏掉了 merge 进去的 episode。
    #   实测代价：7,297 tok → 299 tok，**30 条评论一条不剩**（desc 把预算吃光）。
    #   它属于 §6「多部件信封」族（episode + comments 两个子部件），本任务不修。
    "get_episode_comments": (None, None),
    # ── 单体：有标量骨架（9 / 4 / 4 个标量）—— 本任务的收益点（Step 4）──
    # 与下面「保持现状」那一档的本质区别：单体桶要**重排**字段以救回 score/rank，
    # 而 None 那档一个字节都不动。`get_person_detail` 的 `career` 是 list of str
    # （元素不是 dict）、`infobox` 是 dict → 挡在记录桶外，落到这里。
    "get_bangumi_subject_detail": ("plain", None),
    "get_character_detail": ("plain", None),
    "get_person_detail": ("plain", None),
    # ── 保持现状（多部件信封 / 标量骨架不足）──
    "get_subject_opinions": (None, None),          # 顶层无 list、标量 1 → 不是单体
    "get_blog": (None, None),                      # 顶层有 blog(dict)
    "get_blog(无 subjects)": (None, None),         # 只剩 comments 也不许当文本列表
    "get_blog(无 comments)": (None, None),         # 只剩 subjects 也不许当记录列表
    "get_user_profile(成功形状)": (None, None),     # 3 个 list + 3 个 dict，标量 0
    "get_user_profile(失败形状)": (None, None),
    "get_user_timeline(失败形状)": (None, None),
    "search_bangumi_subject(无结果)": (None, None),
    "错误返回(_error)": (None, None),
    # ── ★ get_user_timeline：与 HANDOFF §2 陷阱 5 / §3 表【不一致】，按实测钉 ──
    #   「元素必须有 id」这条没放宽，落桶差异来自真实形状本身：
    #   收藏/进度事件**必带 subject_id**（clients/sanitizers.py:1200、1218）→ 落 record；
    #   只有日志事件时才无 id（给的是 blog_id，不在四键集）→ 落「保持现状」。
    #   两种都是真实形状，不是「哪个对」的问题。
    "get_user_timeline(收藏/进度事件)": ("record", "events"),
    "get_user_timeline(混合事件)": ("record", "events"),
    "get_user_timeline(仅日志事件)": (None, None),
}


@pytest.mark.parametrize("tool", sorted(EXPECTED))
def test_shape_lands_in_expected_bucket(tool: str) -> None:
    """16 个注册工具的真实形状各落哪个桶 —— 本任务的安全网。"""
    data = ALL_SHAPES[tool]
    kind, key, items = _classify(data)

    assert (kind, key) == EXPECTED[tool], (
        f"{tool} 落桶不符：期望 {EXPECTED[tool]}，实际 ({kind}, {key})。"
        f"顶层键={list(data)}"
    )
    if kind in ("record", "text"):
        assert items is data[key]
    else:
        assert items is None


def test_pin_table_covers_every_registered_tool() -> None:
    """pin 表必须覆盖全部 16 个注册工具，漏一个就等于没钉。"""
    registered = {
        "search_bangumi_subject", "get_bangumi_subject_detail", "get_subject_opinions",
        "get_subject_characters", "get_subject_episodes", "get_episode_comments",
        "get_entity_comments", "get_calendar", "get_trending_subjects", "get_hot_topics",
        "search_local_bangumi", "get_character_detail", "get_person_detail",
        "get_user_profile", "get_user_timeline", "get_blog",
    }
    pinned = {t.split("(")[0] for t in EXPECTED}
    assert registered - pinned == set(), f"未钉住的工具：{registered - pinned}"


def test_bangumi_subject_detail_with_empty_rating_count_is_still_plain() -> None:
    """rating_count 可能是空数组（rating.get("count", [])）。

    此时只剩 tags 一个 list —— 只靠「非空 list ≥ 2」会漏，必须靠「元素必须带 id」
    这条兜住（tags 是 {"name","count"}，无 id）。
    """
    data = dict(REAL_SHAPES["get_bangumi_subject_detail"], rating_count=[])
    assert _classify(data) == ("plain", None, None)


def test_empty_comment_list_does_not_become_text_bucket() -> None:
    """评论为空的信封不许落文本列表桶（空 list 不构成「非空顶层 list」）。

    entity_comments 空返回有 2 个标量（entity_id / comment_count）→ 会落 plain，
    靠「单体桶内容已在预算内则原样返回」兜住（见 test_phase5_l1.py 的契约测试）。
    """
    data = dict(SYNTH_SHAPES["get_entity_comments"], comments=[], comment_count=0)
    kind, key, _ = _classify(data)
    assert kind != "text", f"空 comments 不该落文本列表桶（实际 {kind}/{key}）"


# ═══════════════════════════════════════════════════════════════════
# 反向哨兵：削弱判据必须误判这三条 —— 它们就是那两条条件存在的理由
# ═══════════════════════════════════════════════════════════════════


def _naive_classify(data: dict) -> str:
    """被削弱的判据：有非空 dict 列表就算记录列表（少了「元素带 id」和「无 dict 信封」）。"""
    for v in data.values():
        if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            return "record"
    return "other"


@pytest.mark.parametrize("tool", [
    "get_bangumi_subject_detail",      # tags 是 dict 列表，但无 id
    "get_blog",                        # subjects 是 dict 列表，但顶层有 blog(dict)
    "get_blog(无 comments)",           # 同上，少了 comments 更看不出来
])
def test_weakened_criteria_would_misjudge(tool: str) -> None:
    data = ALL_SHAPES[tool]
    assert _naive_classify(data) == "record", "夹具没触发误判，哨兵失效"
    assert _classify(data)[0] != "record", "生产判据也误判了 —— 那两条条件没起作用"


# ═══════════════════════════════════════════════════════════════════
# 判据自身的边界：解析失败 / 顶层非 dict / 空内容
# ═══════════════════════════════════════════════════════════════════


def test_try_json_rejects_non_dict_json() -> None:
    """顶层不是 dict 的合法 JSON 必须返回 None → 走现状截断。

    不这么做的话判据会拿 list 去调 .items() 抛 AttributeError ——
    CLAUDE.md 规则 1：记忆层不抛异常。
    """
    assert _try_json('{"a": 1}') == {"a": 1}
    assert _try_json("[1, 2, 3]") is None
    assert _try_json('"纯字符串"') is None
    assert _try_json("123") is None
    assert _try_json("null") is None
    assert _try_json("不是 JSON") is None
    assert _try_json("") is None


def test_record_id_keyset_is_exactly_four() -> None:
    """id 键集四键缺一不可（两处独立定义：本处与 eval/reply_quality_judge.py）。"""
    assert set(_RECORD_ID_KEYS) == {"id", "character_id", "subject_id", "person_id"}
    for key in _RECORD_ID_KEYS:
        assert _has_record_id({key: 1}), f"{key} 未被识别为记录身份键"
    assert not _has_record_id({"name": "x", "count": 1})


def test_fixtures_are_json_serializable() -> None:
    """夹具必须是 JSON 可序列化的 —— 真实 ToolMessage 的 content 就是 JSON 文本。"""
    for tool, data in ALL_SHAPES.items():
        json.dumps(data, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════════
# 记录列表桶的压缩输出（HANDOFF 18 Step 2）
# ═══════════════════════════════════════════════════════════════════
# 判据对 ≠ 输出对。判据只保证「这 8 个工具进了同一个函数」，字段活没活下来
# 是另一回事 —— 下面每个片段都对应一个真实工具**原本会丢**的东西。
#
# 这比 test_phase5_l1.py 的压缩测试更强的地方：那些测试喂自造的 search 形状，
# 所以 `hot_topics` 压出 10 行 `- ?`、`characters` 丢光 id、`episodes` 丢光日期，
# 三个缺陷在两侧都是绿的（同一个教训见 test_sanitizers.py 的跨层契约注释）。

RECORD_FIELD_CASES: dict[str, tuple[str, ...]] = {
    # search 卡：回归哨兵 —— 这两条今天就是绿的，改动不许弄坏
    "search_bangumi_subject": ("新世纪福音战士", "id=316025", "⭐8"),
    # 投影后的 tags 是 str 列表（投影前是 dict 列表）→ 尾串收得住
    "search_local_bangumi": ("败犬女主太多了！", "id=464376", "⭐7.96", "tags=恋爱"),
    # 日历整块 584 tok 全被掐；现在 10 天全部留名 + 评分 + 追番数
    "get_calendar": ("碧蓝之海 第三季", "id=569116", "⭐7.13", "watchers=7991"),
    # 趋势整块被掐，trending_score 是它唯一独有的信号
    "get_trending_subjects": ("id=633836", "trending_score=3614"),
    # ★ 元素没有 name/name_cn，只有 title/subject_name —— 旧的名字链取不到值，
    #   整行渲染成 `- ?`（比不压缩还糟）
    "get_hot_topics": ("押井守和神山健治是对的", "subject_id=496276", "reply_count=24"),
    # ★ 元素只有 character_id：旧的 `item.get("id")` 取不到 → 20 个角色全丢身份键
    "get_subject_characters": ("芙莉莲", "character_id=86246", "char_type=主角"),
    # ★ 键名是 airdate 不是 date：旧实现读 date → 播出日期整列丢失
    "get_subject_episodes": ("第1话", "id=1", "airdate=2024-01-01"),
    # ★ 时间线走的是 subject_id 不是 id，名字在 subject_name 上
    "get_user_timeline(收藏/进度事件)": ("新世纪福音战士", "subject_id=265", "type=收藏"),
}


@pytest.mark.parametrize("tool", sorted(RECORD_FIELD_CASES))
def test_record_bucket_keeps_fields(tool: str) -> None:
    """8 个记录型工具的字段存活表 —— 逐个工具验收压缩输出。"""
    from agent.memory.short_term import _compress_tool_result

    data = ALL_SHAPES[tool]
    assert _classify(data)[0] == "record", f"{tool} 不在记录列表桶，本表对它无意义"

    out = _compress_tool_result(
        ToolMessage(content=json.dumps(data, ensure_ascii=False),
                    tool_call_id="t1", name=tool.split("(")[0])
    ).content

    missing = [s for s in RECORD_FIELD_CASES[tool] if s not in out]
    assert not missing, f"{tool} 压缩后丢了 {missing}\n实际输出：\n{out}"


def test_hot_topics_never_renders_placeholder_name() -> None:
    """`- ?` 是旧实现的真实产物，且比不压缩更糟（10 行全是问号）。

    单独立一条而不是并进上表：问号不会「缺失」，它在所有片段断言下都是绿的。
    """
    from agent.memory.short_term import _compress_tool_result

    out = _compress_tool_result(ToolMessage(
        content=json.dumps(ALL_SHAPES["get_hot_topics"], ensure_ascii=False),
        tool_call_id="t1", name="get_hot_topics")).content
    assert "?" not in out and "？" not in out, f"压出了占位名字：\n{out}"


def test_episode_comments_envelope_blocks_text_bucket() -> None:
    """`get_episode_comments` 的 episode(dict) 必须把它挡在文本列表桶外。

    这是「顶层不许有 dict」这条判据的**正向哨兵**（反向哨兵见
    `test_weakened_criteria_would_misjudge`）：这条判据挡掉的不只是 §6 那三个
    多部件工具，还有一个**大家都以为它能进文本桶**的工具。
    去掉 envelope 条件 → 它会以「episode 元数据全丢」为代价换到评论。
    """
    real = SYNTH_SHAPES["get_episode_comments"]
    assert isinstance(real["episode"], dict), "夹具前提不成立：episode 不是 dict"

    assert _classify(real)[0] != "text", "带 episode 信封的真实形状不该落文本列表桶"
    # 去掉信封就落文本桶 —— 证明挡住它的确实是 envelope 这条
    bare = dict(real, episode=None)
    del bare["episode"]
    assert _classify(bare) == ("text", "comments", bare["comments"])


def test_text_bucket_keeps_comments_and_identity() -> None:
    """文本列表桶：评论要留得下，实体身份也不能挤掉。"""
    from agent.memory.short_term import _compress_tool_result

    data = SYNTH_SHAPES["get_entity_comments"]
    assert _classify(data)[0] == "text", "夹具不在文本列表桶，本测试无意义"

    out = _compress_tool_result(ToolMessage(
        content=json.dumps(data, ensure_ascii=False),
        tool_call_id="t1", name="get_entity_comments")).content

    assert "[已压缩]" in out
    assert "entity_name=某人" in out, f"实体名被挤掉了：\n{out}"
    assert data["comments"][0][:20] in out, f"评论没活下来：\n{out}"
    # 旧路径的失败形态：JSON 拦腰截断
    assert not out.startswith("[已压缩] {")


def test_text_bucket_discloses_dropped_comments() -> None:
    """条数超过预算时必须披露，且不能把整块变成空消息。"""
    from agent.memory.short_term import _compress_tool_result

    comments = [f"第{i}楼：" + "这集演出很棒，作画在线。" * 8 for i in range(30)]
    data = {"entity_type": "character", "entity_id": 1, "entity_name": "芙莉莲",
            "comments": comments, "comment_count": len(comments)}
    out = _compress_tool_result(ToolMessage(
        content=json.dumps(data, ensure_ascii=False),
        tool_call_id="t1", name="get_entity_comments")).content

    assert out.strip() != "[已压缩]", "压出了空消息 —— 正是陷阱 3 的失败形态"
    assert "未列出" in out, f"砍了评论却没说明：\n{out}"
    from agent.memory.short_term import _MAX_SINGLE_MESSAGE_TOKENS, count_tokens
    assert count_tokens(out) <= _MAX_SINGLE_MESSAGE_TOKENS


def test_record_bucket_does_not_depend_on_tool_name() -> None:
    """记录列表桶认形状不认工具名 —— 换个名字输出逐字节相同。

    这是「两个硬编码名单消失」的验收：旧实现换个名字就掉进 300 tok 兜底。
    """
    from agent.memory.short_term import _compress_tool_result

    content = json.dumps(ALL_SHAPES["get_hot_topics"], ensure_ascii=False)
    outs = {
        name: _compress_tool_result(
            ToolMessage(content=content, tool_call_id="t1", name=name)).content
        for name in ("get_hot_topics", "某个还没写出来的工具", "")
    }
    assert len(set(outs.values())) == 1, f"输出随工具名变了：{outs}"


def test_record_bucket_output_stays_under_single_message_cap() -> None:
    """造最坏情况也不能越过 `_MAX_SINGLE_MESSAGE_TOKENS`。

    越了会被 `_truncate_oversized_messages` 从中间切断 —— 那正是本任务要
    消灭的失败形态。行数封顶（20 行）挡不住它：20 条带 8 个 tags 的 local 卡
    实测 2,526 tok。
    """
    from agent.memory.short_term import (
        _MAX_SINGLE_MESSAGE_TOKENS,
        _compress_tool_result,
    )

    blob = "长" * 30
    records = [{
        "id": i, "name_cn": blob, "name": "X" * 60, "type": "动画",
        "date": "2024-07-13", "eps": 12, "score": 8.0, "rank": 259,
        "rating_total": 27953, "_source": "rag",
        "info": "12话 / 2024年7月13日 / 北村翔太郎 / 雨森たきび",
        "tags": ["恋爱", "校园", "搞笑", "日常", "青春", "漫画改", "治愈", "催泪"],
    } for i in range(20)]
    out = _compress_tool_result(ToolMessage(
        content=json.dumps({"total": 20, "shown": 20, "results": records}, ensure_ascii=False),
        tool_call_id="t1", name="search_local_bangumi")).content

    from agent.memory.short_term import count_tokens
    assert count_tokens(out) <= _MAX_SINGLE_MESSAGE_TOKENS, f"越限：{count_tokens(out)} tok"
    # 被预算砍掉的条数必须披露 —— 模型不能以为 12 条就是全部
    assert "未列出" in out, f"砍了行却没说明：\n{out[:300]}"


# ═══════════════════════════════════════════════════════════════════
# 单体桶的压缩输出（HANDOFF 18 Step 4 —— 本任务的收益点）
# ═══════════════════════════════════════════════════════════════════
# 旧路径 `_compress_by_truncation(300)` 从 JSON 头部切 300 token：
# `get_bangumi_subject_detail` 实测 1,343 → 299 tok，`score`(4 字) 与 `rank`(2 字)
# 被前面 284 字的 `summary` 挤出窗口，**6/6 全丢** —— 模型记得剧情梗概、
# 忘了评分排名。对「损友」人格来说，评分排名恰恰是它吐槽的依据。


def _subject_detail_raw(**over) -> dict:
    """造一条**真实量级**的条目详情（raw 走 sanitize_subject_detail，键名照抄 API）。

    量级照冻结样本 #0 抄（1,343 tok）：summary 顶到 sanitizer 的 300 字上限、
    tags 十条、collection/infobox 非空 —— 小了就触发早退，测不到重排。
    """
    raw = {
        "id": 265,
        "name": "新世紀エヴァンゲリオン",
        "nameCN": "新世纪福音战士",
        "type": 2,
        "info": "26话 / 1995年10月4日 / 庵野秀明 / GAINAX → 庵野秀明 / 貞本義行",
        "summary": "　　2000年，一个科学探险队在南极洲针对被称作“第一使徒”亚当的"
                   "“光之巨人”进行探险。在对其进行接触实验时，“光之巨人”自毁，"
                   "从而发生了“第二次冲击”，进而导致世界大战。最后，人类人口减半。"
                   "根据对“第二次冲击”的调查，联合国在不久之后，于南极设立了一条"
                   "严禁人员出入的封锁线。seele 与碇源堂在暗中执行着人类补完计划，"
                   "而少年们被推上了驾驶舱。",
        "airtime": {"date": "1995-10-04"},
        "eps": 26,
        "rating": {"score": 8.68, "rank": 23, "total": 34342, "count": [138, 48, 74]},
        "collection": {"1": 5517, "2": 54336},
        "tags": [{"name": n, "count": c} for n, c in (
            ("EVA", 10399), ("庵野秀明", 7762), ("科幻", 6103), ("机战", 4893),
            ("TV", 4211), ("神作", 3902), ("90年代", 2871), ("GAINAX", 2455),
            ("萝卜", 2103), ("心理", 1877))],
        "infobox": [{"key": "导演", "values": [{"v": "庵野秀明"}]},
                    {"key": "脚本", "values": [{"v": "庵野秀明"}]},
                    {"key": "人物设定", "values": [{"v": "貞本義行"}]}],
    }
    raw.update(over)
    return S.sanitize_subject_detail(raw)


def test_plain_bucket_keeps_scalars_behind_a_long_summary() -> None:
    """★ 本任务的收益点：排在前面的长 summary 不许把后面的 score/rank 挤掉。

    同时断言 summary 自己也在 —— 否则「救回 score/rank」就成了拿一个损失
    换另一个损失，而不是修复。
    """
    from agent.memory.short_term import _PLAIN_MAX_TOKENS, _compress_tool_result, count_tokens

    data = _subject_detail_raw()
    content = json.dumps(data, ensure_ascii=False)
    assert count_tokens(content) > _PLAIN_MAX_TOKENS, "夹具没超预算，测不到重排"

    out = _compress_tool_result(ToolMessage(
        content=content, tool_call_id="t1", name="get_bangumi_subject_detail")).content

    assert "⭐8.68" in out, f"score 被 summary 挤掉了：\n{out}"
    assert "#23" in out, f"rank 被 summary 挤掉了：\n{out}"
    # 拿 strip 过的比：summary 开头的全角缩进（Bangumi 原文的「　　」）会被去掉
    assert data["summary"].strip()[:20] in out, f"summary 自己也丢了（那不算修复）：\n{out}"
    assert out.startswith("[已压缩]"), f"旧路径的失败形态（JSON 拦腰截断）：\n{out[:120]}"


def test_plain_bucket_discloses_dropped_containers() -> None:
    """丢掉的容器必须披露 —— 模型得知道它没看到什么，否则又会「以为看全了」。

    这条是**正向哨兵**：夹具的 infobox 必须走真实的
    ``[{"key":…, "values":[{"v":…}]}]`` 形状。照印象写 ``{"value":…}``
    会被 `_clean_infobox` 静默清成 ``{}``，断言照样绿但什么都没测到
    （2026-09-14 Step 4 实际踩过）。
    """
    from agent.memory.short_term import _compress_tool_result

    data = _subject_detail_raw()
    assert data["infobox"] and data["tags"] and data["collection"], "夹具的容器是空的"

    out = _compress_tool_result(ToolMessage(
        content=json.dumps(data, ensure_ascii=False),
        tool_call_id="t1", name="get_bangumi_subject_detail")).content

    for key in ("infobox", "tags", "collection"):
        assert key in out, f"{key} 既没渲染也没披露 —— 模型不知道自己没看到它\n{out}"
    assert "未显示" in out


def test_plain_bucket_leaves_small_payload_untouched() -> None:
    """内容已在预算内 → 一个字节都不动（早退）。

    这条保护的是真实用例：`get_character_detail` 的真实 payload 只有 60–181 tok，
    今天整条（含 infobox）活着。重排字段顺序会把 infobox 换成一行「未显示」——
    那是把一个完整的小记录弄得更差。
    """
    from agent.memory.short_term import _PLAIN_MAX_TOKENS, _compress_tool_result, count_tokens

    data = SYNTH_SHAPES["get_character_detail"]
    content = json.dumps(data, ensure_ascii=False)
    assert count_tokens(content) <= _PLAIN_MAX_TOKENS, (
        f"夹具超了预算（{count_tokens(content)} tok），测不到早退"
    )

    msg = ToolMessage(content=content, tool_call_id="t1", name="get_character_detail")
    assert _compress_tool_result(msg) is msg, "超预算内的小记录被动过了"


def test_plain_bucket_output_stays_under_budget() -> None:
    """造最坏情况也不能越过 `_PLAIN_MAX_TOKENS`。

    上限不能靠估算：标量不参与预算竞争，长字符串只能一个个让位，
    「多少算够」得装配完实测（旧原型实测到过 421 tok）。
    """
    from agent.memory.short_term import _PLAIN_MAX_TOKENS, _compress_tool_result, count_tokens

    data = _subject_detail_raw(
        info="情" * 2000,                      # info 没有 sanitizer 侧的截断
        summary="梗概" * 400,                  # sanitizer 封在 300 字
        tags=[{"name": f"标{i}" * 20, "count": i} for i in range(30)],
        infobox=[{"key": f"键{i}", "values": [{"v": "值" * 60}]} for i in range(20)],
    )
    out = _compress_tool_result(ToolMessage(
        content=json.dumps(data, ensure_ascii=False),
        tool_call_id="t1", name="get_bangumi_subject_detail")).content

    assert count_tokens(out) <= _PLAIN_MAX_TOKENS, f"越限：{count_tokens(out)} tok\n{out}"
    assert "⭐8.68" in out, "预算再紧也不许动标量"


def test_plain_bucket_keeps_rating_histogram_whole() -> None:
    """rating_count 是 10 格评分分布，必须整留。

    记录桶的尾串只留 8 项（`_RECORD_TAIL_MAX_ITEMS`）—— 那是索引卡的取舍，
    单体桶照搬会把 10 格截成 8 格，分布看着就不完整了（「双峰还是单峰」是
    判断一部番是不是争议作的依据）。
    """
    from agent.memory.short_term import _compress_tool_result

    data = _subject_detail_raw(rating={"score": 8.68, "rank": 23, "total": 34342,
                                       "count": [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]})
    out = _compress_tool_result(ToolMessage(
        content=json.dumps(data, ensure_ascii=False),
        tool_call_id="t1", name="get_bangumi_subject_detail")).content

    assert "10、20、30、40、50、60、70、80、90、100" in out, f"评分分布被截了：\n{out}"


def test_plain_bucket_omits_zero_score_and_rank() -> None:
    """0 是「无人评分」的哨兵，渲染成 ⭐0 / #0 比不渲染更糟（同记录桶）。"""
    from agent.memory.short_term import _compress_tool_result

    data = _subject_detail_raw(rating={"score": 0, "rank": 0, "total": 0, "count": []})
    out = _compress_tool_result(ToolMessage(
        content=json.dumps(data, ensure_ascii=False),
        tool_call_id="t1", name="get_bangumi_subject_detail")).content

    assert "⭐0" not in out and "#0" not in out, f"渲染了「无人评分」的哨兵：\n{out}"


def test_plain_bucket_does_not_depend_on_tool_name() -> None:
    """单体桶认形状不认工具名 —— 换个名字输出逐字节相同。

    这条就是「名单②消失」的验收：旧实现只给 `get_person_detail` /
    `get_character_detail` 两个名字 400 tok，换个名字立刻掉到 300 的截断。
    """
    from agent.memory.short_term import _compress_tool_result

    content = json.dumps(SYNTH_SHAPES["get_person_detail"], ensure_ascii=False)
    outs = {
        name: _compress_tool_result(
            ToolMessage(content=content, tool_call_id="t1", name=name)).content
        for name in ("get_person_detail", "某个还没写出来的工具", "")
    }
    assert len(set(outs.values())) == 1, f"输出随工具名变了：{outs}"


def test_non_json_content_gets_no_tool_specialty() -> None:
    """非 JSON 内容（异常文本）对**所有**工具一视同仁 —— 名单②消失的验收。

    旧实现给 `get_person_detail` / `get_character_detail` 单独保留 400 tok，
    那是本任务要删掉的最后一份硬编码工具名名单。喂 1000 tok 纯文本让差异可见。

    真实可达的非 JSON 内容只有 63–77 tok（`format_tool_error` 的异常文本，
    实测参数校验失败），远低于 300/400 两档 —— 差异在实际链路上够不着，
    但仍然要钉住，免得名单哪天又长回来。
    """
    from agent.memory.short_term import (
        _MAX_SINGLE_MESSAGE_TOKENS,
        _compress_tool_result,
        count_tokens,
    )

    # 内容取真实来源的形状：ToolNode(handle_tool_errors=format_tool_error) 的产出
    content = "工具执行失败（ValidationError）：1 validation error\n" * 60
    outs = {
        name: _compress_tool_result(
            ToolMessage(content=content, tool_call_id="t1", name=name)).content
        for name in ("get_person_detail", "get_character_detail",
                     "get_bangumi_subject_detail", "某个还没写出来的工具", "")
    }
    assert len(set(outs.values())) == 1, (
        f"非 JSON 内容的压缩输出随工具名变了：{ {k: count_tokens(v) for k, v in outs.items()} }"
    )
    assert count_tokens(next(iter(outs.values()))) <= 300, "兜底档不是 300"
    assert count_tokens(next(iter(outs.values()))) <= _MAX_SINGLE_MESSAGE_TOKENS


def test_registered_tools_all_share_the_same_buckets() -> None:
    """16 个注册工具走的是同一套判据 —— 没有任何一个名字有特权。

    表驱动地铺一遍：每个工具各喂三种形状（记录 / 单体 / 非 JSON），
    落桶只由形状决定。加新工具时这张表会自动把它带进来。
    """
    from agent.memory.short_term import _classify, _compress_tool_result

    registered = ("search_bangumi_subject", "search_local_bangumi", "get_calendar",
                  "get_trending_subjects", "get_hot_topics", "get_subject_episodes",
                  "get_subject_characters", "get_entity_comments", "get_episode_comments",
                  "get_bangumi_subject_detail", "get_character_detail", "get_person_detail",
                  "get_subject_opinions", "get_user_profile", "get_user_timeline", "get_blog")

    record = json.dumps(ALL_SHAPES["search_bangumi_subject"], ensure_ascii=False)
    plain = json.dumps(_subject_detail_raw(), ensure_ascii=False)
    raw_text = "工具执行失败（RuntimeError）：连接被重置\n" * 60

    for content, expected in ((record, "record"), (plain, "plain"), (raw_text, None)):
        got = {
            name: _classify(_try_json(content))[0] if _try_json(content) is not None else None
            for name in registered
        }
        assert set(got.values()) == {expected}, f"落桶随工具名变了：{got}"
        outs = {
            name: _compress_tool_result(
                ToolMessage(content=content, tool_call_id="t1", name=name)).content
            for name in registered
        }
        assert len(set(outs.values())) == 1, f"{expected} 形状的输出随工具名变了"


def test_plain_bucket_falls_back_when_scalars_alone_exceed_budget() -> None:
    """标量自己就超预算（真实形状不可能）→ 退回截断，绝不吐空消息。

    CLAUDE.md 规则 1：记忆层不抛异常；本任务的第一原则：不制造新的失败形态。
    """
    from agent.memory.short_term import (
        _MAX_SINGLE_MESSAGE_TOKENS,
        _compress_tool_result,
        count_tokens,
    )

    content = json.dumps({**{f"s{i}": i for i in range(400)}, "name": "X"},
                         ensure_ascii=False)
    out = _compress_tool_result(ToolMessage(
        content=content, tool_call_id="t1", name="get_bangumi_subject_detail")).content

    assert out.strip(), "压出了空消息"
    assert "[已压缩]" in out, "退回截断路径也必须带压缩标记"
    assert count_tokens(out) <= _MAX_SINGLE_MESSAGE_TOKENS


# ═══════════════════════════════════════════════════════════════════
# Step 6：错误提示不许被截断吃掉
# ═══════════════════════════════════════════════════════════════════
# 背景：``get_episode_comments`` 评论拉取失败时返回
# ``{episode(334 tok), comments: [], comment_count: 0, comments_error}`` = 373 tok，
# 兜底从 JSON **头部**切 300 tok → ``comments_error`` 一定在窗口之外被切掉。
# 模型于是看到「这集没什么评论」，而不是「评论没拉到」。
#
# **丢错误比丢数据严重**：丢评论至少有「另有 N 条未列出」的披露，丢错误是让
# agent 不知道自己不知道 —— 它会照着空评论区把话讲圆。


def _episode_comments(comments_error: str = "") -> dict:
    """``get_episode_comments`` 的真实返回（``clients/client.py:185-208``）。

    ``desc`` 给到 sanitizer 的 500 字上限 —— 不然 episode 吃不满预算，
    错误键就切不到，这条测试会变成空转。
    """
    payload: dict = {
        "episode": S.sanitize_episode_detail(
            {"id": 1088, "sort": 10, "name": "第10话 名为『芙莉莲』的魔法",
             "name_cn": "第10话", "airdate": "2024-03-08", "duration": "24m",
             "desc": "芙莉莲一行在旅途中遇到了……" * 12, "comment": 42,
             "subject": {"id": 400602, "name": "葬送のフリーレン"}}),
        "comments": [], "comment_count": 0,
    }
    if comments_error:
        payload["comments_error"] = comments_error
    return payload


def test_error_key_survives_the_fallback() -> None:
    """评论拉取失败时，模型必须知道「没拉到」，而不是以为「没有评论」。"""
    from agent.memory.short_term import _compress_tool_result, count_tokens

    content = json.dumps(_episode_comments("获取评论失败（HTTP 503）"), ensure_ascii=False)
    assert count_tokens(content) > 300, "夹具没超预算 → 这条测试会空转"

    out = _compress_tool_result(ToolMessage(
        content=content, tool_call_id="t1", name="get_episode_comments")).content

    assert "comments_error" in out, "错误键被截断吃掉了"
    assert "HTTP 503" in out, "错误原因没传到下一轮"
    assert out.startswith("[已压缩] "), "错误提示要排在标记之后、正文之前"
    assert count_tokens(out) <= 300, "兜底档没守住 300"


def test_error_notice_leaves_clean_payloads_byte_identical() -> None:
    """没有错误键 → 兜底与改动前**逐字节相同**（安全阀不许被这次改动碰松）。"""
    from agent.memory.short_term import (
        _COMPRESSION_MARKER,
        _compress_tool_result,
        _truncate_text_by_tokens,
        count_tokens,
    )

    # 用**同一个真实形状**、只是不带错误键 —— 变量隔离得干净：
    # 上一条测试与这条唯一的差别就是 `comments_error` 在不在。
    # （ALL_SHAPES 里的封套夹具都是缩水版、都 <300 tok，测不到截断路径。）
    content = json.dumps(_episode_comments(), ensure_ascii=False)
    assert count_tokens(content) > 300, "夹具得真的超预算，否则测不到截断路径"

    out = _compress_tool_result(ToolMessage(
        content=content, tool_call_id="t1", name="get_subject_opinions")).content
    expected = _COMPRESSION_MARKER + _truncate_text_by_tokens(
        content, 300 - count_tokens(_COMPRESSION_MARKER))

    assert out == expected, "没有错误键的 payload 被这次改动碰到了"


def test_error_notice_is_capped_so_the_body_still_fits() -> None:
    """病态长错误（真实形状不可能）也不许把正文挤没、不许越过兜底上限。

    可达的错误串最长约 60 字 / 30 tok（``clients/base.py:178`` 的 404 文案带
    path），所以这条是防御性的：真有工具开始往 ``_error`` 里塞正文，
    提示会被截到 ``_ERROR_KEY_MAX_TOKENS`` 并记 warning，而不是吃掉整个窗口。
    """
    from agent.memory.short_term import (
        _COMPRESSION_MARKER,
        _ERROR_KEY_MAX_TOKENS,
        _compress_tool_result,
        count_tokens,
    )

    content = json.dumps({"body": "正文" * 400, "_error": "错误" * 500},
                         ensure_ascii=False)
    out = _compress_tool_result(ToolMessage(
        content=content, tool_call_id="t1", name="某工具")).content

    assert "_error=" in out, "错误键丢了"
    assert "正文" in out, "正文被错误提示挤没了"
    assert count_tokens(out) <= 300, "越过兜底上限"
    head = out.splitlines()[0]
    assert count_tokens(head) <= _ERROR_KEY_MAX_TOKENS + count_tokens(_COMPRESSION_MARKER) + 2


def test_error_notice_reads_the_suffix_convention() -> None:
    """认后缀，不认名字：``_error`` 与 ``*_error`` 都算；空值、非字符串、深层不算。"""
    from agent.memory.short_term import _error_notice

    assert _error_notice({"_error": "连接超时"}) == "_error=连接超时"
    assert _error_notice({"comments_error": "A", "reviews_error": "B"}) == \
        "comments_error=A | reviews_error=B"
    assert _error_notice({"error": "不是后缀"}) == ""
    assert _error_notice({"comments_error": ""}) == ""
    assert _error_notice({"comments_error": "   "}) == ""
    assert _error_notice({"comments_error": None}) == ""
    assert _error_notice({"comments_error": []}) == ""
    assert _error_notice({"nested": {"comments_error": "深层不算"}}) == ""
    assert _error_notice("不是 dict") == ""
    assert _error_notice(None) == ""


def test_error_carrying_shapes_all_land_in_the_fallback() -> None:
    """★ 前提 pin：带 ``*_error`` 键的真实形状**全部**落兜底，一个都不进桶。

    ``_error_notice`` 只装在 ``_compress_by_truncation`` 上 —— 这条断言就是它的
    适用前提。五个顶层错误键产生点（``clients/client.py`` 的 198 / 238 / 252 /
    347 / 446 行）对应的形状都在下面。

    **将来谁放松了判据（比如给多部件信封加机制），这条会先红**：那时错误提示
    必须跟着搬到新路径上，否则会悄悄退回到「agent 不知道自己不知道」。
    本文件里的 ``get_blog`` 夹具是真实形状（5 个标量的 ``blog``），
    所以「blog 也不会进桶」这个结论是真的 —— 换成假夹具这条就会失灵。
    """
    from agent.memory.short_term import _classify, _try_json

    carriers = {
        # client.py:198（episode 元数据拿到了，评论失败）
        "episode_comments(评论失败)": {"episode": {"id": 1088}, "comments": [],
                                       "comment_count": 0,
                                       "comments_error": "获取评论失败（HTTP 503）"},
        # client.py:446
        "blog(blog 失败)": {"entry_id": 1, "blog_error": "请求超时",
                            "comments": ["评论一"], "subjects": [{"id": 8, "name": "EVA"}]},
        "blog(comments 失败)": {"entry_id": 1, "blog": _BLOG_REAL,
                                "comments_error": "获取评论失败",
                                "subjects": [{"id": 8, "name": "EVA"}]},
        "blog(全失败)": {"entry_id": 1, "blog_error": "请求超时",
                         "comments_error": "获取评论失败",
                         "subjects_error": "连接失败（ConnectError）"},
        # client.py:238 / 252
        "opinions(评论失败)": {"subject_id": 265, "comments_error": "获取评论失败",
                               "reviews": {"items": [{"id": 1}], "total": 1}},
        # client.py:347
        "profile(user 失败)": {"username": "alice", "user_error": "认证失败",
                               "user_stats": {"anime": {"collect": 100}},
                               "collections": {"items": [{"id": 1}], "total": 1},
                               "characters": [{"id": 1, "name": "角色"}],
                               "persons": [{"id": 2, "name": "人物"}],
                               "blogs": [{"id": 3, "title": "日志"}]},
    }
    for label, shape in carriers.items():
        data = _try_json(json.dumps(shape, ensure_ascii=False))
        assert data is not None, f"{label} 的夹具不是 JSON dict"
        assert _classify(data)[0] is None, (
            f"{label} 进了「{_classify(data)[0]}」桶 —— 兜底那行错误提示就不生效了，"
            "得把 _error_notice 搬到这条新路径上"
        )
