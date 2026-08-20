# 意图分类器评测报告

**日期**: 2026-08-01 | **Baseline**: keyword | **样本数**: 35

## 总览

| 指标 | 值 |
|------|----|
| Accuracy | 51.43% |
| Macro F1 | 0.5063 |
| Correct / Total | 18 / 35 |

## Per-class 指标

| 类别 | Precision | Recall | F1 | Support |
|------|-----------|--------|----|---------|
| chat | 0.5000 | 0.2500 | 0.3333 | 8 |
| discuss | 1.0000 | 0.7500 | 0.8571 | 8 |
| explore | 0.6000 | 0.6000 | 0.6000 | 5 |
| fallback | 0.0000 | 0.0000 | 0.0000 | 4 |
| fetch | 0.1000 | 0.3333 | 0.1538 | 3 |
| profile | 1.0000 | 0.6667 | 0.8000 | 3 |
| realtime | 0.6667 | 1.0000 | 0.8000 | 4 |

## 混淆矩阵

| | chat | discuss | explore | fallback | fetch | profile | realtime |
|---|---|---|---|---|---|---|---|
| chat | 2 | 0 | 0 | 0 | 4 | 0 | 2 |
| discuss | 1 | 6 | 0 | 0 | 1 | 0 | 0 |
| explore | 0 | 0 | 3 | 0 | 2 | 0 | 0 |
| fallback | 0 | 0 | 2 | 0 | 2 | 0 | 0 |
| fetch | 0 | 0 | 0 | 2 | 1 | 0 | 0 |
| profile | 1 | 0 | 0 | 0 | 0 | 2 | 0 |
| realtime | 0 | 0 | 0 | 0 | 0 | 0 | 4 |

## 冲突清单：预测 ≠ 黄金标签 (Top 25)


> 冲突 ≠ 分类器错：也可能黄金标签存疑。逐条能说出'产品应该这样分'才保留。

- ✗ `今天星期几` → **true=chat** pred=realtime
- ✗ `今天是几号` → **true=chat** pred=realtime
- ✗ `现在几点` → **true=chat** pred=fetch
- ✗ `钢炼` → **true=fetch** pred=fallback
- ✗ `巨人` → **true=fetch** pred=fallback
- ✗ `EVA和进击的巨人哪个更神` → **true=explore** pred=fetch
- ✗ `2025年最值得看的动画` → **true=explore** pred=fetch
- ✗ `今晚吃什么` → **true=chat** pred=fetch
- ✗ `给我讲个冷笑话` → **true=chat** pred=fetch
- ✗ `续作` → **true=fallback** pred=fetch
- ✗ `这个好看吗` → **true=fallback** pred=explore
- ✗ `你上周推荐的那部好看吗` → **true=fallback** pred=explore
- ✗ `帮我写一封邮件` → **true=fallback** pred=fetch
- ✗ `为什么京阿尼这么强` → **true=discuss** pred=fetch
- ✗ `笑死，这评分认真的吗` → **true=discuss** pred=chat
- ✗ `@hxd 最近在追什么` → **true=profile** pred=chat
- ✗ `我也磕到了` → **true=chat** pred=fetch