# RAG 检索评测设计

> **目标**: 定量验证 5 阶段检索管线中每个阶段对检索质量的贡献
> **核心假设**: 语义分桶 + 对数归一化 + MMR 去重的组合策略，相比 vanilla pgvector cosine distance 检索，显著提升 Bangumi 领域数据的检索质量
> **预计产出**: recall@5 / precision@5 / MRR / nDCG@5 的消融对比表

---

## 一、问题背景：为什么 vanilla pgvector 不够

Bangumi 的数据有三个特征使通用向量检索失效：

### 1.1 条目多版本共存（同系列刷屏）

《物语系列》在 Bangumi 上有 20+ 条目：化物语、伪物语、猫物语(黑)、猫物语(白)、…、每个都有独立条目。用户搜"物语系列"，vanilla pgvector 返回 top-5 可能全是物语系列的不同季度——用户真正想要的是"物语系列 + 相近风格的推荐"，而不是 5 个物语。

**解决**: MMR 同名去重（按 `name_cn` 去重）

### 1.2 冷热极度悬殊（热门碾压冷门）

评分人数范围从 0 到 50000+，最大差距约 250×。当语义相似度接近时，热门作品（评分人数多）在向量空间中天然占据优势，冷门佳作被挤出 top-5。

例如：搜"京阿尼青春群像剧"，《冰菓》（rating_total=12000+）和《玉子市场》（rating_total=3000+）的 embedding 距离可能只差 0.005，但 vanilla pgvector 按距离排序时，这个微小差距导致冷门作品永远排不上去。

**解决**: 语义分桶（`cosine_distance / 0.03`，语义梯队内按对数归一化热度辅助排序）

### 1.3 跨类型语义混淆

用户搜"艾伦"（进击的巨人角色），embedding 可能把"艾伦·耶格尔"（character）、"艾伦"相关的人物（person/声优）、以及含有"艾伦"关键词的作品（subject）都召回——类型混杂降低了用户感知的检索质量。

**解决**: 标量前置过滤（`entity_type WHERE` 子句限制检索域）+ 语义前缀嵌入（`[subject]/[character]/[person]` 前缀在 embedding 阶段区分实体类型）

---

## 二、评测指标

### 2.1 recall@k — 召回率

> ground truth 中的相关条目有多少出现在检索结果的 top-k 中

```
recall@k = |retrieved_topk ∩ ground_truth| / |ground_truth|
```

**为什么重要**: 对 Companion Agent 来说，**漏掉正确答案比多返回几个不相关的更致命**。如果用户问"和 EVA 同时代的意识流动画"，ground truth 里有《玲音》（serial experiments lain）但没被召回——agent 之后的整个推理链路都缺少这条关键信息，无法补救。

**k 的选择**: 当前 RAG 默认返回 top-5，所以 `recall@5` 是首选指标。但 agent 实际只使用 RAG 返回的第一条结果？还是全部 5 条都注入 prompt？实际使用方式是"取 top-5 结果注入 System Prompt 作为知识背景"，所以 recall@5 是合理的。额外报告 recall@3 和 recall@1 作为补充。

### 2.2 precision@k — 精确率

> 检索结果的 top-k 中有多少是真正相关的

```
precision@k = |retrieved_topk ∩ ground_truth| / k
```

**为什么重要**: 如果 top-5 里 4 条不相关，浪费了 prompt 中宝贵的 token 预算（L2 记忆注入预算只有 500 tokens），且可能误导 LLM。

### 2.3 MRR — Mean Reciprocal Rank

> 第一个正确答案排名的倒数的平均值

```
MRR = (1/|Q|) × Σ(1/rank_i)

其中 rank_i 是第 i 条查询的第一个 ground truth 条目在结果中的排名。
如果 ground truth 中没有任何条目出现在结果中，1/rank_i = 0。
```

**为什么重要**: 衡量"正确答案排得有多靠前"。用户通常只看第一条结果。MRR=0.5 意味着正确答案平均排在第 2 位（1/2=0.5），MRR=1.0 意味着始终排第一。

### 2.4 nDCG@k — 归一化折损累计增益

> 考虑 ground truth 中有多个正确答案且相关度可能不同

```
DCG@k = Σ(rel_i / log2(i+1))
nDCG@k = DCG@k / IDCG@k

其中 rel_i 是第 i 位的相关度得分，IDCG 是理想排序下的 DCG
```

