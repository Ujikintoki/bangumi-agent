# 意图分类器评测报告

**日期**: 2026-09-12 | **Baseline**: llm | **样本数**: 35

## 总览

| 指标 | 值 |
|------|----|
| Accuracy | 71.43% |
| Macro F1 | 0.7355 |
| Correct / Total | 25 / 35 |

## Per-class 指标

| 类别 | Precision | Recall | F1 | Support |
|------|-----------|--------|----|---------|
| chat | 0.6250 | 0.5556 | 0.5882 | 9 |
| discuss | 0.8000 | 0.5714 | 0.6667 | 7 |
| explore | 0.7143 | 1.0000 | 0.8333 | 5 |
| fallback | 0.5000 | 0.2500 | 0.3333 | 4 |
| fetch | 1.0000 | 1.0000 | 1.0000 | 3 |
| profile | 1.0000 | 1.0000 | 1.0000 | 3 |
| realtime | 0.5714 | 1.0000 | 0.7273 | 4 |

## 混淆矩阵

| | chat | discuss | explore | fallback | fetch | profile | realtime |
|---|---|---|---|---|---|---|---|
| chat | 5 | 1 | 0 | 0 | 0 | 0 | 3 |
| discuss | 2 | 4 | 0 | 1 | 0 | 0 | 0 |
| explore | 0 | 0 | 5 | 0 | 0 | 0 | 0 |
| fallback | 1 | 0 | 2 | 1 | 0 | 0 | 0 |
| fetch | 0 | 0 | 0 | 0 | 3 | 0 | 0 |
| profile | 0 | 0 | 0 | 0 | 0 | 3 | 0 |
| realtime | 0 | 0 | 0 | 0 | 0 | 0 | 4 |

## 冲突清单：预测 ≠ 黄金标签 (Top 25)


> 冲突 ≠ 分类器错：也可能黄金标签存疑。逐条能说出'产品应该这样分'才保留。

- ✗ `今天星期几` → **true=chat** pred=realtime
- ✗ `今天是几号` → **true=chat** pred=realtime
- ✗ `现在几点` → **true=chat** pred=realtime
- ✗ `续作` → **true=fallback** pred=explore
- ✗ `这个好看吗` → **true=fallback** pred=explore
- ✗ `帮我写一封邮件` → **true=fallback** pred=chat
- ✗ `为什么京阿尼这么强` → **true=chat** pred=discuss
- ✗ `新一集喂屎了` → **true=discuss** pred=chat
- ✗ `yyds，这作画` → **true=discuss** pred=chat
- ✗ `这波是什么水平` → **true=discuss** pred=fallback