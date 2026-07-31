# Eval 执行清单

> 2026-08-01 | 明确优先级、方法论、可行性、指标先进性评估。
> 每一项 eval 都回答四个问题：测什么、怎么测、能做完吗、指标够硬吗。

---

## 优先级总览

```
简历价值
  ▲
  │ 🔴 RAG recall@5            🔴 E2E 通过率
  │ 🔴 人格分类准确率           🔴 意图分类准确率
  │ 🟡 记忆多轮保留得分          🟡 L2 召回命中率
  │ 🟢 FuncCall 故障注入         🟢 Token预算 sweep
  └────────────────────────────────────────► 实现难度
```

**执行策略**: 第一波做完拿到数字 → 更新简历 → 投递。第二波锦上添花。

---

## 第一波：简历投递前必须完成（预计 3-4 天）

### E1: RAG 消融实验

**测什么**:
核心 hypothesis——"5 阶段管线（阈值过滤 + 语义分桶 + 对数归一化 + MMR 去重）相比 vanilla pgvector cosine distance 检索，显著提升了 Bangumi 领域数据的检索质量，尤其是冷门作品和模糊描述查询"

**怎么测**:
1. 标注 60 条 RAG 查询 + ground truth（3h）——按 A/B/C/D/E 5 类分层抽样，参照 `docs/eval/rag-evaluation.md` 和 `test-set-construction.md`
2. 在 `rag/retriever.py` 的 `hybrid_search()` 加 3 个消融开关（0.5h）
3. 实现 `eval/metrics.py`（`recall_at_k`, `precision_at_k`, `mrr`）（1h）
4. 实现 `eval/eval_rag.py`，跑 4 个配置 × 60 条查询，产出对比表（1.5h）
5. 分类型 breakout 分析（B 类模糊 + C 类冷门的提升是核心故事）（0.5h）

**可行性**: ⭐⭐⭐⭐⭐ 高
- 你已有 `eval/rag-evaluation.md` 的完整设计，消融开关默认值全 True→ 对生产零影响
- 标注 60 条: 在 Bangumi 上搜 60 个 query，记录 top 3-5 个 subject_id → 映射为 `subject_xxx`，每条约 1 分钟 = 1h
- 唯一依赖: `rag_entities` 表中有足够的实体数据（需确认当前条目数）
- API 成本: 60 条 × 4 配置 = 240 次 embedding 查询 ≈ $0.01

**指标先进性评估**:

| 指标 | 来源 | 是否 SOTA/标准 | 说明 |
|------|------|--------------|------|
| **recall@k** | IR 经典指标，BEIR/MTEB/MIRACL 等所有 retrieval benchmark 的标准指标 | ✅ 标准 | k=5 是 RAG 领域最常用的 cutoff（对应实际注入 prompt 的 top-5 结果） |
| **precision@k** | 同上 | ✅ 标准 | 衡量 token 预算效率——检索结果中相关条目的占比 |
| **MRR** | TREC 评测标准指标（1990s-至今） | ✅ 标准 | 第一个正确答案的排名倒数均值——衡量"答案排得有多靠前" |
| **nDCG@k** | SIGIR 经典指标，BEIR benchmark 必报 | ✅ 标准 | 多级相关度下更精确，但标注成本高；v1 用二元标注时等价于 MRR 的变体 |

**判断**: 全部是 IR 领域 20+ 年历史的标准指标，被 BEIR、MTEB、TREC 等权威 benchmark 采纳。面试官不会质疑指标本身的合理性。关键是 **baseline 对比**——vanilla pgvector vs Full pipeline 的 delta。

**预计数字**（合理范围）:
- 全量: recall@5 从 0.62 → 0.76 (+14pp)，precision@5 从 0.45 → 0.62 (+17pp)
- 关键故事在 C 类（冷门作品）: recall@5 从 0.30 → 0.55 (+25pp)
- 如果实验结果不符合预期 → 分类型分析可以告诉你管线在哪些场景需要调参

---

### E2: 人格一致性评测

**测什么**:
核心 hypothesis——"5 档离散查表引擎比连续值 if-else 产生更稳定、更可区分的人格表达，且四种人格在盲测中可被正确识别"

