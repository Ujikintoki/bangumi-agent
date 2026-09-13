# 轴 3 · 回复质量 — Tier 1 判定报告

**判定于** 2026-09-13T11:31:27+0800 | **样本** `reply_quality-20260913-111412-cfc4a84.json` | **录制于** 2026-09-13T11:24:42+0800 | **git** cfc4a84

> 生成/判定分离：本报告只读冻结样本，不重新生成 —— 同一份样本可反复判定。

> 隔离：user_id=eval-20260913-111412-<scenario_id> —— L2 召回按 user_id 检索（session_id 不参与查询），故每个场景开局都是零记忆

## 设置快照

| 项 | 值 |
|---|---|
| `MEMORY_ENABLED` | `True` |
| `MEMORY_RECALL_TOP_K` | `5` |
| `MEMORY_RECALL_THRESHOLD` | `0.5` |
| `MEMORY_RECENCY_FALLBACK_THRESHOLD` | `0.6` |
| `MEMORY_TIME_DECAY_HALF_LIFE_DAYS` | `14` |
| `LLM_MODEL` | `deepseek-v4-flash` |
| `LLM_TEMPERATURE` | `0.3` |
| `EMBEDDING_MODEL` | `embedding-2` |

## Tier 1 指标（n=21）

| 指标 | 总体 | deep | fast |
|---|---|---|---|
| 空回复 | 0.0% | 0.0% | 0.0% |
| 报错回复 | 9.5% | 50.0% | 5.3% |
| 硬截断触发 | 0.0% | 0.0% | 0.0% |
| render 降级 | 9.5% | 0.0% | 10.5% |
| 超软上限 | 4.8% | 0.0% | 5.3% |
| 超硬截断上界 | 0.0% | 0.0% | 0.0% |
| 有任何格式泄漏 | 4.8% | 0.0% | 5.3% |

**字数** p50=98 p95=222 max=305（软上限 {'fast': 200, 'deep': 350, 'bangumi_kawaii/fast': 250}，硬截断 {'fast': 280, 'deep': 480}）

**泄漏明细**：prompt脚手架×1

## 未录制（9 条）

- `C2-查评分` — 需要 Bangumi API 工具（--offline 跳过）
- `C3-人物查询` — 需要 Bangumi API 工具（--offline 跳过）
- `C4-找相似` — 需要 Bangumi API 工具（--offline 跳过）
- `C6-放送排期` — 需要 Bangumi API 工具（--offline 跳过）
- `D3-评分动态` — 需要 Bangumi API 工具（--offline 跳过）
- `D4-社区热点` — 需要 Bangumi API 工具（--offline 跳过）
- `E1-deep讨论` — 需要 Bangumi API 工具（--offline 跳过）
- `E2-歧义裸标题` — 需要 Bangumi API 工具（--offline 跳过）
- `E6-本季霸权` — 需要 Bangumi API 工具（--offline 跳过）

## 逐条

| 场景 | 人格 | depth | 字数 | 软上限 | 报错 | 超软 | 硬截断 | 降级 | 泄漏 |
|---|---|---|---|---|---|---|---|---|---|
| `A1-常识日期` | bangumi | fast | 72 | 200 |  |  |  |  |  |
| `A2-常识概念` | bangumi | fast | 204 | 200 |  | ✓ |  |  |  |
| `A3-一句话推荐` | bangumi | fast | 75 | 200 |  |  |  |  |  |
| `A4-列表格式` | bangumi | fast | 176 | 200 |  |  |  |  |  |
| `A5-deep深入` | bangumi | deep | 27 | 350 | ✓ |  |  |  |  |
| `A6-无意义输入` | bangumi | fast | 88 | 200 |  |  |  |  |  |
| `B1-bangumi人格` | bangumi | fast | 139 | 200 |  |  |  |  |  |
| `B2-kawaii人格` | bangumi_kawaii | fast | 222 | 250 |  |  |  |  |  |
| `B3-neutral人格` | neutral | fast | 92 | 200 |  |  |  |  |  |
| `B4-立场评价` | bangumi | fast | 19 | 200 | ✓ |  |  |  |  |
| `B5-反馈修正` | bangumi | fast | 98 | 200 |  |  |  |  |  |
| `B6-闲聊回应` | bangumi | fast | 60 | 200 |  |  |  | ✓ | prompt脚手架 |
| `C1-禁工具常识` | bangumi | fast | 79 | 200 |  |  |  |  |  |
| `C5-deep必调工具` | bangumi | deep | 305 | 350 |  |  |  |  |  |
| `D1-无上下文指代` | bangumi | fast | 112 | 200 |  |  |  |  |  |
| `D2-用户画像` | bangumi | fast | 149 | 200 |  |  |  |  |  |
| `D5-社区评价` | bangumi | fast | 39 | 200 |  |  |  | ✓ |  |
| `D6-我的追番` | bangumi | fast | 139 | 200 |  |  |  |  |  |
| `E3-梗找番` | bangumi | fast | 157 | 200 |  |  |  |  |  |
| `E4-梗讨论` | bangumi | fast | 188 | 200 |  |  |  |  |  |
| `E5-能力外` | bangumi | fast | 78 | 200 |  |  |  |  |  |
