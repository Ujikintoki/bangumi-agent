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

    - 全局（全部 35 题）
    - 按 category：tag / year / score / career / exact / semantic
    - 按来源：auto（自动 GT，Recall 精确）vs human（池化标注，Recall 近似）
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
import statistics
import time
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

# ═══════════════════════════════════════════════════════════════════════
# 查询定义（从 queries.py 导入）
# ═══════════════════════════════════════════════════════════════════════

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
            # 注入 subject_type 过滤：SQL 里只写 entity_type 过滤，
            # subject_type 由这里自动追加，保证 GT 与检索范围一致
            filtered_sql = sql
            if stype is not None:
                filtered_sql = f"{sql} AND subject_type={stype}"
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
                    distance_threshold=0.9,
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
) -> list[str]:
    """三通道检索：关键词精确 > trigram 名称 > 向量语义补位。

    ablation_kwargs 透传给 hybrid_search 的消融开关。
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

    # 通道 3: 向量语义匹配
    try:
        results = retriever.hybrid_search(
            query=query, entity_type=entity_type,
            subject_type=subject_type, limit=limit,
            distance_threshold=0.9, **ablation_kwargs,
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
    return merged[:limit]


def _dcg(relevances: list[int]) -> float:
    return sum((2**rel - 1) / math.log2(i + 2) for i, rel in enumerate(relevances))


def _ndcg(relevances: list[int]) -> float:
    dcg = _dcg(relevances)
    ideal = sorted(relevances, reverse=True)
    idcg = _dcg(ideal)
    return dcg / idcg if idcg > 0 else 1.0


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

    # NDCG@K
    for k in K_VALUES:
        relevances = [1 if eid in gt_set else 0 for eid in retrieved_ids[:k]]
        scores[f"NDCG@{k}"] = _ndcg(relevances)

    return scores


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
    print(f"合计: {len(all_queries)} 条\n")

    # ── 检索 + 算指标 ──
    results: list[dict] = []
    for i, q in enumerate(all_queries):
        qid = q["id"]
        qtext = q["query"]
        etype = q.get("entity_type", "all")
        gt = set(q.get("ground_truth", []))

        if not gt:
            continue

        print(f"  [{i+1:2d}/{len(all_queries)}] {qid} \"{qtext}\" GT={len(gt)}", end=" ... ")
        retrieved = _run_retrieval(
            qtext, etype, q.get("subject_type"),
            keyword_filters=q.get("keyword_filters"),
        )
        metrics = _compute_metrics(retrieved, gt)
        print(f"{len(retrieved)} hits, Recall@5={metrics.get('Recall@5', 0):.3f}")

        results.append({
            "query": q,
            "retrieved": retrieved,
            "metrics": metrics,
        })
        time.sleep(0.05)  # 轻微限流

    # ── 聚合 ──
    all_metrics: dict[str, list[float]] = {}
    for r in results:
        for key, val in r["metrics"].items():
            all_metrics.setdefault(key, []).append(val)

    # ── 报告 ──
    print()
    print("=" * 64)
    print("  RAG 检索评测报告")
    print("=" * 64)
    print(f"  有效 query 数: {len(results)}")
    print(f"  GT 来源: auto={len(auto_queries)}, human={len(human_queries)}")
    print()

    # 全局
    print("── 全局指标 ──")
    print(f"  {'指标':<14s}", end="")
    for k in K_VALUES:
        print(f"  {'@'+str(k):>8s}", end="")
    print()
    print(f"  {'─' * 48}")

    for metric_name in ["Recall", "Precision", "NDCG", "Hit"]:
        print(f"  {metric_name:<14s}", end="")
        for k in K_VALUES:
            key = f"{metric_name}@{k}"
            vals = all_metrics.get(key, [])
            if vals:
                print(f"  {statistics.mean(vals):>8.4f}", end="")
            else:
                print(f"  {'N/A':>8s}", end="")
        print()

    # MRR 单独
    mrr_vals = all_metrics.get("MRR", [])
    if mrr_vals:
        print(f"  {'MRR':<14s}  {statistics.mean(mrr_vals):>8.4f}")
    print()

    # ── 按 category 细分 ──
    cats: dict[str, list[dict]] = {}
    for r in results:
        c = r["query"].get("category", "unknown")
        cats.setdefault(c, []).append(r)

    print("── 按 category 细分 ──")
    print(f"  {'Category':<12s} {'#Q':>4s}  {'Recall@5':>10s}  {'Prec@5':>9s}  {'NDCG@5':>9s}  {'MRR':>8s}")
    print(f"  {'─' * 58}")
    for cat in sorted(cats):
        rs = cats[cat]
        vals = {
            m: statistics.mean([r["metrics"][m] for r in rs if m in r["metrics"]])
            for m in ["Recall@5", "Precision@5", "NDCG@5", "MRR"]
            if any(m in r["metrics"] for r in rs)
        }
        print(f"  {cat:<12s} {len(rs):>4d}", end="")
        for m in ["Recall@5", "Precision@5", "NDCG@5", "MRR"]:
            if m in vals:
                print(f"  {vals[m]:>9.4f}", end="")
            else:
                print(f"  {'N/A':>9s}", end="")
        print()

    # ── 按来源细分 ──
    print()
    print("── 按 GT 来源细分 ──")
    sources: dict[str, list[dict]] = {}
    for r in results:
        src = r["query"].get("source", "auto")
        sources.setdefault(src, []).append(r)

    print(f"  {'来源':<10s} {'#Q':>4s}  {'Recall@5':>10s}  {'Prec@5':>9s}  {'NDCG@5':>9s}  {'MRR':>8s}")
    print(f"  {'─' * 56}")
    for src in ["auto", "human"]:
        if src not in sources:
            continue
        rs = sources[src]
        vals = {}
        for m in ["Recall@5", "Precision@5", "NDCG@5", "MRR"]:
            arr = [r["metrics"][m] for r in rs if m in r["metrics"]]
            if arr:
                vals[m] = statistics.mean(arr)

        label = "自动(精确)" if src == "auto" else "人工(近似)"
        print(f"  {label:<10s} {len(rs):>4d}", end="")
        for m in ["Recall@5", "Precision@5", "NDCG@5", "MRR"]:
            if m in vals:
                print(f"  {vals[m]:>9.4f}", end="")
            else:
                print(f"  {'N/A':>9s}", end="")
        print()

    if "human" in sources and "auto" in sources:
        print()
        print("  ℹ Recall: auto 组基于 DB 完整 GT（精确），human 组基于池化标注（近似下界）")

    print()

    # ── 保存详细结果 ──
    merged = {
        "description": "合并后的 Ground Truth + 检索结果",
        "queries": [
            {
                "id": r["query"]["id"],
                "query": r["query"]["query"],
                "entity_type": r["query"]["entity_type"],
                "category": r["query"]["category"],
                "source": r["query"].get("source", "auto"),
                "gt_size": len(r["query"].get("ground_truth", [])),
                "retrieved_count": len(r["retrieved"]),
                "metrics": r["metrics"],
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

ABLATION_CONFIGS = {
    "baseline (全开)":      {"enable_threshold": True,  "enable_bucketing": True,  "enable_mmr": True},
    "no_threshold":         {"enable_threshold": False, "enable_bucketing": True,  "enable_mmr": True},
    "no_bucketing":         {"enable_threshold": True,  "enable_bucketing": False, "enable_mmr": True},
    "no_mmr":               {"enable_threshold": True,  "enable_bucketing": True,  "enable_mmr": False},
    "vanilla (全关)":       {"enable_threshold": False, "enable_bucketing": False, "enable_mmr": False},
}


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

    print(f"消融实验: {len(all_queries)} 题 × {len(ABLATION_CONFIGS)} 配置")
    print()

    # ── 对每个配置跑完整 eval ──
    config_results: dict[str, dict] = {}

    for config_name, kwargs in ABLATION_CONFIGS.items():
        print(f"  [{config_name}]", end=" ", flush=True)
        all_metrics: dict[str, list[float]] = {}

        for i, q in enumerate(all_queries):
            gt = set(q.get("ground_truth", []))
            if not gt:
                continue

            retrieved = _run_retrieval(
                q["query"], q.get("entity_type", "all"),
                q.get("subject_type"),
                keyword_filters=q.get("keyword_filters"),
                **kwargs,
            )
            metrics = _compute_metrics(retrieved, gt)
            for key, val in metrics.items():
                all_metrics.setdefault(key, []).append(val)
            time.sleep(0.02)

        # 聚合
        agg = {}
        for key, vals in all_metrics.items():
            agg[key] = statistics.mean(vals) if vals else 0.0

        config_results[config_name] = {
            "metrics": agg,
            "valid_queries": len(all_metrics.get("Recall@5", [])),
        }
        print(f"{config_results[config_name]['valid_queries']} 题, "
              f"Rec@5={agg.get('Recall@5', 0):.4f}, "
              f"Prec@5={agg.get('Precision@5', 0):.4f}, "
              f"MRR={agg.get('MRR', 0):.4f}")

    # ── 对比表 ──
    baseline = config_results["baseline (全开)"]["metrics"]
    key_metrics = ["Recall@5", "Precision@5", "MRR", "NDCG@5", "Hit@5"]

    print()
    print("=" * 78)
    print("  消融对比 — 相对 baseline 变化")
    print("=" * 78)
    header = f"  {'配置':<24s}"
    for m in key_metrics:
        header += f"  {m:>10s}"
    print(header)
    print(f"  {'─' * 76}")

    for config_name in ABLATION_CONFIGS:
        agg = config_results[config_name]["metrics"]
        row = f"  {config_name:<24s}"
        for m in key_metrics:
            val = agg.get(m, 0)
            if config_name == "baseline (全开)":
                row += f"  {val:>10.4f}"
            else:
                delta = val - baseline.get(m, 0)
                sign = "+" if delta >= 0 else ""
                row += f"  {val:.4f} {sign}{delta:.4f}"
        print(row)

    print()
    print("  解读: 负数 delta = 该组件有正向贡献（关掉后指标下降）")
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