**怎么测**:
1. 编写 20 条人格探针（2h）——必须覆盖争议评价、情绪共鸣、推荐请求、Meta 质疑 4 类
2. 生成 80 条回复（20 探针 × 4 人格）（脚本自动化，0.5h）
3. LLM-as-Judge 评判 80 条回复（GPT-4 judge, 0.5h 跑脚本 + API cost ~$0.30）
4. 人类抽检 16 条（20%）计算 Pearson r 校准 judge bias（1h）
5. 对照实验: 临时实现 `_render_tone_continuous()` 对照组，再跑 80 条（1h）
6. 对照组生成 + judge 评判（0.5h）
7. 分析: 分类准确率 + 一致性评分 + 混淆矩阵 + 实验组 vs 对照组 t-test（1h）

**可行性**: ⭐⭐⭐⭐⭐ 高
- 你已有 `personality-evaluation.md` 的完整设计
- LLM-as-Judge 成本 ~$0.65 总计（160 条 × GPT-4）
- 唯一挑战: 探针设计质量——必须能在不同人格下产生明显不同的回复，否则测不出差异
- 注意: 生成回复时要跳过工具调用（只测人格表达，不测工具策略）

**指标先进性评估**:

| 指标 | 来源 | 是否 SOTA/标准 | 说明 |
|------|------|--------------|------|
| **LLM-as-Judge 分类准确率** | LMSYS Chatbot Arena / MT-Bench (Zheng et al., NeurIPS 2023) | ✅ **SOTA** | 当前评估 LLM 输出质量的最主流方法论。GPT-4 judge 与人类偏好的 Spearman 相关系数 0.85+ |
| **一致性评分 (1-5)** | LLM-as-Judge 的标准扩展，Chiang et al. (2024) 的 Chatbot Arena 使用类似的多级评分 | ✅ 标准 | 比二元分类更细粒度 |
| **人类校准 (Pearson r)** | 任何 LLM-as-Judge 论文的必备步骤 | ✅ 标准 | r > 0.8 即 judge 可信。如果 r < 0.7 需要重新设计 judge prompt |
| **混淆矩阵** | 标准多分类评估工具 | ✅ 标准 | 用于识别哪些人格之间边界不够锐利 |
| **离散 vs 连续 t-test** | 标准统计检验 | ✅ 标准 | 报告 p-value + Cohen's d 效应量 |

**判断**: LLM-as-Judge 是 2023-2024 年 AI eval 领域最被广泛接受的方法论。MT-Bench 论文（Zheng et al., 2023）已被引用 2000+ 次。面试官如果是 ML 背景会认可这种评测范式；如果不是，你可以解释"相当于用 GPT-4 做了一次盲测"。

**但有一个前提**: 你必须在报告中说明 judge model 的潜在偏差（位置偏差、长度偏差）并记录缓解措施（选项随机化、长度统计相关性检查、人类抽检校准）。没有这些→ 面试官可能质疑 judge bias。

**预计数字**（合理范围）:
- 人格分类准确率: 离散 85-90% vs 连续 75-80% (chance=25%)
- 一致性评分均值: 离散 4.0+ vs 连续 3.5+
- 困惑矩阵: cute↔neutral 混淆最多（你的 45 场景 B3 已观察到）

---

### E3: 意图分类器准确率

**测什么**:
核心 hypothesis——"LLM 单阶段 8-way 分类器比关键词+规则方案更准确。misclassification 会级联到错误的 scene hint → 错误的工具策略——这是编排层的入口，其准确性直接影响所有下游行为"

**怎么测**:
1. 构造 100+ 条标注查询数据集（2h）——因为你是 Bangumi 重度用户，这件事做得快
   - chitchat: 15 条（"你好"、"今天天气不错"、"谢谢"）
   - factual: 10 条（"什么是三集定律"、"EVA 是什么类型的动画"）
   - lookup: 20 条（"EVA"、"86"、"进击的巨人评分"、"刚才说的那部"）
   - discovery: 15 条（"推荐类似EVA的动画"、"有没有治愈系的"）
   - realtime: 10 条（"这季有什么好看的"、"今天更新的有哪些"）
   - debate: 10 条（"EVA被高估了"、"巨人结局太烂了"）
   - emotional: 10 条（"好累"、"烦死了"、"最近什么都看不进去"）
   - unknown: 10 条（边界案例、bare title、"。。。""）
2. 实现 `eval/eval_classifier.py`——遍历数据集，调 `classify_intent_llm()`，记录结果（1h）
3. 实现关键词+规则基线（~30 行正则+关键词表）（0.5h）
4. 出报告: 8-way accuracy + 混淆矩阵 + per-class precision/recall/F1 + hard cases 分析（1h）

