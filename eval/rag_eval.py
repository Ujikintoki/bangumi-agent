"""
RAG 检索评测管线 v2

三步工作流::

    # 1. 我运行：生成自动 Ground Truth + 池化人工标注模板
    python -m eval.rag_eval --build

    # 2. 你标注：打开 eval/data/rag_gt/pooled_annotate.json，标记每个候选实体是否相关
    #    格式：将 "relevant": null 改为 true 或 false

    # 3. 我运行：计算全部指标 + 报告
    python -m eval.rag_eval --evaluate

指标::

    Recall@K      K 个结果中找到了多少个 GT 实体 / GT 实体总数
    Precision@K   K 个结果中多少个在 GT 中 / K
    MRR           第一个 GT 实体排名的倒数（1/rank），越早越接近 1.0
    NDCG@K        带位置权重的相关度得分，排序越好越高
    Hit Rate@K    至少找到 1 个 GT 实体的 query 比例

评测维度::

    - 分组：A 自测组（期望恒 1.0，回归断言）/ B 质量组（有信息量）/
            C 人工组（池化偏差，偏高）
    - 按 category：tag / year / score / career / exact / semantic
    - 按检索通道：keyword / name / vector 各自【单独】能找到多少

⚠ 不要读「全局」那个数。它把三个可信度完全不同的组平均在一起，
  其中 A 组是构造出来的恒 1.0，会把整体拉高并稀释掉真正的差异。
  分组的定义见 SELF_TEST_CATEGORIES 的注释。
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import statistics
import time
import unicodedata
from pathlib import Path
from typing import Optional

logging.basicConfig(level=logging.WARNING, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("eval")

EVAL_DIR = Path(__file__).resolve().parent
ARTIFACTS_DIR = EVAL_DIR / "data" / "rag_gt"
AUTO_GT_FILE = ARTIFACTS_DIR / "ground_truth_auto.json"
POOLED_FILE = ARTIFACTS_DIR / "pooled_annotate.json"
MERGED_GT_FILE = ARTIFACTS_DIR / "ground_truth_merged.json"

K_VALUES = [1, 3, 5, 10]

# 检索通道 —— 报告按通道分层。融合后的单一数字会掩盖「某个通道一条都没找到」，
# 而那恰恰是最需要看见的架构结论（见下面 SELF_TEST_CATEGORIES）。
CHANNELS = ("keyword", "name", "vector")

# ── 生产检索配置：唯一事实来源 ──
#
# 必须与 tools/bgm_tools.py:search_local_bangumi 一致 —— 生产【不传】任何消融开关，
# 用的是 rag/retriever.py:hybrid_search 的默认值。所以这三个值就是 hybrid_search
# 的签名默认值，改了生产参数要回来同步这里。
#
# 为什么要有这个常量：--evaluate 与 --ablate 都从它派生，两边口径不会再漂。
# 此前 --ablate 的基准写成「全开」（enable_bucketing=True），而生产默认是 False ——
# 等于拿一个线上不存在的配置当基准，所有 delta 都偏（实测 0.4103 vs 0.4172）。
PRODUCTION_RETRIEVAL: dict[str, bool] = {
    "enable_threshold": True,
    "enable_bucketing": False,   # ← 生产默认【关】。「全开」是虚构配置
    "enable_mmr": True,
}

# 生产距离阈值（hybrid_search 默认 0.65）。评测曾用 0.9，与生产不一致。
# 实测差异 ≈ 0（向量结果本就都落在 0.65 内），但口径必须一致才有意义。
PRODUCTION_DISTANCE_THRESHOLD = 0.65

# 池化模板【不】用生产阈值：池化要的是「尽量多捞候选」以减轻池化偏差（§3.2），
# 用更宽松的 0.9 是有意的；而测量必须复现生产，用 0.65。两者目的不同，别统一。
POOLING_DISTANCE_THRESHOLD = 0.9

# 自测组（self-test）：GT 的产生条件与检索器的 WHERE 条件【同源】。
#
#   a01「芳文社」  GT: tags @> '[{"name":"芳文社"}]'
#                  检索: tags @> '[{"name":"芳文社"}]'   ← 同一个条件
#
# 问的人和答的人是同一个 → 融合结果理论上恒为 1.0，方差为零。
# 它不「偏高」，它【零信息量】—— 测不出任何东西。
#
# 正确用途是【回归断言】：基线恒 1.0，任何下跌都说明 keyword 通道坏了。
# 错误的用途是当质量指标：它永远好看，且掩盖其他通道的失败。
#
# exact 类别不在其中：GT 是人工指定的实体 id，检索器仍须靠名字找出来，
# 两者独立 → 是真实测量。
SELF_TEST_CATEGORIES = frozenset({"tag", "year", "score", "career"})

# 分组标签 → 报告里的一句话说明
GROUP_NOTES = {
    "A": "自测组 — GT 与检索同源：返回的每一条必然在 GT 里 → P/NDCG/MRR 恒 1.0；"
         "R@5 偏低是 |GT|≫K 的天花板压的。四个数【都没有信息量】",
    "B": "质量组 — GT 是人工指定的实体 id，检索器须靠名字独立找到 → 有信息量 ★",
    "C": "人工组 — 池化偏差（候选池由被测系统产出，漏掉的不会被算作 miss）→ 偏高，仅定性",
}

# ═══════════════════════════════════════════════════════════════════════
# 查询定义（从 queries.py 导入）
# ═══════════════════════════════════════════════════════════════════════

from eval.metrics import ndcg_at_k  # noqa: E402
from eval.rag_queries import AUTO_QUERY_DEFS, POOLED_QUERIES  # noqa: E402

# ═══════════════════════════════════════════════════════════════════════
# 基础设施
# ═══════════════════════════════════════════════════════════════════════

_retriever = None


def _get_retriever():
    global _retriever
    if _retriever is None:
        from core.config import get_settings
        from database.engine import engine
        from rag.retriever import RagEntityRetriever

        s = get_settings()
        _retriever = RagEntityRetriever(
            engine=engine,
            zhipu_api_key=s.ZHIPU_API_KEY,
            zhipu_base_url=s.ZHIPU_BASE_URL,
        )
    return _retriever


def _query_db(sql: str) -> list[str]:
    """执行 SQL 返回 id 列表。"""
    from database.engine import engine
    from sqlmodel import Session, text

    with Session(engine) as s:
        rows = s.exec(text(sql)).all()
    return [row[0] for row in rows]


def _parse_keyword_filters(sql: str) -> dict:
    """从 GT SQL 中提取关键词匹配参数，零手工维护。

    支持的 SQL 模式::
        meta_info->'tags' @> '[{"name": "XXX"}]'  →  required_tags: ["XXX"]
        meta_info->>'year' = 'YYYY'               →  year: YYYY
        (meta_info->>'score')::float >= X.X       →  min_score: X.X
        meta_info->'career' ? 'XXX'               →  career: "XXX"
    """
    filters: dict = {}
    tag_match = re.findall(r"""@>\s*'\[\{"name":\s*"([^"]+)"\}\]'""", sql)
    if tag_match:
        filters["required_tags"] = tag_match
    year_match = re.search(r"->>'year'\s*=\s*'(\d{4})'", sql)
    if year_match:
        filters["year"] = int(year_match.group(1))
    score_match = re.search(r"::float\s*>=\s*([\d.]+)", sql)
    if score_match:
        filters["min_score"] = float(score_match.group(1))
    career_match = re.search(r"\?\s*'(\w+)'", sql)
    if career_match:
        filters["career"] = career_match.group(1)
    return filters


def _run_keyword_search(
    entity_type: str,
    subject_type: Optional[int],
    required_tags: Optional[list[str]] = None,
    year: Optional[int] = None,
    min_score: Optional[float] = None,
    career: Optional[str] = None,
    limit: int = 10,
) -> list[str]:
    """精确关键词匹配检索 — 用结构化 WHERE 条件而非向量。"""
    from database.engine import engine
    from database.rag_tables import RagEntity
    from sqlmodel import Session, select, desc

    if not any([required_tags, year is not None, min_score is not None, career]):
        return []

    with Session(engine) as session:
        stmt = select(RagEntity.id, RagEntity.popularity)
        if entity_type and entity_type != "all":
            stmt = stmt.where(RagEntity.entity_type == entity_type)
        if subject_type is not None:
            stmt = stmt.where(RagEntity.subject_type == subject_type)
        stmt = stmt.where(RagEntity.nsfw == False)

        if required_tags:
            for tag_name in required_tags:
                stmt = stmt.where(
                    RagEntity.meta_info["tags"].contains([{"name": tag_name}])
                )
        if year is not None:
            stmt = stmt.where(
                RagEntity.meta_info["year"].as_string() == str(year)
            )
        if min_score is not None:
            from sqlalchemy import cast, Float
            stmt = stmt.where(
                cast(RagEntity.meta_info["score"].as_string(), Float) >= min_score
            )
        if career:
            stmt = stmt.where(RagEntity.meta_info["career"].has_key(career))

        stmt = stmt.order_by(desc(RagEntity.popularity)).limit(limit)
        rows = session.execute(stmt).all()
    return [row[0] for row in rows]


# ═══════════════════════════════════════════════════════════════════════
# Phase 1: --build — 生成 Ground Truth
# ═══════════════════════════════════════════════════════════════════════


def build_auto_gt() -> list[dict]:
    """执行 SQL 生成自动 Ground Truth。"""
    queries = []
    for qid, qtext, etype, stype, cat, sql in AUTO_QUERY_DEFS:
        print(f"  [{qid}] \"{qtext}\"", end=" ... ")

        if cat == "exact":
            # 精确名称：GT 就是这一个 id
            gt = [sql] if sql else []
        else:
            # 注入 subject_type + nsfw 过滤：SQL 里只写 entity_type 过滤，
            # 这两个由这里自动追加，保证 GT 与检索范围 [完全一致]。
            #
            # nsfw 不能漏：_run_keyword_search 有 `nsfw == False`，检索器被
            # 禁止返回 nsfw 条目。若 GT 不排除它们，分母里就含永远拿不到的
            # 条目，Recall 从理论上就打不满（实测 a18 有 10/58 条是 nsfw）。
            filtered_sql = sql
            if stype is not None:
                filtered_sql = f"{sql} AND subject_type={stype}"
            filtered_sql = f"{filtered_sql} AND nsfw = false"
            gt = _query_db(filtered_sql)

        queries.append({
            "id": qid,
            "query": qtext,
            "entity_type": etype,
            "subject_type": stype,
            "category": cat,
            "ground_truth": gt,
            "gt_size": len(gt),
            "keyword_filters": _parse_keyword_filters(sql),
        })
        print(f"GT={len(gt)}")

    return queries


def build_pooled_template(existing_auto_queries: list[dict]) -> list[dict]:
    """对每条语义查询池化 top-20 结果，生成人工标注模板。

    池化策略：两路检索取并集（entity_type=all + entity_type=specific），
    去重后取 top-20，附带关键信息方便人工判 relevance。
    """
    retriever = _get_retriever()
    pooled = []

    for pq in POOLED_QUERIES:
        qid = pq["id"]
        qtext = pq["query"]
        etype = pq["entity_type"]
        print(f"  [{qid}] \"{qtext}\"", end=" ... ")

        # 两路池化
        seen: set[str] = set()
        candidates: list[dict] = []

        stype = pq.get("subject_type")
        for search_type in ["all", etype]:
            try:
                results = retriever.hybrid_search(
                    query=qtext,
                    entity_type=search_type,
                    subject_type=stype if search_type != "all" else None,
                    limit=15,
                    distance_threshold=POOLING_DISTANCE_THRESHOLD,
                )
            except Exception:
                continue

            for r in results:
                if r.entity_id not in seen:
                    seen.add(r.entity_id)
                    # 从 rag_output 提取 summary snippet
                    snippet = ""
                    try:
                        ro = json.loads(r.rag_output)
                        snippet = (ro.get("summary") or "")[:120]
                    except (json.JSONDecodeError, TypeError):
                        pass

                    candidates.append({
                        "entity_id": r.entity_id,
                        "name": r.name,
                        "name_cn": r.name_cn or "",
                        "entity_type": r.entity_type,
                        "subject_type": r.subject_type,
                        "snippet": snippet,
                        "relevant": None,  # 待标注
                    })

        # 去重 + 截断
        candidates = candidates[:20]
        pooled.append({
            "id": qid,
            "query": qtext,
            "entity_type": etype,
            "category": pq["category"],
            "description": pq["desc"],
            "candidates": candidates,
        })
        print(f"{len(candidates)} candidates")

    return pooled


def _count_annotated_pooled() -> int:
    """池化文件里已有多少人工作答（relevant 非 null）。"""
    if not POOLED_FILE.exists():
        return 0
    try:
        data = json.loads(POOLED_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return 0
    return sum(
        1 for q in data.get("queries", [])
        for c in q.get("candidates", [])
        if c.get("relevant") is not None
    )


def cmd_build():
    """--build: 生成 auto GT + 池化模板。"""
    print("=" * 60)
    print("  Phase 1: 自动 Ground Truth")
    print("=" * 60)
    auto_queries = build_auto_gt()

    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    with open(AUTO_GT_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "description": "自动生成的 Ground Truth。GT 来自数据库元数据（tag/infobox/career/score），Recall 精确。",
            "queries": auto_queries,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n  ✓ {len(auto_queries)} 条自动 GT → {AUTO_GT_FILE.name}")

    # 统计
    categories = {}
    for q in auto_queries:
        c = q["category"]
        categories[c] = categories.get(c, 0) + 1
    for c, n in sorted(categories.items()):
        print(f"    {c}: {n}")

    print()
    print("=" * 60)
    print("  Phase 2: 池化人工标注模板")
    print("=" * 60)

    # 防呆：重建会把全部 relevant 重置为 null，人工标注不可恢复。
    # 想重建就先删/改名该文件 —— 显式动作，不靠"跑个命令顺手"。
    n_annotated = _count_annotated_pooled()
    if n_annotated:
        print(f"  ⚠ 跳过重建 —— {POOLED_FILE.name} 已有 {n_annotated} 个人工作答。")
        print(f"    重建会把 relevant 全部重置为 null（标注不可恢复）。")
        print(f"    确实要重建：先删除或重命名 {POOLED_FILE.name} 再跑 --build。")
        total_candidates = sum(
            len(q.get("candidates", []))
            for q in json.loads(POOLED_FILE.read_text(encoding="utf-8")).get("queries", [])
        )
        print(f"    现有 {total_candidates} 个候选，保持原样。")
    else:
        pooled = build_pooled_template(auto_queries)

        with open(POOLED_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "description": (
                    "池化语义查询 — 需要人工标注 relevance。\n"
                    "每条 query 下有 ~20 个候选实体（带名称+简介片段）。\n"
                    "请将每个 candidate 的 relevant 从 null 改为 true（应该出现在搜索结果中）或 false（不应该）。\n"
                    "标注完成后运行: python -m eval.rag_eval --evaluate"
                ),
                "queries": pooled,
            }, f, ensure_ascii=False, indent=2)
        print(f"\n  ✓ {len(pooled)} 条池化查询 → {POOLED_FILE.name}")
        total_candidates = sum(len(q["candidates"]) for q in pooled)
        print(f"    共 {total_candidates} 个候选实体待标注")

    print()
    print("=" * 60)
    print("  下一步")
    print("=" * 60)
    print(f"  1. 打开 {POOLED_FILE.relative_to(Path.cwd()) if POOLED_FILE.is_relative_to(Path.cwd()) else POOLED_FILE}")
    print(f"  2. 对每个 query 的 candidates，改 relevant: null → true/false")
    print(f"  3. 运行 python -m eval.rag_eval --evaluate")
    print()


# ═══════════════════════════════════════════════════════════════════════
# Phase 2: --evaluate — 合并 GT + 检索 + 算指标
# ═══════════════════════════════════════════════════════════════════════


def _load_annotated_pooled() -> list[dict]:
    """加载人工标注的池化查询，提取 GT。"""
    if not POOLED_FILE.exists():
        print(f"  ⚠ {POOLED_FILE.name} 不存在，跳过池化查询")
        return []

    with open(POOLED_FILE, encoding="utf-8") as f:
        data = json.load(f)

    queries = []
    unannotated = 0
    for pq in data.get("queries", []):
        gt = []
        for c in pq.get("candidates", []):
            if c.get("relevant") is True:
                gt.append(c["entity_id"])
            elif c.get("relevant") is None:
                unannotated += 1

        queries.append({
            "id": pq["id"],
            "query": pq["query"],
            "entity_type": pq["entity_type"],
            "subject_type": pq.get("subject_type"),
            "category": pq.get("category", "semantic"),
            "source": "human",
            "ground_truth": gt,
            "gt_size": len(gt),
        })

    if unannotated > 0:
        print(f"  ⚠ {unannotated} 个候选实体未标注（relevant=null），已视为不相关")

    return queries


def _run_retrieval(
    query: str,
    entity_type: str,
    subject_type: Optional[int] = None,
    limit: int = 10,
    keyword_filters: Optional[dict] = None,
    **ablation_kwargs: bool,
) -> dict:
    """三通道检索：关键词精确 > trigram 名称 > 向量语义补位。

    ablation_kwargs 透传给 hybrid_search 的消融开关。

    返回::

        {
          "merged":  [...],   # 融合后（keyword > name > vector 去重截断）
          "keyword": [...],   # 各通道【原始输出】，各自 limit 截断
          "name":    [...],
          "vector":  [...],
          "fusion":  {"keyword": n, "name": n, "vector": n},  # 融合结果里各通道占几条
        }

    【为什么保留原始输出】原先只返回 merged，导致「某通道贡献 0」在报告里
    完全不可见 —— a01「芳文社」融合后是 10/10 满分，但那 10 条全来自 keyword，
    name 和 vector 一条没找到。只看融合数字，会以为三条通道都在工作。
    各通道都用同一个 limit 截断，所以可以同口径比较。
    """
    # 通道 1: 关键词精确匹配
    keyword_ids: list[str] = []
    if keyword_filters:
        keyword_ids = _run_keyword_search(
            entity_type=entity_type, subject_type=subject_type,
            limit=limit, **keyword_filters,
        )
    keyword_seen = set(keyword_ids)

    retriever = _get_retriever()

    # 通道 2: trigram 名称匹配
    name_ids: list[str] = []
    try:
        name_results = retriever.name_search(
            query=query, entity_type=entity_type,
            subject_type=subject_type, limit=limit,
        )
        name_ids = [r.entity_id for r in name_results]
    except Exception:
        pass
    name_seen = set(name_ids) | keyword_seen

    # 通道 3: 向量语义匹配 —— 用生产阈值，测的就是线上会返回什么
    try:
        results = retriever.hybrid_search(
            query=query, entity_type=entity_type,
            subject_type=subject_type, limit=limit,
            distance_threshold=PRODUCTION_DISTANCE_THRESHOLD, **ablation_kwargs,
        )
        vector_ids = [r.entity_id for r in results]
    except Exception as e:
        logger.error("检索失败 %s: %s", query, e)
        vector_ids = []

    # 合并: 关键词 > 名称 > 向量 去重
    merged = list(keyword_ids)
    for nid in name_ids:
        if nid not in keyword_seen:
            merged.append(nid)
    for vid in vector_ids:
        if vid not in keyword_seen and vid not in name_seen:
            merged.append(vid)
    merged = merged[:limit]

    # 融合结果里各通道各占几条 —— 回答"这个分数是谁挣来的"
    fusion = {"keyword": 0, "name": 0, "vector": 0}
    for eid in merged:
        if eid in keyword_seen:
            fusion["keyword"] += 1
        elif eid in name_seen:      # name_seen 含 keyword_seen，故此处已是 name 独有
            fusion["name"] += 1
        else:
            fusion["vector"] += 1

    return {
        "merged": merged,
        "keyword": keyword_ids[:limit],
        "name": name_ids[:limit],
        "vector": vector_ids[:limit],
        "fusion": fusion,
    }


def _compute_metrics(
    retrieved_ids: list[str],
    gt_ids: set[str],
) -> dict[str, float]:
    """对单条 query 的检索结果计算全部指标。"""
    scores: dict[str, float] = {}
    gt_set = set(gt_ids)

    if not gt_set:
        return scores

    for k in K_VALUES:
        top_k = retrieved_ids[:k]

        # Recall@K
        found = sum(1 for eid in top_k if eid in gt_set)
        scores[f"Recall@{k}"] = found / len(gt_set)

        # Precision@K
        scores[f"Precision@{k}"] = found / k if k > 0 else 0.0

        # Hit Rate@K
        scores[f"Hit@{k}"] = 1.0 if found > 0 else 0.0

    # MRR (使用全部检索结果，不限于 K)
    for i, eid in enumerate(retrieved_ids):
        if eid in gt_set:
            scores["MRR"] = 1.0 / (i + 1)
            break
    else:
        scores["MRR"] = 0.0

    # NDCG@K —— 二值相关度 {id: 1}，IDCG 由 eval.metrics 从【全集 GT】推导，
    # 而不是本次返回结果的降序重排；零命中返回 0.0 而非 1.0。
    #
    # 注意：二值相关度 + |GT| > k 时，NDCG@k 与 Precision@k 单调相关，
    # 信息量有限。它现在的作用是"不再算错"，不是"变敏锐了"。
    graded_gt = {eid: 1 for eid in gt_set}
    for k in K_VALUES:
        scores[f"NDCG@{k}"] = ndcg_at_k(retrieved_ids, graded_gt, k)

    return scores


def _split_by_gt(queries: list[dict]) -> tuple[list[dict], list[dict]]:
    """按 GT 是否为空分离查询：可参与指标计算的 vs 必须报告的。

    GT 为空的查询不能算指标（分母为 0），但 [不能静默丢弃] —— 它本身
    就是"检索完全跑偏"的证据。典型：p07 的 20 个候选全部被判为不相关，
    旧版 `continue` 让这条从报告里彻底消失，35 条查询只出了 34 条结果。
    """
    usable, dropped = [], []
    for q in queries:
        if q.get("ground_truth"):
            usable.append(q)
        else:
            dropped.append({
                "id": q.get("id"),
                "query": q.get("query"),
                "reason": "GT 为空（候选无一被判为相关）",
            })
    return usable, dropped


def _pad(s: str, width: int, align: str = "<") -> str:
    """按【显示宽度】补齐 —— CJK 占 2 列而 len() 只算 1，直接格式化必然错位。

    query 列混了「芳文社」和「CloverWorks」，不用这个函数表格会参差不齐。
    """
    w = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)
    fill = " " * max(0, width - w)
    return fill + s if align == ">" else s + fill


def _group_of(q: dict) -> str:
    """把查询归入三个可信度不同的组 —— 报告的分层依据。

    A 自测组：GT 与检索同源（见 SELF_TEST_CATEGORIES 注释）→ 恒 1.0，回归断言
    B 质量组：GT 由人工指定 id，检索器须独立找到 → 真数字
    C 人工组：人工判的 GT，但候选池是被测系统产出的 → 池化偏差，偏高
    """
    if q.get("source") == "human":
        return "C"
    if q.get("category") in SELF_TEST_CATEGORIES:
        return "A"
    return "B"


def _mean_metrics(rs: list[dict], keys: tuple[str, ...] = ("Recall@5", "Precision@5", "NDCG@5", "MRR")) -> dict:
    """一组结果的指标均值 —— 会自动跳过缺该指标的条目。

    GT 为空的查询 _compute_metrics 返回 {}，若不过滤会算进分母拉低均值。
    """
    out = {}
    for m in keys:
        arr = [r["metrics"][m] for r in rs if m in r["metrics"]]
        if arr:
            out[m] = statistics.mean(arr)
    return out


def cmd_evaluate():
    """--evaluate: 合并 GT + 检索 + 算指标。"""
    # ── 加载 GT ──
    if not AUTO_GT_FILE.exists():
        print(f"错误: {AUTO_GT_FILE.name} 不存在，请先运行 --build")
        return

    with open(AUTO_GT_FILE, encoding="utf-8") as f:
        auto_data = json.load(f)
    auto_queries = auto_data.get("queries", [])
    print(f"自动 GT: {len(auto_queries)} 条")

    human_queries = _load_annotated_pooled()
    print(f"池化标注: {len(human_queries)} 条")

    all_queries = auto_queries + human_queries
    usable_queries, dropped = _split_by_gt(all_queries)
    print(f"合计: {len(all_queries)} 条（其中 {len(dropped)} 条 GT 为空，见报告末尾）\n")

    # ── 检索 + 算指标 ──
    results: list[dict] = []
    for i, q in enumerate(usable_queries):
        qid = q["id"]
        qtext = q["query"]
        etype = q.get("entity_type", "all")
        gt = set(q.get("ground_truth", []))

        print(f"  [{i+1:2d}/{len(usable_queries)}] {qid} \"{qtext}\" GT={len(gt)}", end=" ... ")
        retrieved = _run_retrieval(
            qtext, etype, q.get("subject_type"),
            keyword_filters=q.get("keyword_filters"),
        )
        metrics = _compute_metrics(retrieved["merged"], gt)
        # 各通道【单独】再算一遍 —— 纯函数，零额外成本。
        # 融合数字回答不了"这条通道自己找得到吗"：keyword 一满员，
        # name/vector 就被挤出 merged，看数字像是它们不存在。
        channel_metrics = {
            ch: _compute_metrics(retrieved[ch], gt) for ch in CHANNELS
        }
        print(f"{len(retrieved['merged'])} hits, Recall@5={metrics.get('Recall@5', 0):.3f}")

        results.append({
            "query": q,
            "retrieved": retrieved,
            "metrics": metrics,
            "channel_metrics": channel_metrics,
        })
        time.sleep(0.05)  # 轻微限流

    # ── 聚合 ──
    all_metrics: dict[str, list[float]] = {}
    for r in results:
        for key, val in r["metrics"].items():
            all_metrics.setdefault(key, []).append(val)

    # ── 报告 ──
    print()
    print("=" * 78)
    print("  RAG 检索评测报告")
    print("=" * 78)
    print(f"  有效 query 数: {len(results)}")
    print(f"  GT 来源: auto={len(auto_queries)}, human={len(human_queries)}")
    print()

    # ── 分组（放在全局之前：全局均值在这里是误导性的，见 GROUP_NOTES）──
    groups: dict[str, list[dict]] = {"A": [], "B": [], "C": []}
    for r in results:
        groups[_group_of(r["query"])].append(r)

    order = ("Recall@5", "Precision@5", "NDCG@5", "MRR")
    group_names = {"A": "A 自测", "B": "B 质量", "C": "C 人工"}

    print("── 分组指标 ──")
    print("  三组可信度不同，【不要】平均成一个数。")
    print()
    print(f"  {'组':<5s}{'#Q':>4s}" + "".join(f"{m:>12s}" for m in order))
    print(f"  {'─' * 57}")
    for g in ("A", "B", "C"):
        rs = groups[g]
        if not rs:
            continue
        vals = _mean_metrics(rs, order)
        print(f"  {g:<5s}{len(rs):>4d}", end="")
        for m in order:
            print(f"{vals[m]:>12.4f}" if m in vals else f"{'N/A':>12s}", end="")
        print()

    all_vals = _mean_metrics(results, order)
    print(f"  {'─' * 57}")
    print(f"  {'ALL':<5s}{len(results):>4d}", end="")
    for m in order:
        print(f"{all_vals[m]:>12.4f}" if m in all_vals else f"{'N/A':>12s}", end="")
    print()
    print()
    for g in ("A", "B", "C"):
        if groups[g]:
            print(f"  {g}  {GROUP_NOTES[g]}")
    print("  ALL  ⚠ 含 A 组构造出来的数，拉高整体并稀释真实差异，仅供参考")
    print()

    # ── 通道分层：各通道【单独】检索 ──
    print("── 通道分层 · 各通道单独检索的 Recall@5 ──")
    print("  问的是「这条通道自己找得到吗」，不是「它给融合结果贡献了几条」。")
    print()
    print(f"  {'组':<5s}{'#Q':>4s}" + "".join(f"{ch:>10s}" for ch in CHANNELS) + f"{'merged':>10s}")
    print(f"  {'─' * 53}")
    for g in ("A", "B", "C"):
        rs = groups[g]
        if not rs:
            continue
        print(f"  {g:<5s}{len(rs):>4d}", end="")
        for ch in CHANNELS:
            arr = [r["channel_metrics"][ch]["Recall@5"] for r in rs
                   if "Recall@5" in r["channel_metrics"][ch]]
            print(f"{statistics.mean(arr):>10.4f}" if arr else f"{'N/A':>10s}", end="")
        arr = [r["metrics"]["Recall@5"] for r in rs if "Recall@5" in r["metrics"]]
        print(f"{statistics.mean(arr):>10.4f}" if arr else f"{'N/A':>10s}")
    print()
    # 融合可能【低于】单通道 —— 优先级高的通道用不相关结果挤掉了后面的相关结果。
    # 这是只看融合数字永远发现不了的，必须显式指出来。
    for g in ("A", "B", "C"):
        rs = groups[g]
        if not rs:
            continue
        m = statistics.mean([r["metrics"]["Recall@5"] for r in rs if "Recall@5" in r["metrics"]])
        best_ch, best_v = None, -1.0
        for ch in CHANNELS:
            arr = [r["channel_metrics"][ch]["Recall@5"] for r in rs
                   if "Recall@5" in r["channel_metrics"][ch]]
            if arr and statistics.mean(arr) > best_v:
                best_ch, best_v = ch, statistics.mean(arr)
        if best_ch and m < best_v - 1e-9:
            print(f"  ⚠ {g} 组：融合({m:.4f}) < {best_ch} 单独({best_v:.4f}) —— "
                  f"融合把 {best_ch} 的相关结果挤出了前 10（优先级更高的通道占了名额）")
    print()

    # ── A 自测组逐条：融合分数到底是谁挣来的 ──
    if groups["A"]:
        print("── A 自测组逐条 · 各通道单独命中 GT 几条 ──")
        print("  keyword 列命中率必然 100%：它和 GT 用的是同一个 WHERE 条件（同义反复）。")
        print("  ★ name / vector 两列才是实测 —— 它们说明这个满分里有多少是真本事。")
        print()
        print(f"  {'id':<5s}" + _pad("query", 20) + f"{'GT':>5s}"
              + "".join(f"{ch:>11s}" for ch in CHANNELS) + "   融合来源")
        print(f"  {'─' * 82}")
        fusion_total = {ch: 0 for ch in CHANNELS}
        for r in groups["A"]:
            q = r["query"]
            gt_set = set(q.get("ground_truth", []))
            print(f"  {q['id']:<5s}" + _pad(q["query"][:18], 20) + f"{q.get('gt_size', 0):>5d}", end="")
            for ch in CHANNELS:
                ids = r["retrieved"][ch]
                hits = sum(1 for e in ids if e in gt_set)
                print(f"{f'{hits}/{len(ids)}':>11s}", end="")
            fu = r["retrieved"]["fusion"]
            parts = [f"{ch}×{fu[ch]}" for ch in CHANNELS if fu[ch]]
            print(f"   {'+'.join(parts) if parts else '（空）'}")
            for ch in CHANNELS:
                fusion_total[ch] += fu[ch]
        print(f"  {'─' * 82}")
        n_total = sum(fusion_total.values())
        detail = " + ".join(f"{ch} {fusion_total[ch]}" for ch in CHANNELS)
        print(f"  融合结果共 {n_total} 条 = {detail}")
        print()
        print("  读法：融合来源那一列若是清一色 keyword×N，说明这条查询上")
        print("        name / vector 一条都没进结果 —— 不是它们找到了没用上，")
        print("        是 keyword 先把 10 个名额占满了。所以「融合满分」不能")
        print("        证明向量通道会这些查询；它只证明 SQL 通道会。")
        print()

    # ── 未参与统计的查询 ──
    if dropped:
        print()
        print("── 未参与统计的查询 ──")
        for d in dropped:
            print(f"  {d['id']} \"{d['query']}\" — {d['reason']}")
        print(f"  （共 {len(dropped)} 条；GT 为空无法算指标，但检索跑偏本身就是结果）")

    print()

    # ── 保存详细结果 ──
    merged = {
        "description": "合并后的 Ground Truth + 检索结果",
        "group_legend": {g: GROUP_NOTES[g] for g in ("A", "B", "C")},
        "dropped_queries": dropped,
        "queries": [
            {
                "id": r["query"]["id"],
                "query": r["query"]["query"],
                "entity_type": r["query"]["entity_type"],
                "category": r["query"]["category"],
                "source": r["query"].get("source", "auto"),
                "group": _group_of(r["query"]),
                "gt_size": len(r["query"].get("ground_truth", [])),
                "retrieved_count": len(r["retrieved"]["merged"]),
                "metrics": r["metrics"],
                # 各通道【单独】的指标 —— 只看融合数字看不出"某通道贡献 0"
                "channel_metrics": r["channel_metrics"],
                # 融合结果里各通道各占几条（总分是谁挣来的）
                "fusion": r["retrieved"]["fusion"],
            }
            for r in results
        ],
    }
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    with open(MERGED_GT_FILE, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    print(f"  详细结果已保存到 {MERGED_GT_FILE}")


# ═══════════════════════════════════════════════════════════════════════
# Phase 3: --ablate — 消融实验
# ═══════════════════════════════════════════════════════════════════════

# 消融以【生产配置】为基准，每行只动一个开关 —— delta 才可解释为
# 「相对线上，动这一个开关会怎样」。
#
# 命名规则：`-xxx` = 把生产【开着】的关掉；`+xxx` = 把生产【关着】的打开。
# `+bucketing` 因此不是"消融"而是"候选改动" —— 它的 delta 直接回答
# 「要不要在生产开这个组件」。
ABLATION_CONFIGS = {
    "baseline (生产配置)":   dict(PRODUCTION_RETRIEVAL),
    "-threshold":           {**PRODUCTION_RETRIEVAL, "enable_threshold": False},
    "+bucketing":           {**PRODUCTION_RETRIEVAL, "enable_bucketing": True},
    "-mmr":                 {**PRODUCTION_RETRIEVAL, "enable_mmr": False},
    "vanilla (全关)":        {"enable_threshold": False, "enable_bucketing": False,
                             "enable_mmr": False},
}

BASELINE_NAME = "baseline (生产配置)"


def cmd_ablate():
    """--ablate: 对每个消融配置跑完整 eval，输出对比表。"""
    logging.getLogger("sqlalchemy").setLevel(logging.ERROR)

    # ── 加载 GT ──
    if not AUTO_GT_FILE.exists():
        print(f"错误: {AUTO_GT_FILE.name} 不存在，请先运行 --build")
        return

    with open(AUTO_GT_FILE, encoding="utf-8") as f:
        auto_data = json.load(f)
    auto_queries = auto_data.get("queries", [])
    human_queries = _load_annotated_pooled()
    all_queries = auto_queries + human_queries
    usable_queries, dropped = _split_by_gt(all_queries)

    print(f"消融实验: {len(usable_queries)} 题 × {len(ABLATION_CONFIGS)} 配置"
          + (f"（另有 {len(dropped)} 条 GT 为空，未参与）" if dropped else ""))
    print()

    # ── 对每个配置跑完整 eval ──
    config_results: dict[str, dict] = {}

    for config_name, kwargs in ABLATION_CONFIGS.items():
        print(f"  [{config_name}]", end=" ", flush=True)
        all_metrics: dict[str, list[float]] = {}
        # 分组聚合：主表把 A 组 18 条构造出来的 1.0 混进来，会稀释掉真实差异。
        group_metrics: dict[str, dict[str, list[float]]] = {"A": {}, "B": {}, "C": {}}

        for i, q in enumerate(usable_queries):
            gt = set(q.get("ground_truth", []))
            grp = _group_of(q)

            retrieved = _run_retrieval(
                q["query"], q.get("entity_type", "all"),
                q.get("subject_type"),
                keyword_filters=q.get("keyword_filters"),
                **kwargs,
            )
            metrics = _compute_metrics(retrieved["merged"], gt)
            for key, val in metrics.items():
                all_metrics.setdefault(key, []).append(val)
                group_metrics[grp].setdefault(key, []).append(val)
            time.sleep(0.02)

        # 聚合
        agg = {}
        for key, vals in all_metrics.items():
            agg[key] = statistics.mean(vals) if vals else 0.0

        config_results[config_name] = {
            "metrics": agg,
            "group_metrics": {
                g: {k: statistics.mean(v) for k, v in gm.items() if v}
                for g, gm in group_metrics.items() if gm
            },
            "valid_queries": len(all_metrics.get("Recall@5", [])),
        }
        print(f"{config_results[config_name]['valid_queries']} 题, "
              f"Rec@5={agg.get('Recall@5', 0):.4f}, "
              f"Prec@5={agg.get('Precision@5', 0):.4f}, "
              f"MRR={agg.get('MRR', 0):.4f}")

    # ── 对比表 ──
    baseline = config_results[BASELINE_NAME]["metrics"]
    key_metrics = ["Recall@5", "Precision@5", "MRR", "NDCG@5", "Hit@5"]

    print()
    print("=" * 78)
    print(f"  消融对比 — 相对【{BASELINE_NAME}】的变化")
    print("=" * 78)
    print("  基准就是线上跑的那套参数（tools/bgm_tools.py 不传消融开关）。")
    # 单元格宽 13 = "0.4172 +0.0000"；baseline 行不显示 delta，用 >13 补齐以对齐
    header = "  " + _pad("配置", 26)
    for m in key_metrics:
        header += f"  {m:>13s}"
    print(header)
    print(f"  {'─' * 82}")

    for config_name in ABLATION_CONFIGS:
        agg = config_results[config_name]["metrics"]
        row = "  " + _pad(config_name, 26)
        for m in key_metrics:
            val = agg.get(m, 0)
            if config_name == BASELINE_NAME:
                row += f"  {val:>13.4f}"
            else:
                delta = val - baseline.get(m, 0)
                sign = "+" if delta >= 0 else ""
                row += f"  {val:.4f} {sign}{delta:.4f}"
        print(row)

    print()
    print("  解读: 正 delta = 这一改动【比线上好】，负 = 比线上差。")
    print("        `-xxx` 行看你关掉线上某组件会损失多少；")
    print("        `+bucketing` 行是【候选改动】—— 正数就直接说明该在生产开它。")
    print()

    # ── 分组对比 ──
    # 上面那张表把 A 组 18 条构造出来的 1.0 平均了进去，配置差异被稀释。
    # 分组看才知道某个组件到底帮的是【哪一类】查询。
    print("=" * 78)
    print("  分组 Recall@5（消除 A 组稀释后的真实差异）")
    print("=" * 78)
    print("  " + _pad("配置", 26) + "".join(f"{g:>12s}" for g in ("A 自测", "B 质量", "C 人工")))
    print(f"  {'─' * 74}")

    for config_name in ABLATION_CONFIGS:
        gm = config_results[config_name]["group_metrics"]
        row = "  " + _pad(config_name, 26)
        for g in ("A", "B", "C"):
            val = gm.get(g, {}).get("Recall@5")
            row += f"{val:>12.4f}" if val is not None else f"{'N/A':>12s}"
        print(row)

    print()
    print("  读法: A 组的数【不反映质量】—— 它就是配置的函数（GT 与检索同源）。")
    print("        若某配置把 A 组压低了 → keyword 通道被关掉或坏了，那是【回归】。")
    print("        B / C 组才是组件收益所在。")
    print()


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(description="RAG 检索评测管线 v2")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--build", action="store_true",
                       help="生成自动 Ground Truth + 池化人工标注模板")
    group.add_argument("--evaluate", action="store_true",
                       help="检索 + 计算全部指标")
    group.add_argument("--ablate", action="store_true",
                       help="消融实验：对比 threshold/bucketing/MMR 各组件的贡献")
    args = parser.parse_args()

    if args.build:
        cmd_build()
    elif args.evaluate:
        cmd_evaluate()
    elif args.ablate:
        cmd_ablate()


if __name__ == "__main__":
    main()