**为什么需要**: 部分查询的 ground truth 有多个正确答案且相关度有差异（"和 EVA 同时代的意识流"——《玲音》高度相关、《少女革命》中度相关、《星际牛仔》低度相关但可接受）。nDCG 能捕捉这种多级相关度，比 recall@k（只看有/无）更细粒度。

**⚠️ 标注成本权衡**: 多级相关度（rel=3/2/1/0）的标注工作量远大于二元相关度（rel=1/0）。建议 v1 评测集先用二元相关度，rel ∈ {0, 1}。在二元标注下，nDCG 等价于 DCG 的归一化版本，信息量和 MRR 相近。

### 2.5 v1 推荐指标组合

| 指标 | 优先度 | 理由 |
|------|--------|------|
| recall@5 | 🔴 必须 | 最直观，agent 的实际使用方式 |
| precision@5 | 🔴 必须 | 衡量 token 预算效率 |
| MRR | 🟡 推荐 | 衡量排序质量 |
| recall@3 | 🟢 补充 | 如果 top-5 太宽 |
| nDCG@5 | 🟢 未来 | v2 评测集引入多级相关度标注时启用 |

---

## 三、测试集构造

### 3.1 查询分类体系（Query Taxonomy）

不同难度的查询测不同的能力。按 Bangumi 用户的真实使用场景分 5 类：

| 类别 | 示例 | 数量 | 测什么 | 预期难度 |
|------|------|------|--------|---------|
| **A. 精确作品名** | "進撃の巨人"、"Steins;Gate"、"EVA" | 15 | 精确匹配：embedding 能否把作品名映射到正确向量 | 低 |
| **B. 模糊描述** | "80年代黑暗系机战"、"京阿尼青春群像剧"、"让人致郁的动画" | 20 | 语义理解：自然语言描述 → 正确的类别/风格匹配 | 高 |
| **C. 冷门作品** | 选择 rating_total < 500 的作品作为查询目标 | 10 | 冷启动：热度信号极弱时检索是否仍然有效 | 最高 |
| **D. 跨类型查询** | "艾伦"（搜 character）/"新房昭之"（搜 person） | 10 | 标量过滤：entity_type 过滤是否生效 | 中 |
| **E. 模糊边界** | "和EVA同时代的意识流"、"类似星际牛仔的公路片" | 5 | 跨作品关联：能否建立作品间的语义连接 | 最高 |

> 总数: 60 条。对 v1 评测集来说够用——再少则统计不显著，再多则标注成本过高。

### 3.2 Ground Truth 标注规范

**二元标注**（v1）: 每条查询标注 3-5 个 ground truth entity_ids，表示"最相关的前 5 个结果"。所有标注条目视为同等相关（rel=1）。

**标注原则**:
1. 标注者 = 你自己（你是 Bangumi 重度用户，且最了解系统设计意图）
2. 标注时**只看作品列表和简介，不看 RAG 返回结果**——避免确认偏误
3. 如果 ground truth 条目不在 `rag_entities` 表中（还没灌数据）→ 标注时标记为"待验证"

**标注来源**: 在 Bangumi 网站搜索 query text，取前 3-5 个最相关的结果的 subject_id → 映射到 `rag_entities` 表的主键（`subject_XXX`）。

**标注文件格式**:

```json
{
  "version": "1",
  "created": "2026-08-02",
  "description": "RAG evaluation v1 — 60 queries across 5 categories",
  "queries": [
    {
      "id": "q001",
      "text": "80年代黑暗系机战动画",
      "entity_type": "subject",
      "category": "B",
      "ground_truth": ["subject_265", "subject_3008", "subject_876"],
      "notes": "模糊描述查询。ground_truth 来自 Bangumi 搜索'黑暗 机战'标签筛选 + 个人判断"
    }
  ]
}
```

---

## 四、消融实验设计

### 4.1 实验配置