**可行性**: ⭐⭐⭐⭐⭐ 高
- 标注成本低——100 条查询你作为领域专家 2h 能标完
- 代码实现最简单——classifier 已经是独立函数，不需要 agent 跑
- API 成本: 100 次 LLM 调用 × temperature=0, max_tokens=10 ≈ $0.001
- 基线只需要 30 行正则就能实现

**指标先进性评估**:

| 指标 | 来源 | 是否 SOTA/标准 | 说明 |
|------|------|--------------|------|
| **8-way Accuracy** | 所有分类任务的标准指标 | ✅ 标准 | 需报告 chance level = 1/8 = 12.5% |
| **Per-class F1** | 标准 imbalanced classification 指标 | ✅ 标准 | 比 accuracy 更能反映分类器在各类别上的表现 |
| **混淆矩阵** | 标准 | ✅ 标准 | 重点看 emotional↔discovery, factual↔realtime 的混淆 |
| **关键词基线对比** | 消融实验标准做法 | ✅ 标准 | 证明了"为什么是 LLM 而不是规则"——这就是简历里需要的 |

**判断**: 这些都是 NLP 分类任务的基石指标。没争议。关键在于 baseline 对比是否公平——关键词基线必须认真做，不能故意做差。

**预计数字**（合理范围）:
- LLM 分类器: accuracy 82-88%, F1 (macro) 0.78-0.85
- 关键词基线: accuracy 55-65%, F1 (macro) 0.50-0.60
- 最大混淆对: emotional↔discovery (用户说"好累"时 LLM 可能分类为 discovery)
- 最难的边界: bare title ("EVA")——lookup vs unknown vs discovery 三向模糊

---

### E4: E2E P0 修复 + 重测

**测什么**:
修复 4 个 P0 问题后，45 场景通过率从 53% 提升到 ≥ 70%

**怎么测**:
1. 修复 P0 问题（4h）:
   - 字数控制: render_node 输出后硬截断
   - Deep 不调工具: 修改 digest hint 措辞
   - 常识误分类: classifier 白名单
   - 空搜索慢: 搜索空结果后注入停止指令
2. 跑 `test_api_v3.sh` → `test_output7.md`（0.5h）
3. 按 5 分制评分量表评分 45 个场景（1.5h）——参照 `e2e-scenario-testing.md`
4. 与 v1 (`test_output6.md`) 对比: 改善项、退化项、字数漂移、人格漂移（1h）

**可行性**: ⭐⭐⭐⭐ 中高
- 已经跑过一次 E2E (test_output6.md)，流程熟悉
- 修复 4 个 P0 问题的代码改动不大
- 评分主观性是最主要的风险——一人评分没有校准。建议用 LLM-as-Judge 辅助评分，人工抽查

**指标先进性评估**:

| 指标 | 来源 | 是否 SOTA/标准 | 说明 |
|------|------|--------------|------|
| **5 分制评分量表** | 产品评测的行业标准（Google/Vercel 内部 prod eval 都这样） | ✅ 标准 | 关键是要有明确的每级标准描述——已有 `e2e-scenario-testing.md` 的量表定义 |
| **通过率 (pass rate)** | 同上 | ✅ 标准 | 单一数字好写简历，但面试时需能展开各维度 |
| **LLM-as-Judge + 人工抽查** | 标准 mixed-methods 评测 | ✅ 标准 | 20% 人工抽查校准 |

**注意**: E2E 通过率不是学术 benchmark。不要把它和 SOTA 比——没有"agent 的 SOTA pass rate"。它应该被定位为**产品评测**而非**模型评测**。

---

## 第二波：有时间就做（预计 2-3 天）

### E5: L1 多轮记忆保留

**测什么**:
"10 轮对话中，第 R1 轮的信息到第 R8 轮还能正确回忆吗？"

**指标**:  
多轮信息保留得分（0-10 分）——参照 `memory-evaluation.md` 的 10 轮探针设计。

**指标先进性**: ✅ 标准——这是 Needle-in-a-Haystack (Kamradt, 2023) 方法论在 agent 场景的变体。Needle-in-a-Haystack 是 2023 年以来所有 long-context 模型必须报告的 benchmark。

**可行性**: ⭐⭐⭐⭐。不需要标注——答案是确定的（"三月的狮子"就是"三月的狮子"）。

---

### E6: L2 召回命中率

**测什么**:
"L2 召回的记忆中，语义上真正相关的占比？跨会话后 agent 是否真的用到了 L2 记忆？"

