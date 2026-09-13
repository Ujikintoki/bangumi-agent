"""
Eval 共享指标模块

纯函数，无状态，无外部依赖。每个函数实现一个标准评测指标。
所有指标公式注释中注明来源，确保面试时可溯源。

指标来源:
  - ndcg_at_k: SIGIR 经典 (Järvelin & Kekäläinen, 2002)
  - cohen_kappa: Cohen (1960) + Landis & Koch (1977) 分档
  - classification_report: 标准 sklearn-style 多分类指标

⚠️ 这里【只放有人在用的函数】。Recall@K / Precision@K / MRR 不在此列 ——
   轴 2 的 `_compute_metrics` 一次算出 `found` 再派生这三个数，不需要库版本。
   （2026-09-12 删掉了三个零消费者的实现，别再把它们加回来。）
"""

from __future__ import annotations


# Landis & Koch (1977) 的 κ 分档。别把它当判据用 —— 0.61 和 0.60 没有实质差别，
# 它只是给数字一个说法。真正的判据是「分歧落在哪一类样本上」，见 generate_rag_gt
# 的报告里按查询风格分层的 κ。
_KAPPA_BANDS = (
    (0.81, "almost perfect"),
    (0.61, "substantial"),
    (0.41, "moderate"),
    (0.21, "fair"),
    (0.00, "slight"),
)


def cohen_kappa(y_a: list[str], y_b: list[str]) -> dict:
    """两个评判者对同一批样本的 Cohen's κ —— 一致性，扣除「碰巧一致」。

    po = 观察一致率 = 一致条数 / 总条数
    pe = 按两人各自的边际分布独立时，期望的一致率
    κ  = (po − pe) / (1 − pe)

    为什么要 κ 而不是简单一致率：判官判「不相关」占九成时，一个永远判「不相关」的
    废物判官也能拿 90% 一致率。κ 把这份白送的一致扣掉。

    来源: Cohen, J. (1960). "A coefficient of agreement for nominal scales",
          Educational and Psychological Measurement, 20(1), 37-46.

    Args:
        y_a: 评判者 A（本项目里是 LLM 判官）的标签列表。
        y_b: 评判者 B（本项目里是人工）的标签列表，与 y_a 逐条对齐。

    Returns:
        `{"n", "po", "pe", "kappa", "band", "matrix"}`。
        kappa 在 pe == 1（两人各自都只用一个标签）时是 `None` —— 此时 κ 无定义，
        不是 0 也不是 1。band 为 "undefined"。

    Raises:
        ValueError: 两个列表长度不等。**不静默截断** —— 错位的 κ 是个看起来完全
            正常的数字，比报错危险得多。
    """
    if len(y_a) != len(y_b):
        raise ValueError(f"长度不等：{len(y_a)} vs {len(y_b)} —— 逐条对齐是前提")

    n = len(y_a)
    if n == 0:
        return {"n": 0, "po": None, "pe": None, "kappa": None,
                "band": "undefined", "matrix": {}}

    labels = sorted(set(y_a) | set(y_b))
    matrix = {a: {b: 0 for b in labels} for a in labels}
    for a, b in zip(y_a, y_b):
        matrix[a][b] += 1

    po = sum(matrix[l][l] for l in labels) / n
    pe = sum((sum(matrix[l].values()) / n) * (sum(matrix[a][l] for a in labels) / n)
             for l in labels)

    if pe >= 1.0:
        kappa, band = None, "undefined"
    else:
        kappa = (po - pe) / (1 - pe)
        band = next((name for lo, name in _KAPPA_BANDS if kappa >= lo), "poor")

    # ⚠ 只留 6 位、不做 4 位那种"好看"的舍入：报告层还会再格式化一次，
    # 两次舍入会叠出假精度（0.83146 → round4 → 0.8315 → "%.3f" → 0.832）。
    return {
        "n": n,
        "po": round(po, 6),
        "pe": round(pe, 6),
        "kappa": None if kappa is None else round(kappa, 6),
        "band": band,
        "matrix": matrix,
    }


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