```python
ABLATION_CONFIGS = [
    # Config 0: 基线 — 纯向量检索
    {
        "name": "Vanilla pgvector",
        "enable_threshold": False,
        "enable_bucketing": False,
        "enable_mmr": False,
        "hypothesis": "无任何后处理，纯依赖 pgvector cosine_distance 排序"
    },
    # Config 1: + 距离阈值
    {
        "name": "+ Distance threshold (0.65)",
        "enable_threshold": True,
        "enable_bucketing": False,
        "enable_mmr": False,
        "hypothesis": "阈值过滤能剔除完全不相关的噪音（cosine_distance > 0.65），但不改变排序质量"
    },
    # Config 2: + 语义分桶
    {
        "name": "+ Semantic bucketing",
        "enable_threshold": True,
        "enable_bucketing": True,
        "enable_mmr": False,
        "hypothesis": "分桶后同梯队内按对数热度排序，冷门作品不再被热门以微小距离优势挤出"
    },
    # Config 3: + MMR 去重（完整管线）
    {
        "name": "+ MMR dedup (Full pipeline)",
        "enable_threshold": True,
        "enable_bucketing": True,
        "enable_mmr": True,
        "hypothesis": "MMR 去重解决同系列刷屏问题，进一步提升 precision"
    }
]
```

> **注**: 语义分桶和对数归一化在代码中是同一个排序 key 的两个部分，无法独立开关。Config 2 将两者作为一个整体启用。如果消融结果显示 Config 2 提升明显，未来可考虑拆分实验（线性热度 vs 对数归一化热度）。

### 4.2 每个配置回答的问题

| 对比 | 回答的问题 |
|------|-----------|
| Config 1 vs Config 0 | "距离阈值过滤是否必要？丢弃的是噪音还是弱相关结果？" |
| Config 2 vs Config 1 | "语义分桶+对数归一化是否解决冷热悬殊问题？冷门作品 recall 是否提升？" |
| Config 3 vs Config 2 | "MMR 去重是否提高 precision？是否误杀了同名的不同作品？" |

### 4.3 分类型分析

除了全量指标，还应该按查询分类（A/B/C/D/E）分别报告 recall@5，以定位"pipeline 的优势集中在哪类查询"：

```
                Vanilla     +Threshold    +Bucketing    +MMR (Full)
A. 精确作品名    0.95        0.95          0.95          0.95
B. 模糊描述      0.55        0.58          0.72 ↑        0.74
C. 冷门作品      0.35        0.35          0.55 ↑↑       0.58
D. 跨类型查询    0.80        0.82          0.82          0.85
E. 模糊边界      0.30        0.30          0.40          0.42
```

如果 B 类和 C 类在 Config 2 有显著提升，就验证了"冷热悬殊是核心问题，分桶解决了它"。

---

## 五、实验协议

### 5.1 环境

- **嵌入模型**: Zhipu embedding-3（2048d），与生产环境一致
- **检索库**: `rag_entities` 表当前数据快照。实验报告标注数据快照日期和条目总数
- **参数固定**: `distance_threshold=0.65`, `semantic_bucket_size=0.03`, `limit=5`（除非消融变量）
- **NSFW**: `exclude_nsfw=True`（安全护栏开启）

### 5.2 可复现性

1. 评测脚本 `eval/eval_rag.py` 必须是一键运行（`python eval/eval_rag.py`）
2. 实验报告包含：日期、数据快照信息、嵌入模型版本、所有配置的完整参数
3. 评测数据集（`rag_queries.json`）入 git
4. 实验结果（`eval/results/YYYY-MM-DD_rag_ablation.md` 和 `.csv`）入 git

### 5.3 输出报告格式

```markdown
## RAG Evaluation Report — 2026-08-02

**数据快照**: rag_entities 共 15,234 条，2026-08-01 导出
**嵌入模型**: Zhipu embedding-3 (2048d)
**参数**: distance_threshold=0.65, bucket_size=0.03, limit=5, exclude_nsfw=True

### 总体指标

| Configuration                    | recall@5 | precision@5 | MRR   |
|----------------------------------|----------|-------------|-------|
| Vanilla pgvector                 | 0.XX     | 0.XX        | 0.XX  |
| + Threshold (0.65)               | 0.XX     | 0.XX        | 0.XX  |
| + Semantic bucketing + log norm  | 0.XX     | 0.XX        | 0.XX  |
| + MMR dedup (Full pipeline)      | 0.XX     | 0.XX        | 0.XX  |

### 分类型 recall@5

| Category (n)  | Vanilla | +Thr | +Bucket | Full |
|---------------|---------|------|---------|------|
| A. 精确 (15)  | 0.XX    | 0.XX | 0.XX    | 0.XX |
| B. 模糊 (20)  | 0.XX    | 0.XX | 0.XX    | 0.XX |
| C. 冷门 (10)  | 0.XX    | 0.XX | 0.XX    | 0.XX |
| D. 跨类型 (10)| 0.XX    | 0.XX | 0.XX    | 0.XX |
| E. 边界 (5)   | 0.XX    | 0.XX | 0.XX    | 0.XX |

### 关键发现

1. ...
2. ...
```

