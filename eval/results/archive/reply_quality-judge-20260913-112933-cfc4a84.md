# 轴 3 · 回复质量 — Tier 1 判定报告

**判定于** 2026-09-13T11:29:33+0800 | **样本** `reply_quality-20260913-111412-cfc4a84.json` | **录制于** 2026-09-13T11:24:42+0800 | **git** cfc4a84

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
| 报错回复 | 4.8% | 0.0% | 5.3% |
| 硬截断触发 | 0.0% | 0.0% | 0.0% |
| render 降级 | 9.5% | 0.0% | 10.5% |
| 超软上限 | 9.5% | 0.0% | 10.5% |
| 超硬截断上界 | 0.0% | 0.0% | 0.0% |
| 有任何格式泄漏 | 4.8% | 0.0% | 5.3% |

**字数** p50=98 p95=222 max=305（软上限 {'fast': 200, 'deep': 350}，硬截断 {'fast': 280, 'deep': 480}）

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

| 场景 | depth | 字数 | 硬截断 | 降级 | 泄漏 |
|---|---|---|---|---|---|
| `A1-常识日期` | fast | 72 |  |  |  |
| `A2-常识概念` | fast | 204 |  |  |  |
| `A3-一句话推荐` | fast | 75 |  |  |  |
| `A4-列表格式` | fast | 176 |  |  |  |
| `A5-deep深入` | deep | 27 |  |  |  |
| `A6-无意义输入` | fast | 88 |  |  |  |
| `B1-bangumi人格` | fast | 139 |  |  |  |
| `B2-kawaii人格` | fast | 222 |  |  |  |
| `B3-neutral人格` | fast | 92 |  |  |  |
| `B4-立场评价` | fast | 19 |  |  |  |
| `B5-反馈修正` | fast | 98 |  |  |  |
| `B6-闲聊回应` | fast | 60 |  | ✓ | prompt脚手架 |
| `C1-禁工具常识` | fast | 79 |  |  |  |
| `C5-deep必调工具` | deep | 305 |  |  |  |
| `D1-无上下文指代` | fast | 112 |  |  |  |
| `D2-用户画像` | fast | 149 |  |  |  |
| `D5-社区评价` | fast | 39 |  | ✓ |  |
| `D6-我的追番` | fast | 139 |  |  |  |
| `E3-梗找番` | fast | 157 |  |  |  |
| `E4-梗讨论` | fast | 188 |  |  |  |
| `E5-能力外` | fast | 78 |  |  |  |
