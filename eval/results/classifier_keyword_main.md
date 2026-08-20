# 意图分类器评测报告

**日期**: 2026-08-01 | **Baseline**: keyword | **样本数**: 102

## 总览

| 指标 | 值 |
|------|----|
| Accuracy | 70.59% |
| Macro F1 | 0.7771 |
| Correct / Total | 72 / 102 |

## Per-class 指标

| 类别 | Precision | Recall | F1 | Support |
|------|-----------|--------|----|---------|
| chat | 1.0000 | 0.3611 | 0.5306 | 36 |
| discuss | 1.0000 | 0.8000 | 0.8889 | 10 |
| explore | 0.7857 | 0.7857 | 0.7857 | 14 |
| fallback | 0.7778 | 0.8750 | 0.8235 | 8 |
| fetch | 0.4211 | 1.0000 | 0.5926 | 16 |
| profile | 1.0000 | 1.0000 | 1.0000 | 8 |
| realtime | 0.7500 | 0.9000 | 0.8182 | 10 |

## 混淆矩阵

| | chat | discuss | explore | fallback | fetch | profile | realtime |
|---|---|---|---|---|---|---|---|
| chat | 13 | 0 | 0 | 2 | 18 | 0 | 3 |
| discuss | 0 | 8 | 2 | 0 | 0 | 0 | 0 |
| explore | 0 | 0 | 11 | 0 | 3 | 0 | 0 |
| fallback | 0 | 0 | 0 | 7 | 1 | 0 | 0 |
| fetch | 0 | 0 | 0 | 0 | 16 | 0 | 0 |
| profile | 0 | 0 | 0 | 0 | 0 | 8 | 0 |
| realtime | 0 | 0 | 1 | 0 | 0 | 0 | 9 |

## 冲突清单：预测 ≠ 黄金标签 (Top 25)


> 冲突 ≠ 分类器错：也可能黄金标签存疑。逐条能说出'产品应该这样分'才保留。

- ✗ `今天天气不错` → **true=chat** pred=realtime
- ✗ `好的我知道了` → **true=chat** pred=fetch
- ✗ `你真有意思` → **true=chat** pred=fetch
- ✗ `什么是三集定律` → **true=chat** pred=fetch
- ✗ `动画的制作流程是怎样的` → **true=chat** pred=fetch
- ✗ `什么是总集篇` → **true=chat** pred=fetch
- ✗ `声优是怎么选出来的` → **true=chat** pred=fetch
- ✗ `动画的BD和TV版有什么区别` → **true=chat** pred=fetch
- ✗ `什么是OVA` → **true=chat** pred=fetch
- ✗ `为什么有些动画没有第二季` → **true=chat** pred=fetch
- ✗ `Bangumi的评分系统是怎么运作的` → **true=chat** pred=fetch
- ✗ `具体说说第一部的剧情` → **true=explore** pred=fetch
- ✗ `和乒乓风格相似的动画` → **true=explore** pred=fetch
- ✗ `这季有什么好看的动画` → **true=realtime** pred=explore
- ✗ `2024年最受期待的新番` → **true=explore** pred=fetch
- ✗ `现在的动画都不如以前的好看了` → **true=discuss** pred=explore
- ✗ `评分高不代表好看，EVA就是例子` → **true=discuss** pred=explore
- ✗ `好累` → **true=chat** pred=fallback
- ✗ `烦死了` → **true=chat** pred=fetch
- ✗ `最近什么都看不进去了` → **true=chat** pred=fetch
- ✗ `好无聊` → **true=chat** pred=fetch
- ✗ `今天心情不好` → **true=chat** pred=realtime
- ✗ `失恋了` → **true=chat** pred=fetch
- ✗ `好开心` → **true=chat** pred=fetch
- ✗ `郁闷` → **true=chat** pred=fallback