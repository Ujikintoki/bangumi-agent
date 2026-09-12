"""
RAG 评测查询定义

两类查询：
  - AUTO_QUERY_DEFS: 自动 Ground Truth（从数据库元数据生成，Recall 精确）
  - POOLED_QUERIES:  池化人工标注查询（语义查询，无客观 GT）
"""

from __future__ import annotations

from typing import Optional

# ═══════════════════════════════════════════════════════════════════════
# 1. 自动 Ground Truth — 从数据库元数据生成
# ═══════════════════════════════════════════════════════════════════════

# 每条定义：(query_id, query_text, entity_type, subject_type, category, GT_SQL)
# subject_type: 1=书籍, 2=动画, None=不适用(character/person)
# GT_SQL 返回 id 列表，这些 id = 该 query 的完整正确答案
AUTO_QUERY_DEFS: list[tuple[str, str, str, Optional[int], str, str]] = [
    # ── 标签匹配：搜标签名 → GT = 所有带该标签的 subject ──
    ("a01", "芳文社", "subject", 2, "tag",
     """SELECT id FROM rag_entities WHERE entity_type='subject'
        AND meta_info->'tags' @> '[{"name": "芳文社"}]'"""),
    ("a02", "CloverWorks", "subject", 2, "tag",
     """SELECT id FROM rag_entities WHERE entity_type='subject'
        AND meta_info->'tags' @> '[{"name": "CloverWorks"}]'"""),
    ("a03", "京都动画", "subject", 2, "tag",
     """SELECT id FROM rag_entities WHERE entity_type='subject'
        AND meta_info->'tags' @> '[{"name": "京都动画"}]'"""),
    ("a04", "ufotable", "subject", 2, "tag",
     """SELECT id FROM rag_entities WHERE entity_type='subject'
        AND meta_info->'tags' @> '[{"name": "ufotable"}]'"""),
    ("a05", "Production.I.G", "subject", 2, "tag",
     """SELECT id FROM rag_entities WHERE entity_type='subject'
        AND meta_info->'tags' @> '[{"name": "Production.I.G"}]'"""),
    ("a06", "TRIGGER", "subject", 2, "tag",
     """SELECT id FROM rag_entities WHERE entity_type='subject'
        AND meta_info->'tags' @> '[{"name": "TRIGGER"}]'"""),
    ("a07", "动画工房", "subject", 2, "tag",
     """SELECT id FROM rag_entities WHERE entity_type='subject'
        AND meta_info->'tags' @> '[{"name": "动画工房"}]'"""),
    ("a08", "虚渊玄", "subject", 2, "tag",
     """SELECT id FROM rag_entities WHERE entity_type='subject'
        AND meta_info->'tags' @> '[{"name": "虚渊玄"}]'"""),

    # ── 更多标签 ──
    ("a09", "MADHouse", "subject", 2, "tag",
     """SELECT id FROM rag_entities WHERE entity_type='subject'
        AND meta_info->'tags' @> '[{"name": "MADHouse"}]'"""),
    ("a10", "BONES", "subject", 2, "tag",
     """SELECT id FROM rag_entities WHERE entity_type='subject'
        AND meta_info->'tags' @> '[{"name": "BONES"}]'"""),
    ("a11", "P.A.WORKS", "subject", 2, "tag",
     """SELECT id FROM rag_entities WHERE entity_type='subject'
        AND meta_info->'tags' @> '[{"name": "P.A.WORKS"}]'"""),
    ("a12", "吉卜力", "subject", 2, "tag",
     """SELECT id FROM rag_entities WHERE entity_type='subject'
        AND meta_info->'tags' @> '[{"name": "吉卜力"}]'"""),

    # ── 年份 ──
    ("a13", "2023年的动画", "subject", 2, "year",
     """SELECT id FROM rag_entities WHERE entity_type='subject'
        AND meta_info->>'year' = '2023'"""),
    ("a14", "2022年的动画", "subject", 2, "year",
     """SELECT id FROM rag_entities WHERE entity_type='subject'
        AND meta_info->>'year' = '2022'"""),

    # ── 评分 ──
    ("a15", "高分神作", "subject", 2, "score",
     """SELECT id FROM rag_entities WHERE entity_type='subject'
        AND (meta_info->>'score')::float >= 8.8"""),

    # ── 声优/创作者 ──
    ("a16", "声优", "person", None, "career",
     """SELECT id FROM rag_entities WHERE entity_type='person'
        AND meta_info->'career' ? 'seiyu'"""),
    ("a17", "歌手艺人", "person", None, "career",
     """SELECT id FROM rag_entities WHERE entity_type='person'
        AND meta_info->'career' ? 'artist'"""),
    ("a18", "动画制作人", "person", None, "career",
     """SELECT id FROM rag_entities WHERE entity_type='person'
        AND meta_info->'career' ? 'producer'"""),

    # ── 精确名称（人工指定 target ID）──
    ("a19", "孤独摇滚", "subject", 2, "exact",
     "subject_328609"),
    ("a20", "進撃の巨人", "subject", 2, "exact",
     "subject_55770"),
    ("a21", "命运石之门", "subject", 2, "exact",
     "subject_10380"),
    ("a22", "化物語", "subject", 2, "exact",
     "subject_1671"),
    ("a23", "CLANNAD", "subject", 2, "exact",
     "subject_51"),
    ("a24", "牧瀬紅莉栖", "character", None, "exact",
     "character_12393"),
    ("a25", "花澤香菜", "person", None, "exact",
     "person_4765"),
]

# ═══════════════════════════════════════════════════════════════════════
# 2. 池化人工标注查询 — 语义查询，无客观 GT，需人工判 relevance
# ═══════════════════════════════════════════════════════════════════════

POOLED_QUERIES: list[dict] = [
    {"id": "p01", "query": "关于音乐乐队的百合动画",
     "entity_type": "subject", "subject_type": 2, "category": "semantic",
     "desc": "语义 — 三个概念组合"},
    {"id": "p02", "query": "异世界转生冒险",
     "entity_type": "subject", "subject_type": 2, "category": "semantic",
     "desc": "语义 — 奇幻子类型"},
    {"id": "p03", "query": "机甲战斗科幻",
     "entity_type": "subject", "subject_type": 2, "category": "semantic",
     "desc": "语义 — 科幻子类型"},
    {"id": "p04", "query": "悬疑推理惊悚",
     "entity_type": "subject", "subject_type": 2, "category": "semantic",
     "desc": "语义 — 类型组合"},
    {"id": "p05", "query": "温馨轻松的日常故事",
     "entity_type": "subject", "subject_type": 2, "category": "semantic",
     "desc": "语义 — 氛围/节奏"},
    {"id": "p06", "query": "制作精良的剧场版动画电影",
     "entity_type": "subject", "subject_type": 2, "category": "semantic",
     "desc": "语义 — 品质+类型"},
    {"id": "p07", "query": "傲娇双马尾美少女",
     "entity_type": "character", "subject_type": None, "category": "semantic",
     "desc": "角色 — 属性组合"},
    {"id": "p08", "query": "帅气冷酷的男性角色",
     "entity_type": "character", "subject_type": None, "category": "semantic",
     "desc": "角色 — 性格描述"},
    {"id": "p09", "query": "知名动画导演",
     "entity_type": "person", "subject_type": None, "category": "semantic",
     "desc": "人物 — 职位+知名度"},
    {"id": "p10", "query": "配乐出色的作曲家",
     "entity_type": "person", "subject_type": None, "category": "semantic",
     "desc": "人物 — 职业+品质"},
]