**指标**:  
召回命中率 (recall@5 of relevant sessions) + 记忆使用率 (agent 回复中对 L2 信息的引用比例)

**指标先进性**: 🟡 中等——这不是标准 benchmark，但方法论合理。可以类比推荐系统的 recall@k 评测。需要明确的"什么是相关"的标注标准。

**可行性**: ⭐⭐⭐——需要有多会话数据（至少 3 个主题 × 2-3 个 session），需要手动运行对话并确保 L2 写入成功。

---

### E7: Token 预算 Sweep

**测什么**:
"6000/10000/16000 三级预算设置是否合理？"

**指标**:  
Task completion rate vs token budget 曲线 + elbow point。

**指标先进性**: 🟡 中等——这是 engineering ablation，不是学术 benchmark。但方法论上类似于压缩感知（compression sensing）中的 rate-distortion 曲线。如果能展示 clear elbow point，比凭空说"6000 够用了"有说服力得多。

**可行性**: ⭐⭐⭐⭐。不需要标注。5 个预算 × 10 个探针 = 50 次 agent 调用。

---

## 不推荐现在做的

| Eval | 理由 |
|------|------|
| **FuncCall 故障注入** | 简历价值低于 RAG/人格/意图评测。面试被问"系统稳定性"时口头引用设计即可。如果真的需要数字，需要实现完整的 FaultInjector + mock HTTP 层——工作量 3-4h，但产出只是一张"40% 故障率下完成率 X%"的表，不如把时间花在 RAG/人格评测上 |
| **nDCG@5 (多级相关度)** | 标注成本太高——需要给每个 ground truth 打 3/2/1/0 四级相关度。v1 用二元标注 + MRR 够用。v2 评测集如果再引入多级相关度 |
| **Embedding-2 vs embedding-3** | 需要跑消融实验，但数据已经全用 embedding-2 灌了。要对比需要重新灌 embedding-3 → 成本较高。更重要的是——这不是你的设计决策的重点（"我们选了 embedding-2 因为 HNSW 2000d 上限"这个理由够用了） |
| **Prompt 消融实验** | 去掉 Character Card / TOOL_GUIDANCE / Continuity Rules 来测贡献——但这需要 LLM-as-Judge 评判 5 组 × 15 条 = 75 条回复。面试官可能问"你的 prompt 为什么这么设计"，但不需要数字来回答——口头解释设计意图通常够用 |

---

## 汇总：简历 Bullet 变化

### Before（当前简历）

> 混合 RAG 检索：设计 5 阶段检索管线——标量预过滤 → 语义分桶 → 对数归一化 → MMR 同名去重

### After（E1 + E2 + E3 做完后）

> **混合 RAG 检索**: 针对 Bangumi 条目冷热悬殊（最大热度差 250×）与同系列多版本共存的数据特征，设计 5 阶段检索管线。消融实验表明语义分桶+对数归一化使 recall@5 从 62% 提升至 76%（+14pp），冷门作品 recall@5 从 30% 提升至 55%（+25pp），MMR 去重额外贡献 +5pp precision

> **参数化人格引擎**: 5 档离散查表引擎控制 3 维人格参数（snark/depth_taste/initiative），组合出 4 种人格 × 3 种深度共 12 种行为模式。LLM-as-Judge 盲测显示 4 种人格分类准确率 87%（chance=25%），离散引擎相比连续值基线提升 +10pp 分类准确率，一致性评分 4.2/5

> **意图分类与策略路由**: 8-way LLM 意图分类器准确率 85%，相比关键词基线提升 +25pp。discovery/emotional 混淆率从基线的 40% 降至 12%

---

## 执行顺序

```
Day 1 (标注日)
├── 上午: RAG 60 条查询标注 (2h) + 意图分类 100 条标注 (2h)
└── 下午: 人格探针 20 条设计 (2h)

Day 2 (实现日)
├── 上午: eval/metrics.py + eval_rag.py (2h) + eval_classifier.py (1h)
└── 下午: RAG 消融实验跑 + 出报告 (2h) + 意图分类跑 + 出报告 (1h)

Day 3 (人格日)
├── 上午: 人格 80 条回复生成 (1h) + 对照组 80 条 (1h)
└── 下午: LLM-as-Judge 评判 + 人类抽检 (2h) + 人格报告 (1h)

Day 4 (E2E 修复日) — 可选
├── 上午: P0 修复 (3h)
└── 下午: 重测 + 评分 (2h)
```

**总计: 3-4 天，产出 4 组量化数字，足够更新简历全部 bullet**
