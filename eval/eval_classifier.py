"""
意图分类器独立评测管线

不依赖 agent 图、不依赖数据库、不依赖 Bangumi API。
仅需 LLM API Key（DeepSeek 或其他 OpenAI 兼容 provider）。

用法::

    python eval/eval_classifier.py                          # 跑 LLM 分类器
    python eval/eval_classifier.py --baseline keyword       # 跑关键词基线
    python eval/eval_classifier.py --output results/classifier_report.md

产出:
  - stdout: 实时进度 + 汇总表
  - 报告文件: Markdown 格式的完整评测报告（含混淆矩阵）
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional

# 确保项目根目录在 Python path 中（从 eval/ 目录 import 需要）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("eval.classifier")

# ═══════════════════════════════════════════════════════════════════════════════
# 关键词基线分类器
# ═══════════════════════════════════════════════════════════════════════════════

_KEYWORD_RULES: list[tuple[str, str]] = [
    # (关键词/正则, intent) — 按优先级排列
    ("再见|拜拜|谢谢|你好|嗨|早上好|晚上好|在吗|哈哈|你是谁|你叫什么", "chitchat"),
    ("推荐|有没有.*的|有什么.*的|想看.*的|找.*番|类似.*的|冷门.*推荐|求.*番", "discovery"),
    ("这季|今季|今天更新|排期|热门|最近.*新番|定档|霸权|黑马|热议", "realtime"),
    ("被高估|被过誉|真的太烂|不如以前|全是垃圾|我觉得.*比.*厉害|为什么.*吹|商业化|艺术性", "debate"),
    ("好累|烦死了|无聊|心情不好|失恋|开心|郁闷|难过|压力|空虚|感动|看不进去", "emotional"),
    ("评分多少|评分|帮我查|具体讲讲|是谁|叫什么|什么类型|评分|详情|系列|版本|剧情|有哪些", "lookup"),
    ("什么是|是什么|为什么|怎么.*的|区别|区别是什么|是怎么|运作", "factual"),
    ("^[。！？\\.\\,\\s]+$|^嗯$|^哦$|^在$|^md$|测试|123", "unknown"),
]

# 单字/短作品名白名单——已知的 ACGN 作品名缩写
_KNOWN_SHORT_TITLES = frozenset({
    "eva", "86", "k", "c", "fz", "ubw", "ab", "lb",
    "cl", "wa", "sa", "dc", "ef", "ac", "sd",
})


def _classify_keyword(text: str) -> str:
    """基于关键词+正则的 8-way 意图分类（基线）。

    Args:
        text: 用户输入文本。

    Returns:
        intent 字符串。
    """
    text_stripped = text.strip()
    text_lower = text_stripped.lower()

    # 短文本检查: 单字或极短的已知作品名 → lookup
    if len(text_stripped) <= 3 and text_lower in _KNOWN_SHORT_TITLES:
        return "lookup"

    # 纯标点 → unknown
    if all(c in "。！？.…,，、 " for c in text_stripped):
        return "unknown"

    for pattern, intent in _KEYWORD_RULES:
        import re
        if re.search(pattern, text_stripped):
            return intent

    # 兜底: 短文本 → unknown，长文本 → lookup
    if len(text_stripped) <= 2:
        return "unknown"
    return "lookup"


# ═══════════════════════════════════════════════════════════════════════════════
# LLM 分类器
# ═══════════════════════════════════════════════════════════════════════════════


async def _classify_llm(text: str) -> str:
    """使用 LLM (DeepSeek) 做 8-way 意图分类。"""
    from agent.orchestrate.classifier import classify_intent_llm
    from agent.llm import create_llm

    llm = create_llm(temperature=0, max_tokens=10, request_timeout=10)
    return await classify_intent_llm(text, llm)


# ═══════════════════════════════════════════════════════════════════════════════
# 评测主流程
# ═══════════════════════════════════════════════════════════════════════════════


async def evaluate(
    queries: list[dict],
    baseline: str = "llm",
) -> dict:
    """跑完整评测。

    Args:
        queries: [{"text": ..., "label": ...}]。
        baseline: "llm" 或 "keyword"。

    Returns:
        包含所有指标的 dict。
    """
    labels = sorted(set(q["label"] for q in queries))
    y_true: list[str] = []
    y_pred: list[str] = []
    errors: list[dict] = []

    for i, q in enumerate(queries):
        text = q["text"]
        true_label = q["label"]

        if baseline == "keyword":
            pred_label = _classify_keyword(text)
        else:
            pred_label = await _classify_llm(text)

        y_true.append(true_label)
        y_pred.append(pred_label)

        status = "✓" if true_label == pred_label else "✗"
        if true_label != pred_label:
            errors.append({
                "text": text,
                "true": true_label,
                "predicted": pred_label,
            })

        if (i + 1) % 20 == 0 or i == len(queries) - 1:
            logger.info("  进度: %d/%d", i + 1, len(queries))

    from eval.metrics import accuracy, confusion_matrix, macro_f1, per_class_f1

    return {
        "baseline": baseline,
        "total": len(queries),
        "correct": sum(1 for t, p in zip(y_true, y_pred) if t == p),
        "accuracy": round(accuracy(y_true, y_pred), 4),
        "macro_f1": round(macro_f1(y_true, y_pred, labels), 4),
        "per_class": per_class_f1(y_true, y_pred, labels),
        "confusion_matrix": confusion_matrix(y_true, y_pred, labels),
        "errors": errors,
        "y_true": y_true,
        "y_pred": y_pred,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 报告输出
# ═══════════════════════════════════════════════════════════════════════════════


def print_report(results: dict) -> None:
    """打印评测报告到 stdout。"""
    print("\n" + "=" * 60)
    print(f"  意图分类器评测 — baseline={results['baseline']}")
    print("=" * 60)
    print(f"  总样本: {results['total']}")
    print(f"  正确数: {results['correct']}")
    print(f"  准确率: {results['accuracy']:.2%}")
    print(f"  Macro F1: {results['macro_f1']:.4f}")
    print()

    print("  Per-class 指标:")
    print(f"  {'类别':<15} {'Precision':>10} {'Recall':>10} {'F1':>10} {'Support':>8}")
    print("  " + "-" * 55)
    for label, m in results["per_class"].items():
        print(
            f"  {label:<15} {m['precision']:>10.4f} {m['recall']:>10.4f} "
            f"{m['f1']:>10.4f} {m['support']:>8}"
        )

    print("\n  混淆矩阵 (行=Actual, 列=Predicted):")
    cm = results["confusion_matrix"]
    labels = sorted(cm.keys())
    header = "         " + "".join(f"{l:>8}" for l in labels)
    print(header)
    for row_label in labels:
        row = f"  {row_label:<7}"
        for col_label in labels:
            count = cm[row_label][col_label]
            row += f"{count:>8}"
        print(row)

    if results["errors"]:
        print(f"\n  错误样本 ({len(results['errors'])} 条):")
        for e in results["errors"][:15]:
            print(f"    ✗ '{e['text'][:40]}' → true={e['true']} pred={e['predicted']}")

    print()


def save_report(results: dict, output_path: str) -> None:
    """保存 Markdown 评测报告。"""
    lines: list[str] = []
    lines.append(f"# 意图分类器评测报告")
    lines.append(f"\n**日期**: 2026-08-01 | **Baseline**: {results['baseline']} | **样本数**: {results['total']}")
    lines.append(f"\n## 总览\n")
    lines.append(f"| 指标 | 值 |")
    lines.append(f"|------|----|")
    lines.append(f"| Accuracy | {results['accuracy']:.2%} |")
    lines.append(f"| Macro F1 | {results['macro_f1']:.4f} |")
    lines.append(f"| Correct / Total | {results['correct']} / {results['total']} |")

    lines.append(f"\n## Per-class 指标\n")
    lines.append(f"| 类别 | Precision | Recall | F1 | Support |")
    lines.append(f"|------|-----------|--------|----|---------|")
    for label, m in results["per_class"].items():
        lines.append(f"| {label} | {m['precision']:.4f} | {m['recall']:.4f} | {m['f1']:.4f} | {m['support']} |")

    lines.append(f"\n## 混淆矩阵\n")
    cm = results["confusion_matrix"]
    labels = sorted(cm.keys())
    header = "| | " + " | ".join(labels) + " |"
    sep = "|---|" + "|".join(["---" for _ in labels]) + "|"
    lines.append(header)
    lines.append(sep)
    for row_label in labels:
        row = f"| {row_label} | " + " | ".join(str(cm[row_label][l]) for l in labels) + " |"
        lines.append(row)

    if results["errors"]:
        lines.append(f"\n## 错误样本 (Top 15)\n")
        for e in results["errors"][:15]:
            lines.append(f"- ✗ `{e['text'][:60]}` → **true={e['true']}** pred={e['predicted']}")

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_text("\n".join(lines), encoding="utf-8")
    logger.info("报告已保存: %s", output_path)


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(description="意图分类器评测")
    parser.add_argument(
        "--data", default="eval/data/intent_queries.json",
        help="评测数据集路径 (默认: eval/data/intent_queries.json)",
    )
    parser.add_argument(
        "--baseline", default="llm", choices=["llm", "keyword"],
        help="基线类型: llm (DeepSeek分类器) 或 keyword (关键词规则)",
    )
    parser.add_argument(
        "--output", default=None,
        help="Markdown 报告输出路径 (默认: 仅打印到 stdout)",
    )
    args = parser.parse_args()

    # 加载数据
    data_path = Path(args.data)
    if not data_path.exists():
        logger.error("数据集不存在: %s", data_path)
        sys.exit(1)

    data = json.loads(data_path.read_text(encoding="utf-8"))
    queries = data["queries"]
    logger.info("加载 %d 条评测查询 (baseline=%s)", len(queries), args.baseline)

    # 跑评测
    import asyncio
    results = asyncio.run(evaluate(queries, baseline=args.baseline))

    # 输出
    print_report(results)
    if args.output:
        save_report(results, args.output)


if __name__ == "__main__":
    main()
