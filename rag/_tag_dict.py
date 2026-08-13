"""
标签词表加载器 — 用于 RAG 搜索工具的关键词规则兜底提取。

维护原则:
  - 词表从 DB 实时加载（lazy + cache），不手工维护
  - 仅在 LLM 未提取到结构化参数时作为兜底
  - 子串匹配 + 长度 ≥2 + 上限 5 个防误触发

用法::

    from rag._tag_dict import load_tag_vocabulary
    tags = load_tag_vocabulary()
    matched = [t for t in tags if t in user_query][:5]
"""

from __future__ import annotations

from functools import lru_cache


@lru_cache(maxsize=1)
def load_tag_vocabulary() -> frozenset[str]:
    """从 rag_entities 表加载所有已知标签名，进程级缓存。

    仅在 subject 实体上查询（character/person 无 tags 字段）。
    首次调用时执行 DB 查询，后续命中 lru_cache。

    Returns:
        所有标签名的 frozenset，可用于子串匹配。
    """
    from database.engine import engine
    from sqlmodel import Session, text

    sql = (
        "SELECT DISTINCT jsonb_array_elements(meta_info->'tags')->>'name' AS tag "
        "FROM rag_entities "
        "WHERE entity_type = 'subject' AND meta_info->'tags' IS NOT NULL"
    )
    try:
        with Session(engine) as session:
            rows = session.exec(text(sql)).all()
    except Exception:
        return frozenset()

    return frozenset(t for (t,) in rows if t and isinstance(t, str))
