# Agent Eval 决策手册 — 官方标准 × 本项目映射

> 2026-08-17 | 回答一个问题："官方说 agent 要测什么，映射到 BGM Agent 的每一块，分别建什么 pipeline、出什么数字？"
> 官方来源：Anthropic《Demystifying evals for AI agents》（[原文](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)）、《How to build agents with the Claude API》、[LangSmith Evaluation Types](https://docs.langchain.com/langsmith/evaluation-types)、[OpenAI Evals 决策框架](https://theneuralbase.com/openai-evals/learn/intermediate/decision-frameworks/)、[τ-bench](https://github.com/sierra-research/tau-bench)、[LongMemEval](https://web.cs.ucla.edu/~kwchang/bibliography/wu2025longmemeval/)、Google Agent 白皮书

---

## 0. 一句话原则

**测什么 = 测"你将来会因为改代码而后悔的事"。** 官方框架只是标准分类，落到本项目就是下面这张表。**不要为每个板块都建 eval**——只建"改动频率高 + 坏掉代价高"的（见 §3 优先级）。

## 1. 官方评估类别 → 本项目映射总表

| # | 官方类别（出处） | 本项目的对应组件 | 测什么（具体行为） | 用什么方法 | 出什么指标 | 现有资产 → 缺口 |
|---|---|---|---|---|---|---|
| 1 | **分类正确性**（OpenAI Evals: classification/accuracy；Anthropic: evals for classification） | `agent/nodes/classify.py` 7-intent 分类器 | 每条用户消息被分成哪个 intent；尤其常识→realtime 误分（P0#3） | 冻结黄金集（102+20 条）跑分类器 + 关键词基线对照 | accuracy / per-class P/R/F1 / 混淆矩阵 / 置信度-校准 | ✅ `intent_queries_v7.json`+`intent_adversarial.json`（已建）→ ❌ `eval_classifier.py` 关键词基线仍是旧标签 |
| 2 | **任务完成率 task completion**（Anthropic demystifying；OpenAI: gradable success） | `agent/graph.py` 端到端（/chat 全链路） | 用户目标是否达成（推荐给理由、查到评分、讨论了立场）——不是逐字比对 | 30-50 条 golden 场景，每条预标注断言（intent/工具集/字数/内容要点） | 场景通过率（按维度分组）/ 错误率 | ⚠️ `docs/Eval/e2e-scenario-testing.md` 是设计；`test_api_v3.sh` 是 45 场景手跑 53% → ❌ 无脚本化 harness |
| 3 | **工具调用正确性 tool use**（Anthropic《Writing tools for agents》测量法：复杂参数 72%→90%；τ-bench） | `tools/bgm_tools.py` 16 个工具 + `agent/nodes/reasoning.py` 工具选择 | 该调哪个工具是否调对；参数是否正确（subject_id 是否幻觉）；`{"_error"}` 后能否恢复；deep 是否调工具（P0#2） | 脚本化任务集（预知正确工具+参数）+ 运行时 schema 校验日志 + 错误恢复探测 | 工具选择准确率 / 参数正确率 / 私有字段校验通过率 / 幻觉实体率 / 恢复率 | ⚠️ pytest 测了 schema 契约 → ❌ 无真实 LLM 下的工具选择测量 |
| 4 | **轨迹质量 trajectory/behavior**（Anthropic demystifying；LangSmith trajectory evaluator；LangGraph eval 策略） | `agent/routing/routes.py` 熔断 + 隐式终止 | 是否空转（连续搜索无果是否及时退出）；迭代是否浪费用量；显式终止是否正确触发；重复调用检测是否生效 | 从 e2e 运行 trace 里数行为（迭代数/空搜索次数/重复调用数/终止类型），纯代码统计 | 平均/最大迭代数、空搜索终止率、重复调用率、**预算浪费率**（超出理论最小迭代的比例） | ❌ 完全没有。但零成本——trace 已经在 `DEV_MODE` 里 |
| 5 | **检索质量 retrieval**（IR 标准 + Google/Ragas 检索评估） | `rag/retriever.py` 三通道 | 检索是否召回相关实体；名称/关键词/向量通道退化检测 | 现有 35 题 + 盲人工标注 + GT 入库 | Recall@k / Precision@k / MRR / NDCG@k / 分类型 | ✅ `rag/eval/evaluate.py` 完整 → ⚠️ 补盲标注去"射箭后画靶" |
| 6 | **记忆保持率 memory retention**（LongMemEval 方法论；LangGraph memory 文档） | `agent/memory/` L1 滑动窗口 + L2 语义召回 | 8+ 轮后是否还记得（P1#4）；跨会话召回是否被利用（注入≠使用）；L2 开关 A/B | 脚本化多轮对话种事实→远轮收回；judge 判断回复是否用了召回内容 | 事实保持率（vs 轮距遗忘曲线）/ 召回利用率 / L2 A/B delta | ⚠️ `docs/Eval/memory-evaluation.md` 是设计 → ❌ 无脚本化数据 |
| 7 | **输出质量 output quality**（Anthropic/LangSmith: rubric、pairwise；MT-Bench 思路） | `agent/persona/` Character Card + Render Node | 字数合规（P0#1）；markdown/emoji 泄漏；回复相关度；三人格可区分度 | 确定性规则（字数/泄漏/空回复）+ LLM-judge（rubric 先行、judge≠被评模型、20% 人工校准） | 字数合规率 / 泄漏率 / judge 评分分布 / 人格盲猜准确率 | ⚠️ `docs/Eval/personality-evaluation.md` 是完整设计 → ❌ 未实现 |
| 8 | **安全与隐私 safety**（Anthropic/OpenAI 一致：必须测） | `agent/guardrails.py` + 日志 | 用户评分/观看历史是否泄漏（vision 隐私第一）；提示注入；NSFW 边界 | red-team 探测集（10-20 条）+ 日志脱敏抽查 | 泄漏率 / 注入成功率 / 脱敏是否生效 | ❌ 完全没有。低优先级（只读 agent，风险面小） |
| 9 | **成本与延迟 cost/latency**（Anthropic demystifying：efficiency；Google 白皮书） | `main.py` 端点 + `agent/llm.py` | 每会话 token 成本；p50/p95 延迟；限流阈值设置依据 | e2e 跑批时顺带统计 | 单会话成本 / 延迟分位数 / 按 intent 分桶 | ❌ 无。但 DEV_MODE telemetry 已有原始数据，统计代码极少 |
| 10 | **模型/参数退化检测 model regression**（Anthropic 核心场景：prompt 改动回归） | 全部（每次改 prompt/人格/工具描述） | 改动前后同一套 eval 的 delta——防"改好了 A 弄坏了 B" | 上面的所有 eval 加一个统一 runner + 基线对比 | 全指标 delta 报告 | ❌ **这是最该先建的一层：eval run 必须先于一切** |

## 2. 不需要建 eval 的部分（避免过度工程）

- **`core/config.py`、`database/` 迁移、`schemas/`**：纯 schema/配置，pytest 已覆盖，无 LLM 行为
- **`clients/` HTTP 重试/消毒**：pytest 已覆盖（确定性代码）
- **单节点内部纯函数**（extract_user_input 等）：pytest 已覆盖
- **模型选择**：单一 DeepSeek，无模型对比需求

## 3. 优先级排序（按"改动频率 × 坏掉代价"）

| 优先级 | 组件 | 理由 |
|--------|------|------|
| **P0** | 10 统一 runner + 1 分类 + 2 任务完成 | 分类是入口（全链路都受它影响）；任务完成是产品语义；runner 是所有 eval 的地基 |
| **P1** | 3 工具正确性 + 4 轨迹 | 成本与幻觉的直接来源；P0#2 就住在这里 |
| **P1** | 7 输出质量 | 人格是差异化资产，但改动频率低于分类 |
| **P2** | 5 检索（已有，只补盲标注）+ 6 记忆 + 9 成本延迟 | 依赖 DB 环境；reactive 维度 |
| **P3** | 8 安全隐私 | 只读 agent，风险面小 |

## 4. 与现有资产的对账（你其实比你以为的多）

| 已有 | 状态 | 还缺 |
|------|------|------|
| 102+20 条分类黄金数据 | ✅ 刚建 | `eval_classifier.py` 关键词基线同步 7-intent |
| `eval/metrics.py`（accuracy/F1/混淆矩阵/Recall@k/MRR/NDCG） | ✅ 完整 | 无 |
| `rag/eval` 检索评测（35 题 + pooling 标注 + 消融） | ✅ 工程完整 | 盲标注 + GT 入库 |
| `docs/Eval/` 8 篇方法论（e2e/personality/memory/tool-reliability…） | ✅ 设计完整 | **全是设计，无 harness**——这是与"缺数据"直接对应的断层 |
| `test_api_v3.sh` 45 场景（手跑 53%） | ✅ 产品反馈数据 | 脚本化 + 断言化 + 可复现（tape 化） |
| 539 pytest / 493 绿 | ✅ 结构回归网 | 不含任何 LLM 行为测量 |

## 5. 落地路径（与轴 1 已建数据衔接）

1. **先建第 10 项（统一 runner）**——一个 `eval/run.py`：读数据集 → 调 `agent_app` 或分类器 → 采集 trace → 算确定性指标 → 归档 `results/<date>-<hash>.json` → 与基线 diff。其他所有轴都挂到它上面
2. 轴 1 分类：同步关键词基线 → 跑第一份 accuracy
3. 轴 2 任务完成：把 45 场景脚本化（每条预标注断言）
4. 轴 3 工具/轨迹：从 e2e trace 顺带采集，零额外调用成本
5. 其余轴按 P1/P2 顺序挂载

> 完整引用：Anthropic 强调 eval-first 工作流（"You cannot ship an agent without evals"）；OpenAI 决策框架（gradable vs non-gradable、先确定性后 judge）；LangSmith 评估类型（correctness/rubric/pairwise/trajectory）。本表的每一行都对应一个可执行 spec，spec 缺失的 = 下一张待建任务卡。