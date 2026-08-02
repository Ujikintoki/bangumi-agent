"""
RAG 检索独立评测管线

不依赖 agent 图。仅需数据库连接 + Zhipu embedding API。
消融实验 4 配置对比，产出 per-category 分类型分析。

用法::

    python eval/eval_rag.py                              # 跑全部 4 配置
    python eval/eval_rag.py --config vanilla              # 只跑基线
    python eval/eval_rag.py --output results/rag_report.md
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# 确保项目根目录在 Python path 中
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("eval.rag")

# ═══════════════════════════════════════════════════════════════════════════════
# 消融配置
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class AblationConfig:
    name: str
    enable_threshold: bool
    enable_bucketing: bool
    enable_mmr: bool
    hypothesis: str


ABLATION_CONFIGS: list[AblationConfig] = [
    AblationConfig(
        name="Vanilla pgvector",
        enable_threshold=False,
        enable_bucketing=False,
        enable_mmr=False,
        hypothesis="纯 cosine_distance 排序，无任何后处理",
    ),
    AblationConfig(
        name="+ Distance threshold (0.65)",
        enable_threshold=True,
        enable_bucketing=False,
        enable_mmr=False,
        hypothesis="阈值过滤剔除完全不相关的噪音 (cosine_distance > 0.65)，但不改变排序",
    ),
    AblationConfig(
        name="+ Semantic bucketing + log norm",
        enable_threshold=True,
        enable_bucketing=True,
        enable_mmr=False,
        hypothesis="分桶后同梯队内按对数热度排序——冷门作品不再被热门以微小距离优势挤出",
    ),
    AblationConfig(
        name="+ MMR dedup (Full pipeline)",
        enable_threshold=True,
        enable_bucketing=True,
        enable_mmr=True,
        hypothesis="MMR 同名去重解决同系列刷屏问题，进一步提升 precision",
    ),
]


# ═══════════════════════════════════════════════════════════════════════════════
# 评测主流程
# ═══════════════════════════════════════════════════════════════════════════════


def _build_retriever():
    """初始化 RagEntityRetriever。"""
    from core.config import get_settings
    from database.engine import engine
    from rag.retriever import RagEntityRetriever

    settings = get_settings()
    return RagEntityRetriever(
        engine=engine,
        zhipu_api_key=settings.ZHIPU_API_KEY,
        zhipu_base_url=settings.ZHIPU_BASE_URL,
    )


def _skip_query(query: dict) -> bool:
    """跳过 ground_truth 为空的查询（标注未完成）。"""
    gt = query.get("ground_truth", [])
    return not gt or len(gt) == 0


def compute_query_metrics(
    retrieved_ids: list[str],
    ground_truth_ids: list[str],
) -> dict:
    """计算单条查询的指标。"""
    from eval.metrics import mrr, precision_at_k, recall_at_k

    gt_set = set(ground_truth_ids)
    return {
        "recall@1": round(recall_at_k(retrieved_ids, gt_set, 1), 4),
        "recall@3": round(recall_at_k(retrieved_ids, gt_set, 3), 4),
        "recall@5": round(recall_at_k(retrieved_ids, gt_set, 5), 4),
        "precision@5": round(precision_at_k(retrieved_ids, gt_set, 5), 4),
        "mrr": round(mrr(retrieved_ids, gt_set), 4),
    }


def run_ablation(
    retriever,
    queries: list[dict],
    config: AblationConfig,
) -> dict:
    """跑单个消融配置。

    Args:
        retriever: RagEntityRetriever 实例。
        queries: 评测查询列表（已过滤空 ground_truth）。
        config: 消融配置。

    Returns:
        包含该配置下的全量指标和 per-category 指标。
    """
    logger.info("── %s ──", config.name)

    all_metrics: list[dict] = []
    per_category: dict[str, list[dict]] = {}

    for q in queries:
        try:
            results = retriever.hybrid_search(
                query=q["text"],
                entity_type=q.get("entity_type", "all"),
                limit=5,
                enable_threshold=config.enable_threshold,
                enable_bucketing=config.enable_bucketing,
                enable_mmr=config.enable_mmr,
            )
            retrieved_ids = [r.entity_id for r in results]
        except Exception as exc:
            logger.warning("  查询失败 '%s': %s", q["text"][:50], exc)
            retrieved_ids = []

        m = compute_query_metrics(retrieved_ids, q["ground_truth"])
        m["query_id"] = q["id"]
        m["category"] = q["category"]
        all_metrics.append(m)

        cat = q["category"]
        if cat not in per_category:
            per_category[cat] = []
        per_category[cat].append(m)

        logger.debug(
            "  %s: recall@5=%.2f precision@5=%.2f mrr=%.2f",
            q["id"], m["recall@5"], m["precision@5"], m["mrr"],
        )

    # 汇总
    def _avg(key: str, metrics: list[dict]) -> float:
        vals = [m[key] for m in metrics]
        return round(sum(vals) / len(vals), 4) if vals else 0.0

    overall = {
        "config": config.name,
        "n_queries": len(all_metrics),
        "recall@1": _avg("recall@1", all_metrics),
        "recall@3": _avg("recall@3", all_metrics),
        "recall@5": _avg("recall@5", all_metrics),
        "precision@5": _avg("precision@5", all_metrics),
        "mrr": _avg("mrr", all_metrics),
    }

    by_cat: dict[str, dict] = {}
    for cat, cat_metrics in per_category.items():
        by_cat[cat] = {
            "n": len(cat_metrics),
            "recall@5": _avg("recall@5", cat_metrics),
            "precision@5": _avg("precision@5", cat_metrics),
            "mrr": _avg("mrr", cat_metrics),
        }

    logger.info(
        "  → recall@5=%.4f  precision@5=%.4f  mrr=%.4f",
        overall["recall@5"], overall["precision@5"], overall["mrr"],
    )

    return {"overall": overall, "by_category": by_cat, "per_query": all_metrics}


def evaluate(
    data_path: str = "eval/data/rag_queries.json",
    config_filter: str | None = None,
) -> list[dict]:
    """跑全部（或指定）消融配置。

    Args:
        data_path: 评测数据集路径。
        config_filter: 只跑指定配置（名称模糊匹配）。None = 全跑。

    Returns:
        每个配置的结果 dict 列表。
    """
    # 加载数据
    data = json.loads(Path(data_path).read_text(encoding="utf-8"))
    all_queries: list[dict] = data["queries"]
    valid_queries = [q for q in all_queries if not _skip_query(q)]
    skipped = len(all_queries) - len(valid_queries)

    logger.info("评测数据集: %d 条查询 (%d 条已标注, %d 条待标注)",
                len(all_queries), len(valid_queries), skipped)

    if not valid_queries:
        logger.error("没有已标注的查询！请先在 eval/data/rag_queries.json 中填充 ground_truth。")
        logger.error("标注方法: 在 Bangumi 网站搜索 query text → 取前 3-5 个相关 subject_id")
        logger.error("→ 映射为 rag_entities 表主键 (如 subject_265) → 填入 ground_truth 数组")
        sys.exit(1)

    # 初始化
    logger.info("初始化 RagEntityRetriever...")
    retriever = _build_retriever()

    # 跑消融
    configs = ABLATION_CONFIGS
    if config_filter:
        configs = [c for c in configs if config_filter.lower() in c.name.lower()]
        if not configs:
            logger.error("未找到匹配的配置: %s", config_filter)
            sys.exit(1)

    all_results: list[dict] = []
    for config in configs:
        result = run_ablation(retriever, valid_queries, config)
        all_results.append(result)

    return all_results


# ═══════════════════════════════════════════════════════════════════════════════
# 报告输出
# ═══════════════════════════════════════════════════════════════════════════════


CATEGORY_LABELS = {
    "A": "A. 精确作品名",
    "B": "B. 模糊描述",
    "C": "C. 冷门作品",
    "D": "D. 跨类型查询",
    "E": "E. 模糊边界",
}


def print_report(all_results: list[dict]) -> None:
    """打印评测报告到 stdout。"""
    print("\n" + "=" * 70)
    print("  RAG 检索消融实验")
    print("=" * 70)

    # 总览表
    print(f"\n  {'Configuration':<35} {'recall@5':>10} {'precision@5':>12} {'MRR':>8}")
    print("  " + "-" * 67)
    for r in all_results:
        o = r["overall"]
        print(f"  {o['config']:<35} {o['recall@5']:>10.4f} {o['precision@5']:>12.4f} {o['mrr']:>8.4f}")

    # 分类型
    categories = sorted(set(
        cat for r in all_results for cat in r["by_category"]
    ))
    for cat in categories:
        print(f"\n  {CATEGORY_LABELS.get(cat, cat)}:")
        print(f"  {'Configuration':<35} {'recall@5':>10} {'precision@5':>12} {'MRR':>8}")
        print("  " + "-" * 67)
        for r in all_results:
            bc = r["by_category"].get(cat, {})
            print(f"  {r['overall']['config']:<35} "
                  f"{bc.get('recall@5', 0):>10.4f} "
                  f"{bc.get('precision@5', 0):>12.4f} "
                  f"{bc.get('mrr', 0):>8.4f}")

    # Delta
    if len(all_results) >= 2:
        baseline = all_results[0]["overall"]
        full = all_results[-1]["overall"]
        print(f"\n  Baseline → Full pipeline delta:")
        delta_r = full["recall@5"] - baseline["recall@5"]
        delta_p = full["precision@5"] - baseline["precision@5"]
        delta_m = full["mrr"] - baseline["mrr"]
        print(f"  recall@5:  {baseline['recall@5']:.4f} → {full['recall@5']:.4f} "
              f"({'↑' if delta_r >= 0 else '↓'}{abs(delta_r):.4f})")
        print(f"  precision@5: {baseline['precision@5']:.4f} → {full['precision@5']:.4f} "
              f"({'↑' if delta_p >= 0 else '↓'}{abs(delta_p):.4f})")
        print(f"  MRR:       {baseline['mrr']:.4f} → {full['mrr']:.4f} "
              f"({'↑' if delta_m >= 0 else '↓'}{abs(delta_m):.4f})")

    print()


def save_report(all_results: list[dict], output_path: str) -> None:
    """保存 Markdown 评测报告。"""
    lines: list[str] = []
    lines.append("# RAG Retrieval Evaluation Report")
    lines.append(f"\n**日期**: 2026-08-01 | **查询数**: {all_results[0]['overall']['n_queries']}")
    lines.append(f"\n## 消融实验配置\n")
    for c in ABLATION_CONFIGS:
        lines.append(f"- **{c.name}**: {c.hypothesis}")

    lines.append(f"\n## 总体指标\n")
    lines.append(f"| Configuration | recall@1 | recall@3 | recall@5 | precision@5 | MRR |")
    lines.append(f"|---|------|------|------|------|------|")
    for r in all_results:
        o = r["overall"]
        lines.append(f"| {o['config']} | {o['recall@1']:.4f} | {o['recall@3']:.4f} | "
                     f"{o['recall@5']:.4f} | {o['precision@5']:.4f} | {o['mrr']:.4f} |")

    lines.append(f"\n## 分类型 recall@5\n")
    categories = sorted(set(cat for r in all_results for cat in r["by_category"]))
    header = "| Category (n) | " + " | ".join(r["overall"]["config"] for r in all_results) + " |"
    sep = "|---|" + "|".join(["---" for _ in all_results]) + "|"
    lines.append(header)
    lines.append(sep)
    for cat in categories:
        config_vals = []
        for r in all_results:
            bc = r["by_category"].get(cat, {})
            config_vals.append(f"{bc.get('recall@5', 0):.4f}")
        label = CATEGORY_LABELS.get(cat, cat)
        n = next((r["by_category"].get(cat, {}).get("n", "?") for r in all_results if cat in r["by_category"]), "?")
        lines.append(f"| {label} ({n}) | " + " | ".join(config_vals) + " |")

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_text("\n".join(lines), encoding="utf-8")
    logger.info("报告已保存: %s", output_path)


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(description="RAG 检索消融实验")
    parser.add_argument(
        "--data", default="eval/data/rag_queries.json",
        help="评测数据集路径",
    )
    parser.add_argument(
        "--config", default=None,
        help="只跑指定配置 (如 'vanilla' 或 'Full pipeline')",
    )
    parser.add_argument(
        "--output", default=None,
        help="Markdown 报告输出路径",
    )
    args = parser.parse_args()

    results = evaluate(data_path=args.data, config_filter=args.config)
    print_report(results)

    if args.output:
        save_report(results, args.output)


if __name__ == "__main__":
    main()