---

## 六、代码架构

### 6.1 生产代码改动

`rag/retriever.py` — `RagEntityRetriever.hybrid_search()` 增加 3 个消融控制参数：

```python
def hybrid_search(
    self,
    query: str,
    entity_type: Literal["subject", "character", "person", "all"] = "all",
    limit: int = 5,
    exclude_nsfw: bool = True,
    distance_threshold: float = 0.65,
    semantic_bucket_size: float = 0.03,
    # ── 消融控制 ──
    enable_threshold: bool = True,
    enable_bucketing: bool = True,
    enable_mmr: bool = True,
) -> list[RagSearchResult]:
```

内部逻辑变更：3 个 `if enable_*:` 判断。默认值全 True → 对现有调用方完全透明。

### 6.2 评测代码

```
eval/
├── data/
│   └── rag_queries.json       # 60 条评测查询 + ground truth
├── metrics.py                  # recall_at_k, precision_at_k, mrr, ndcg_at_k
├── eval_rag.py                 # RAG 评测主脚本
└── results/
    └── .gitkeep
```

`eval_rag.py` 的主流程：

```python
def main():
    # 1. 加载测试集
    queries = load_queries("eval/data/rag_queries.json")

    # 2. 初始化检索器
    settings = get_settings()
    engine = create_engine(settings.DATABASE_URL)
    retriever = RagEntityRetriever(
        engine=engine,
        zhipu_api_key=settings.ZHIPU_API_KEY,
    )

    # 3. 跑消融实验
    results = {}
    for config_name, config_kwargs in ABLATION_CONFIGS.items():
        all_retrieved = []
        for q in queries:
            retrieved = retriever.hybrid_search(
                query=q["text"],
                entity_type=q["entity_type"],
                **config_kwargs,
            )
            all_retrieved.append([r.entity_id for r in retrieved])
        results[config_name] = compute_all_metrics(queries, all_retrieved)

    # 4. 输出报告
    print_report(results, output_path="eval/results/rag_ablation.md")
```

---

## 七、预期结果与解释

### 7.1 如果实验结果符合假设

| 对比 | 预期 | 解释 |
|------|------|------|
| Threshold vs Vanilla | recall 基本持平，precision 小幅提升 | 阈值过滤去掉的是完全不相关的噪音（cos_dist > 0.65），不太影响 recall |
| Bucketing vs Threshold | **recall 明显提升（+8~15pp），尤其是 B 类和 C 类** | 这是管线最大的价值点：分桶隔离热门/冷门，冷门作品不再被挤出 |
| Full vs Bucketing | precision 提升（+3~5pp），recall 基本持平 | MMR 去重减少重复结果，但不应该降低 recall |

### 7.2 如果实验结果不符合假设

- **Bucketing 没有提升**: 说明冷热悬殊不是主要问题，可能是 embedding 质量已经足够好（语义相似的作品 cosine_distance 天然聚类），或者 bucket_size=0.03 划分太粗/太细。可以调参后重试
- **MMR 降低了 recall**: 可能误杀了同名但不同的作品（如"劇場版 鬼滅の刃 無限列車編"和"鬼滅の刃"被当成同名去重）。需要检查 MMR 的 dedup_key 粒度
- **全管线提升很小（<5pp）**: 如果 vanilla pgvector 已经 recall@5=0.85+，那 pipeline 的价值不大。但即便如此，分类型分析（C 类冷门作品）仍可能有意义——"全量提升不大但冷门作品提升显著"也是一个有力的论点

---

## 八、迭代计划

- **v1**（本周末）: 60 条查询，二元相关度标注，4 个消融配置
- **v2**（未来）: 扩充到 100+ 条，引入多级相关度（3/2/1/0），增加 nDCG
- **v3**（未来）: 如果换了 embedding 模型或调了 chunking 策略，重新跑一次完整消融。评测集保持稳定，只重新标注 ground truth 如果数据库条目变了
