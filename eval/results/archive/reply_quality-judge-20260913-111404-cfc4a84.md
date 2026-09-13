# 轴 3 · 回复质量 — Tier 1 判定报告

**判定于** 2026-09-13T11:14:04+0800 | **样本** `reply_quality-20260913-111212-cfc4a84.json` | **录制于** 2026-09-13T11:13:42+0800 | **git** cfc4a84

> 生成/判定分离：本报告只读冻结样本，不重新生成 —— 同一份样本可反复判定。

> 隔离：user_id=eval-20260913-111212-<scenario_id> —— L2 召回按 user_id 检索（session_id 不参与查询），故每个场景开局都是零记忆

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

## Tier 1 指标（n=3）

| 指标 | 总体 | fast |
|---|---|---|
| 空回复 | 0.0% | 0.0% |
| 报错回复 | 0.0% | 0.0% |
| 硬截断触发 | 0.0% | 0.0% |
| render 降级 | 33.3% | 33.3% |
| 超软上限 | 0.0% | 0.0% |
| 超硬截断上界 | 0.0% | 0.0% |
| 有任何格式泄漏 | 33.3% | 33.3% |

**字数** p50=55 p95=111 max=111（软上限 {'fast': 200, 'deep': 350}，硬截断 {'fast': 280, 'deep': 480}）

**泄漏明细**：prompt脚手架×1

## 逐条

| 场景 | depth | 字数 | 硬截断 | 降级 | 泄漏 |
|---|---|---|---|---|---|
| `A1-常识日期` | fast | 45 |  |  |  |
| `A2-常识概念` | fast | 55 |  | ✓ | prompt脚手架 |
| `A3-一句话推荐` | fast | 111 |  |  |  |
