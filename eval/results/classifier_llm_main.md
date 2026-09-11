# 意图分类器评测报告

**日期**: 2026-08-01 | **Baseline**: llm | **样本数**: 102

## 总览

| 指标 | 值 |
|------|----|
| Accuracy | 89.22% |
| Macro F1 | 0.8839 |
| Correct / Total | 91 / 102 |

## Per-class 指标

| 类别 | Precision | Recall | F1 | Support |
|------|-----------|--------|----|---------|
| chat | 1.0000 | 0.9722 | 0.9859 | 36 |
| discuss | 1.0000 | 0.9091 | 0.9524 | 11 |
| explore | 0.6364 | 1.0000 | 0.7778 | 14 |
| fallback | 1.0000 | 0.7500 | 0.8571 | 8 |
| fetch | 0.7500 | 0.6000 | 0.6667 | 15 |
| profile | 1.0000 | 1.0000 | 1.0000 | 8 |
| realtime | 1.0000 | 0.9000 | 0.9474 | 10 |

## 混淆矩阵

| | chat | discuss | explore | fallback | fetch | profile | realtime |
|---|---|---|---|---|---|---|---|
| chat | 35 | 0 | 0 | 0 | 1 | 0 | 0 |
| discuss | 0 | 10 | 1 | 0 | 0 | 0 | 0 |
| explore | 0 | 0 | 14 | 0 | 0 | 0 | 0 |
| fallback | 0 | 0 | 0 | 6 | 2 | 0 | 0 |
| fetch | 0 | 0 | 6 | 0 | 9 | 0 | 0 |
| profile | 0 | 0 | 0 | 0 | 0 | 8 | 0 |
| realtime | 0 | 0 | 1 | 0 | 0 | 0 | 9 |

## 冲突清单：预测 ≠ 黄金标签 (Top 25)


> 冲突 ≠ 分类器错：也可能黄金标签存疑。逐条能说出'产品应该这样分'才保留。

- ✗ `EVA是什么类型的动画` → **true=fetch** pred=explore
- ✗ `新海诚和宫崎骏的风格有什么区别` → **true=discuss** pred=explore
- ✗ `花泽香菜配过哪些角色` → **true=fetch** pred=explore
- ✗ `刚才说的那部具体讲讲` → **true=fetch** pred=explore
- ✗ `新房昭之是谁` → **true=fetch** pred=explore
- ✗ `这部作品还有哪些系列` → **true=fetch** pred=explore
- ✗ `它还出了哪些版本` → **true=fetch** pred=explore
- ✗ `最近评分上升最快的是哪部` → **true=realtime** pred=explore
- ✗ `123` → **true=fallback** pred=fetch
- ✗ `md` → **true=chat** pred=fetch
- ✗ `0` → **true=fallback** pred=fetch