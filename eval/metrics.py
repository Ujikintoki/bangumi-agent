"""
Eval 共享指标模块

纯函数，无状态，无外部依赖。每个函数实现一个标准评测指标。
所有指标公式注释中注明来源，确保面试时可溯源。

指标来源:
  - ndcg_at_k: SIGIR 经典 (Järvelin & Kekäläinen, 2002)
  - classification_report: 标准 sklearn-style 多分类指标

⚠️ 这里【只放有人在用的函数】。Recall@K / Precision@K / MRR 不在此列 ——
   轴 2 的 `_compute_metrics` 一次算出 `found` 再派生这三个数，不需要库版本。
   （2026-09-12 删掉了三个零消费者的实现，别再把它们加回来。）
"""

from __future__ import annotations


def ndcg_at_k(
    retrieved_ids: list[str],
    ground_truth: dict[str, int],
    k: int,
) -> float:
    """归一化折损累计增益 @ k。

    DCG@k = Σ(rel_i / log2(i+1))
    nDCG@k = DCG@k / IDCG@k

    其中 rel_i 是第 i 位的相关度得分，IDCG 是理想排序下的 DCG。

    来源: Järvelin, K. & Kekäläinen, J. (2002).
          "Cumulated gain-based evaluation of IR techniques"

    Args:
        retrieved_ids: 检索结果 ID 列表（已排序）。
        ground_truth: {entity_id: relevance_score} 映射。
            relevance_score: 3=高度相关, 2=中度, 1=低度, 0=不相关。
        k: 截断位置。

    Returns:
        [0, 1] 浮点数。ground_truth 为空或全 0 时返回 0.0。
    """
    if not ground_truth or k <= 0:
        return 0.0

    def _dcg(ids: list[str]) -> float:
        total = 0.0
        for i, rid in enumerate(ids[:k], start=1):
            rel = ground_truth.get(rid, 0)
            total += rel / (__import__("math").log2(i + 1))
        return total

    dcg = _dcg(retrieved_ids)

    # IDCG: 理想排序——所有 ground truth 条目按相关度降序排列
    ideal_order = sorted(ground_truth.values(), reverse=True)
    ideal_dcg = 0.0
    for i, rel in enumerate(ideal_order[:k], start=1):
        ideal_dcg += rel / (__import__("math").log2(i + 1))

    return dcg / ideal_dcg if ideal_dcg > 0 else 0.0


def accuracy(y_true: list[str], y_pred: list[str]) -> float:
    """多分类准确率。

    accuracy = correct / total

    Args:
        y_true: 真实标签列表。
        y_pred: 预测标签列表。

    Returns:
        [0, 1] 浮点数。
    """
    if not y_true:
        return 0.0
    correct = sum(1 for t, p in zip(y_true, y_pred) if t == p)
    return correct / len(y_true)


def per_class_f1(y_true: list[str], y_pred: list[str], labels: list[str]) -> dict[str, dict[str, float]]:
    """per-class precision / recall / F1。

    返回格式: {label: {"precision": ..., "recall": ..., "f1": ..., "support": ...}}

    Args:
        y_true: 真实标签列表。
        y_pred: 预测标签列表。
        labels: 所有类别标签。

    Returns:
        {label: metrics_dict}。
    """
    result: dict[str, dict[str, float]] = {}
    for label in labels:
        tp = sum(1 for t, p in zip(y_true, y_pred) if t == label and p == label)
        fp = sum(1 for t, p in zip(y_true, y_pred) if t != label and p == label)
        fn = sum(1 for t, p in zip(y_true, y_pred) if t == label and p != label)
        support = sum(1 for t in y_true if t == label)

        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

        result[label] = {
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "support": support,
        }
    return result


def confusion_matrix(
    y_true: list[str], y_pred: list[str], labels: list[str]
) -> dict[str, dict[str, int]]:
    """混淆矩阵。

    返回格式: {actual_label: {predicted_label: count}}

    Args:
        y_true: 真实标签列表。
        y_pred: 预测标签列表。
        labels: 所有类别标签。

    Returns:
        二维混淆矩阵 dict。
    """
    matrix: dict[str, dict[str, int]] = {label: {l: 0 for l in labels} for label in labels}
    for t, p in zip(y_true, y_pred):
        if t in matrix and p in matrix[t]:
            matrix[t][p] += 1
    return matrix


def macro_f1(y_true: list[str], y_pred: list[str], labels: list[str]) -> float:
    """Macro-averaged F1 score。

    Args:
        y_true: 真实标签列表。
        y_pred: 预测标签列表。
        labels: 所有类别标签。

    Returns:
        [0, 1] 浮点数。
    """
    per_class = per_class_f1(y_true, y_pred, labels)
    f1s = [m["f1"] for m in per_class.values() if m["support"] > 0]
    return sum(f1s) / len(f1s) if f1s else 0.0
