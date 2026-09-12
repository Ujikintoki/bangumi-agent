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
from datetime import date
from pathlib import Path

# 确保项目根目录在 Python path 中（从 eval/ 目录 import 需要）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("eval.classifier")

# ═══════════════════════════════════════════════════════════════════════════════
# 关键词基线分类器（7-intent）
# ═══════════════════════════════════════════════════════════════════════════════

_KEYWORD_RULES: list[tuple[str, str]] = [
    # (关键词/正则, intent) — 按优先级排列
    # 注意：基线是"朴素对比锚"，允许与黄金标签不一致；"最近"等歧义词故意不映射，交给 LLM 分类器。
    # chat: 问候 / 情绪 / 应答词 / 中文互联网情绪梗（产品定位：用户用梗说话）
    (
        "你好|嗨|早上好|晚上好|再见|拜拜|谢谢|哈哈|在吗|你是谁|你叫什么|"
        "^(在|哦|嗯|md|草|艹|666|555)$|"
        "hhh|2333|哈哈哈|绷不住了|蚌埠住了|破防|笑死|笑不活了|典！|典中典|"
        "emo|我破防|集美|hxd|好兄弟|家人们",
        "chat",
    ),
    # profile: @ 用户 / 品味分析
    (r"@\S+|品味|评分习惯|看番轨迹|收藏统计|追番|口味匹配|评分分布|追番进度", "profile"),
    # fetch: 查确定实体的属性
    (
        "评分多少|评分是|打几分|多少分|排名|查一下|帮我查|查查|是谁|是什么类型|是啥类型|"
        "详情|系列|版本|剧情|配过|导演|声优|讲了什么|讲什么|资源|第几季|续作",
        "fetch",
    ),
    # discuss: 观点 / 评价 / 评价向梗
    (
        "过誉|高估|太烂|烂尾|喂屎|垃圾|不如以前|商业化|艺术性|比.*厉害|吹|吹爆|"
        "踩一捧一|锐评|瑞平|什么水平|什么地位|什么咖位|什么档位|封神|yyds|永远滴神|"
        "崩了|崩坏|这评分|这波|神作但|认真的吗",
        "discuss",
    ),
    # realtime: 时效信息（置于 explore 之前："这季...好看" 的时效信号优先于 "好看"）
    (
        "这季|今季|这周|本周|今天|昨晚|昨天|更新|定档|排期|霸权|黑马|热议|最热|"
        "评分上升|新番表|开播|暴死|出圈|破圈|月番",
        "realtime",
    ),
    # explore: 推荐 / 发现（含内容类型梗：纯爱/NTR/黑深残）
    (
        "推荐|有没有|有啥|类似|想看|求.*番|求安利|安利|种草|冷门|治愈|致郁|"
        "纯爱|黑深残|牛头人|ntr|好看|值得一看|活着真好|入坑|补番|适合新人|适合入坑|"
        "求几部|有什么好的|哪些.*值得",
        "explore",
    ),
]

# 单字/短作品名白名单——已知的 ACGN 作品名缩写/裸标题 → fetch
_KNOWN_SHORT_TITLES = frozenset({
    "eva", "86", "k", "c", "fz", "ubw", "ab", "lb",
    "cl", "wa", "sa", "dc", "ef", "ac", "sd",
    "钢炼", "巨人", "芙莉莲", "凉宫", "银魂", "夏目", "石头门",
})


def _classify_keyword(text: str) -> str:
    """基于关键词+正则的 7-intent 分类（基线）。

    Args:
        text: 用户输入文本。

    Returns:
        intent 字符串。
    """
    text_stripped = text.strip()
    text_lower = text_stripped.lower()

    # 短文本检查: 单字或极短的已知作品名 → fetch
    if len(text_stripped) <= 3 and text_lower in _KNOWN_SHORT_TITLES:
        return "fetch"

    # 纯标点 → fallback
    if all(c in "。！？.…,，、～~ " for c in text_stripped):
        return "fallback"

    for pattern, intent in _KEYWORD_RULES:
        import re
        if re.search(pattern, text_stripped):
            return intent

    # 兜底: 短文本 → fallback，长文本 → fetch（沿用旧基线"默认查数据"哲学）
    if len(text_stripped) <= 2:
        return "fallback"
    return "fetch"


# ═══════════════════════════════════════════════════════════════════════════════
# LLM 分类器
# ═══════════════════════════════════════════════════════════════════════════════


async def _classify_llm(text: str) -> str:
    """使用 LLM (DeepSeek) 做 7-intent 意图分类。

    ``classify_intent_llm`` 返回 ``(intent, confidence)`` 元组，此处必须解包
    成字符串再返回——下游用 ``true_label == pred_label`` 比较，若直接透传
    元组则恒为 False，准确率恒 0（P4）。
    """
    from agent.nodes.classify import classify_intent_llm
    from agent.llm import create_llm

    llm = create_llm(temperature=0, max_tokens=10, request_timeout=10)
    intent, _ = await classify_intent_llm(text, llm)
    return intent


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
    lines.append(
        f"\n**日期**: {date.today().isoformat()} | "
        f"**Baseline**: {results['baseline']} | **样本数**: {results['total']}"
    )
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
        lines.append(f"\n## 冲突清单：预测 ≠ 黄金标签 (Top 25)\n")
        lines.append(f"\n> 冲突 ≠ 分类器错：也可能黄金标签存疑。逐条能说出'产品应该这样分'才保留。\n")
        for e in results["errors"][:25]:
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
        "--data", default="eval/data/intent_queries_v7.json",
        help="评测数据集路径 (默认: eval/data/intent_queries_v7.json)",
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
