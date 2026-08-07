"""
RAG 检索评测管线 v2

三步工作流::

    # 1. 我运行：生成自动 Ground Truth + 池化人工标注模板
    python scripts/eval/evaluate.py --build

    # 2. 你标注：打开 pooled_annotate.json，标记每个候选实体是否相关
    #    格式：将 "relevant": null 改为 true 或 false

    # 3. 我运行：计算全部指标 + 报告
    python scripts/eval/evaluate.py --evaluate

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
import statistics
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

logging.basicConfig(level=logging.WARNING, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("eval")

EVAL_DIR = Path(__file__).resolve().parent
AUTO_GT_FILE = EVAL_DIR / "ground_truth_auto.json"
POOLED_FILE = EVAL_DIR / "pooled_annotate.json"
MERGED_GT_FILE = EVAL_DIR / "ground_truth_merged.json"

K_VALUES = [1, 3, 5, 10]

# ═══════════════════════════════════════════════════════════════════════
# 1. 自动 Ground Truth — 从数据库元数据生成
# ═══════════════════════════════════════════════════════════════════════

# 每条定义：(query_id, query_text, entity_type, category, GT_SQL)
# GT_SQL 返回 id 列表，这些 id = 该 query 的完整正确答案
AUTO_QUERY_DEFS: list[tuple[str, str, str, str, str]] = [
    # ── 标签匹配：搜标签名 → GT = 所有带该标签的 subject ──
    ("a01", "芳文社", "subject", "tag",
     """SELECT id FROM rag_entities WHERE entity_type='subject'
        AND meta_info->'tags' @> '[{"name": "芳文社"}]'"""),
    ("a02", "CloverWorks", "subject", "tag",
     """SELECT id FROM rag_entities WHERE entity_type='subject'
        AND meta_info->'tags' @> '[{"name": "CloverWorks"}]'"""),
    ("a03", "京都动画", "subject", "tag",
     """SELECT id FROM rag_entities WHERE entity_type='subject'
        AND meta_info->'tags' @> '[{"name": "京都动画"}]'"""),
    ("a04", "ufotable", "subject", "tag",
     """SELECT id FROM rag_entities WHERE entity_type='subject'
        AND meta_info->'tags' @> '[{"name": "ufotable"}]'"""),
    ("a05", "Production I.G", "subject", "tag",
     """SELECT id FROM rag_entities WHERE entity_type='subject'
        AND meta_info->'tags' @> '[{"name": "Production.I.G"}]'"""),
    ("a06", "TRIGGER", "subject", "tag",
     """SELECT id FROM rag_entities WHERE entity_type='subject'
        AND meta_info->'tags' @> '[{"name": "TRIGGER"}]'"""),
    ("a07", "动画工房", "subject", "tag",
     """SELECT id FROM rag_entities WHERE entity_type='subject'
        AND meta_info->'tags' @> '[{"name": "动画工房"}]'"""),
    ("a08", "虚渊玄", "subject", "tag",
     """SELECT id FROM rag_entities WHERE entity_type='subject'
        AND meta_info->'tags' @> '[{"name": "虚渊玄"}]'"""),

    # ── 更多标签 ──
    ("a09", "MADHOUSE", "subject", "tag",
     """SELECT id FROM rag_entities WHERE entity_type='subject'
        AND meta_info->'tags' @> '[{"name": "MADHouse"}]'"""),
    ("a10", "BONES", "subject", "tag",
     """SELECT id FROM rag_entities WHERE entity_type='subject'
        AND meta_info->'tags' @> '[{"name": "BONES"}]'"""),
    ("a11", "P.A.WORKS", "subject", "tag",
     """SELECT id FROM rag_entities WHERE entity_type='subject'
        AND meta_info->'tags' @> '[{"name": "P.A.WORKS"}]'"""),
    ("a12", "吉卜力", "subject", "tag",
     """SELECT id FROM rag_entities WHERE entity_type='subject'
        AND meta_info->'tags' @> '[{"name": "吉卜力"}]'"""),

    # ── 年份 ──
    ("a13", "2023年的动画", "subject", "year",
     """SELECT id FROM rag_entities WHERE entity_type='subject'
        AND meta_info->>'year' = '2023'"""),
    ("a14", "2022年的动画", "subject", "year",
     """SELECT id FROM rag_entities WHERE entity_type='subject'
        AND meta_info->>'year' = '2022'"""),

    # ── 评分 ──
    ("a15", "高分神作", "subject", "score",
     """SELECT id FROM rag_entities WHERE entity_type='subject'
        AND (meta_info->>'score')::float >= 8.8"""),

    # ── 声优/创作者 ──
    ("a16", "声优", "person", "career",
     """SELECT id FROM rag_entities WHERE entity_type='person'
        AND meta_info->'career' ? 'seiyu'"""),
    ("a17", "歌手艺人", "person", "career",
     """SELECT id FROM rag_entities WHERE entity_type='person'
        AND meta_info->'career' ? 'artist'"""),
    ("a18", "动画制作人", "person", "career",
     """SELECT id FROM rag_entities WHERE entity_type='person'
        AND meta_info->'career' ? 'producer'"""),

    # ── 精确名称（人工指定 target ID）──
    ("a19", "孤独摇滚", "subject", "exact",
     "subject_328609"),
    ("a20", "進撃の巨人", "subject", "exact",
     "subject_290980"),
    ("a21", "命运石之门", "subject", "exact",
     "subject_10380"),
    ("a22", "化物語", "subject", "exact",
     "subject_793"),
    ("a23", "CLANNAD", "subject", "exact",
     "subject_51"),
    ("a24", "牧瀬紅莉栖", "character", "exact",
     "character_12393"),
    ("a25", "花澤香菜", "person", "exact",
     "person_4765"),
]

# ═══════════════════════════════════════════════════════════════════════
# 2. 池化人工标注查询 — 语义查询，无客观 GT，需人工判 relevance
# ═══════════════════════════════════════════════════════════════════════

POOLED_QUERIES: list[dict] = [
    {"id": "p01", "query": "关于音乐乐队的百合动画",
     "entity_type": "subject", "category": "semantic",
     "desc": "语义 — 三个概念组合"},
    {"id": "p02", "query": "异世界转生冒险",
     "entity_type": "subject", "category": "semantic",
     "desc": "语义 — 奇幻子类型"},
    {"id": "p03", "query": "机甲战斗科幻",
     "entity_type": "subject", "category": "semantic",
     "desc": "语义 — 科幻子类型"},
    {"id": "p04", "query": "悬疑推理惊悚",
     "entity_type": "subject", "category": "semantic",
     "desc": "语义 — 类型组合"},
    {"id": "p05", "query": "温馨轻松的日常故事",
     "entity_type": "subject", "category": "semantic",
     "desc": "语义 — 氛围/节奏"},
    {"id": "p06", "query": "制作精良的剧场版动画电影",
     "entity_type": "subject", "category": "semantic",
     "desc": "语义 — 品质+类型"},
    {"id": "p07", "query": "傲娇双马尾美少女",
     "entity_type": "character", "category": "semantic",
     "desc": "角色 — 属性组合"},
    {"id": "p08", "query": "帅气冷酷的男性角色",
     "entity_type": "character", "category": "semantic",
     "desc": "角色 — 性格描述"},
    {"id": "p09", "query": "知名动画导演",
     "entity_type": "person", "category": "semantic",
     "desc": "人物 — 职位+知名度"},
    {"id": "p10", "query": "配乐出色的作曲家",
     "entity_type": "person", "category": "semantic",
     "desc": "人物 — 职业+品质"},
]

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


# ═══════════════════════════════════════════════════════════════════════
# Phase 1: --build — 生成 Ground Truth
# ═══════════════════════════════════════════════════════════════════════


def build_auto_gt() -> list[dict]:
    """执行 SQL 生成自动 Ground Truth。"""
    queries = []
    for qid, qtext, etype, cat, sql in AUTO_QUERY_DEFS:
        print(f"  [{qid}] \"{qtext}\"", end=" ... ")

        if cat == "exact":
            # 精确名称：GT 就是这一个 id
            gt = [sql] if sql else []
        else:
            gt = _query_db(sql)

        queries.append({
            "id": qid,
            "query": qtext,
            "entity_type": etype,
            "category": cat,
            "ground_truth": gt,
            "gt_size": len(gt),
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

        for search_type in ["all", etype]:
            try:
                results = retriever.hybrid_search(
                    query=qtext,
                    entity_type=search_type,
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
                "标注完成后运行: python scripts/eval/evaluate.py --evaluate"
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
    print(f"  1. 打开 {POOLED_FILE.name}")
    print(f"  2. 对每个 query 的 candidates，改 relevant: null → true/false")
    print(f"  3. 运行 python scripts/eval/evaluate.py --evaluate")
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
            "category": pq.get("category", "semantic"),
            "source": "human",
            "ground_truth": gt,
            "gt_size": len(gt),
        })

    if unannotated > 0:
        print(f"  ⚠ {unannotated} 个候选实体未标注（relevant=null），已视为不相关")

    return queries


def _run_retrieval(query: str, entity_type: str, limit: int = 10) -> list[str]:
    """检索并返回 entity_id 有序列表。"""
    retriever = _get_retriever()
    try:
        results = retriever.hybrid_search(
            query=query,
            entity_type=entity_type,
            limit=limit,
            distance_threshold=0.9,
        )
        return [r.entity_id for r in results]
    except Exception as e:
        logger.error("检索失败 %s: %s", query, e)
        return []


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
        retrieved = _run_retrieval(qtext, etype)
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
    with open(MERGED_GT_FILE, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    print(f"  详细结果已保存到 {MERGED_GT_FILE.name}")


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
    args = parser.parse_args()

    if args.build:
        cmd_build()
    elif args.evaluate:
        cmd_evaluate()


if __name__ == "__main__":
    main()
