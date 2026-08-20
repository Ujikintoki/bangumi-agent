# Eval 实施计划 v1 — 操作级细节

> 2026-08-17 | 承接 `agent-eval-blueprint.md`（决策手册）与 `intent-v7-relabeling.md`（标注报告）
> 原则：先冻结数据 → 跑出两个基线 → 建立回归机制。每个 Phase 都有文件级操作与完成标准。

---

## Phase A — 冻结轴 1 数据集（今天，~1h，需 owner 配合）

**操作清单：**

1. 我生成复核表 `eval/data/review_sheet_19.md`：19 条 flagged（14 条与 `_INTENT_ALIASES` 差异 + 5 条语境敏感），每行 = `条目 | 我的标注 | 别名表会给 | 理由 | 你批注（保持/修改/不确定）`
2. 你批注后我执行：
   - "保持/修改" → 更新 `eval/data/intent_queries_v7.json`
   - "不确定" → 移入新文件 `eval/data/intent_boundary.json`（同 schema，加 `"reason"` 字段）
3. 冻结提交（此后改数据集 = 开新版本，不再直接改）：
   ```bash
   git add eval/data/ docs/Eval/intent-v7-relabeling.md
   git commit -m "data(eval): 冻结轴1数据集 v7（核心N / 边界M / 对抗20）"
   ```
4. 校验：JSON 合法性 + 标签分布脚本（7 intent 全覆盖断言）

**完成标准**：三个 JSON 入库；主集只含 owner 90% 确定的条目。

---

## Phase B — `eval_classifier.py` 对齐 7-intent + 第一份基线（0.5-1d）

**修改点 1：`_KEYWORD_RULES` 重写为 7-intent 输出（顺序即优先级）：**

```
chat:     "你好|嗨|早上好|晚上好|再见|拜拜|谢谢|哈哈|在吗|哦|嗯|累|烦|无聊|难过|压力|失恋|开心|郁闷|空虚|感动|看不进去|你是谁|你叫什么"
fetch:    "评分|排名|查一下|帮我查|是谁|是什么类型|详情|系列|版本|剧情|配过|导演|声优"  + 裸标题白名单(short titles) → fetch
explore:  "推荐|有没有|类似|想看|找?番|冷门|治愈|致郁|好看|值得一看|活着真好|推荐几部"
discuss:  "过誉|高估|太烂|垃圾|不如以前|商业化|艺术性|比.*厉害|吹|普通水准"
realtime: "这季|今季|这周|本周|今天|更新|定档|排期|霸权|黑马|热议|最热|评分上升"
profile:  r"@\S+|品味|评分习惯|看番轨迹|收藏|追番|口味匹配"
兜底 → fallback
```

注意：**"最近"不映射**（歧义，交由 LLM 分类器）；`_KNOWN_SHORT_TITLES` 白名单保留 → fetch。

**修改点 2：** 报告增加"冲突清单"节——LLM 预测 ≠ 黄金标签的条目明细（供 owner 判断是标注问题还是分类器问题）。

**命令（三次运行，三个数据集各一份报告）：**
```bash
python eval/eval_classifier.py --data eval/data/intent_queries_v7.json --output results/classifier_main.md
python eval/eval_classifier.py --data eval/data/intent_adversarial.json --output results/classifier_adversarial.md
python eval/eval_classifier.py --data eval/data/intent_boundary.json --output results/classifier_boundary.md
```

**完成标准**：三个 report 生成；accuracy/per-class F1/混淆矩阵/冲突清单齐全；P0#3（常识误分类）有首个数字。

---

## Phase C — e2e runner v0（1-2d，主力工程）

**文件 1：`eval/data/e2e_scenarios.json`（30-45 条，schema）：**
```json
{
  "id": "E4-常识日期",
  "message": "今天星期几",
  "depth": "fast",
  "output_style": "bangumi",
  "expect_intent": "chat",
  "forbid_tools": ["*"],
  "require_tools": [],
  "word_range": [1, 280],
  "content_points": [],
  "notes": "P0#3 回归用例；chat 意图禁止任何工具"
}
```
`"*"` 表示全部工具禁止；`require_tools` 非空时断言工具子集被调用。从 45 场景迁移：A 输出质量 8 / B 人格 16 / C 工具策略 7 / D 真实场景 8 / E 边界 10。

**文件 2：`eval/run_e2e.py` 主流程：**

1. 加载场景 → `httpx.AsyncClient(transport=ASGITransport(app=app))` POST `/chat`（走真实端点，含中间件；`DEV_MODE=true` 拿 telemetry 的 token/节点耗时）
2. 每条确定性断言（**无 LLM-judge**）：
   - `query_intent == expect_intent`
   - `tools_used ∩ forbid_tools == ∅`（含 `"*"` 情况 = 一条工具都不许调）
   - `require_tools ⊆ tools_used`
   - `len(reply) ∈ word_range`
   - 无 error 降级（`reply` 不含降级文案、`error_flag` 为 False）
3. 统计输出：总通过率 + 按 intent 分组 + 按 A-E 维度分组 + 延迟 p50/p95 + token 汇总 → 成本估算（价格常量表）
4. 归档：`results/e2e-<date>-<hash>.json` + `.md`

**v0 已知限制（写进报告头）：** Bangumi API 真实调用（站点数据漂移 → 断言只测行为不测内容数值）；LLM 非确定 → 通过率有抖动，看趋势不看单次。

**命令：** `python -m eval.run_e2e`

**完成标准**：一次运行产出 ② 任务完成 + ③ 工具正确性 + ④ 轨迹 + ⑨ 成本延迟全部数字。

---

## Phase D — 回归入口（0.5d，可与 C 并行）

**文件：`eval/run.py`：**
- `--axes all|classifier|e2e`；运行前取 `git rev-parse --short HEAD` 做结果后缀
- 跑 → 归档 `results/<axis>-<date>-<hash>.json` → 更新 `results/last-run.json` 指针 → 输出与上一次的 delta 表
- 可选包装脚本 `scripts/change_check.sh`：改前跑 `--all`，改后再跑，打印"这次改动让哪些数字变了"

**纪律（Phase D 是执行器）：** 每次改 `agent/nodes/classify.py` / `agent/prompts/` / `agent/persona/` 前，先 `python -m eval.run --all`。

**完成标准**：改参数 → 跑一遍 → 3 分钟内拿到 delta 报告。

---

## Phase E — 推迟项（部署前，仅任务卡不实施）

记忆脚本化多轮（需 DB 环境）/ 输出质量 judge / 安全探测 / RAG 盲标注。

---

## 风险与预算（诚实版）

| 风险 | 对策 |
|------|------|
| LLM 成本：分类 ~122 条 + e2e 30 条 × 3-8 次调用 ≈ 300-400 次/轮 | DeepSeek flash：单轮个位数到十几元量级（粗估）；每周 5-10 轮有预算意识即可 |
| 复现性：LLM 非确定 | 通过率容忍带 ±2-3 个百分点；看趋势与 delta，不看单次绝对值 |
| Bangumi API 429/数据变化 | BaseClient 重试已吸收 429；断言不含具体数值（只测行为） |
| 中文分词种子规则误伤 | 关键词基线定位是"对比锚"，LLM 分类器才是被测对象，基线差不影响结论 |

---

## 执行顺序（并行策略）

```
我：生成复核表(A1) ──→ 你批注 ──→ 我改数据集+冻结(A2-4)
我：B 阶段可立即开工（不依赖你的批注，规则先就位，数据文件用主集）
    └──→ C 阶段（e2e schema 先行，等你批注完 A 再跑全量）
D 阶段随时并行
```