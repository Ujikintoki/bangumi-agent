# RAG 模块 Pipeline 概览

> BGM Agent RAG 模块的宏观架构梳理，按标准 RAG Pipeline 框架组织。
> 最后更新: 2026-08-05

---

## 1. 数据摄入与索引 (Data Ingestion & Indexing)

### 1.1 数据加载 (Loading)

数据来源为 Bangumi p1 API，分三类实体独立采集：

| 实体类型 | 数据采集器 | API 链路 | 说明 |
|---------|-----------|---------|------|
| **Subject**（作品） | `SubjectCollector` | 单次 `GET /p1/subjects/{id}` | 全量数据（评分、标签、简介、infobox），无需 multi-hop |
| **Character**（角色） | `CharacterEnricher` | `GET /p1/characters/{id}` + `/casts` | 并发双请求，casts 端点补全关联作品边 |
| **Person**（人物） | `PersonEnricher` | `GET /p1/persons/{id}` + `/works` | 并发双请求，works 端点补全代表作边 |

**设计原则：**
- 异步优先（全 async + `httpx`）
- `asyncio.Semaphore` 并发控制（默认 3） + 请求间休眠限流（0.15–0.2s）
- 永不抛异常：API 失败返回 `{"_error": "..."}`，单个实体失败不影响批次
- 批量灌入脚本 `rag/cli/ingest.py`（`python -m rag.cli.ingest`）编排全流程：ID 发现 → 富化 → 灌入
  - ID 发现来源：trending / calendar / 关键词搜索 / 已知经典 ID 列表
  - 目标规模：250 Subject + 120 Character + 80 Person
  - 热度筛选策略：前 85%（热度段） + 尾 15%（冷门留存）

### 1.2 文本分块 (Chunking)

**方案：单实体单块 (One Entity, One Chunk)**

Bangumi 摘要文本普遍 < 300 tokens，无需滑动窗口切分。原始的 `text_processor.py`（`BangumiTextProcessor`，含 tiktoken 滑动窗口 chunking 和 Parent-Child Retriever 模式）已完全废弃。

当前策略：
- `_build_chunk_text()` 拼接 `summary` + `info` 字段，以句号连接
- 硬截断上限：3000 字符（防止 embedding API token 溢出），优先在句号处断开
- 每个实体（Subject / Character / Person）生成一个 `chunk_text`

### 1.3 向量化 (Embedding)

**模型：** 智谱 embedding-2 (1024 维)

**核心设计——防稀释语义前缀 (Semantic Prefixing)：**

在 embedding 前，为每种实体类型拼接极简自然语言定调前缀，防止机械键值对模板词稀释大模型 Embedding 语义质心：

| 实体类型 | 前缀格式 | 示例 |
|---------|---------|------|
| Subject | `[作品名] {name_cn}。{chunk_text}` | `[作品名] 进击的巨人。巨人支配一切的世界…` |
| Character | `[角色] {name_cn}，出自《{subject_name}》。{chunk_text}` | `[角色] 艾伦·耶格尔，出自《进击的巨人》。…` |
| Person | `[人物] {name_cn}。{chunk_text}` | `[人物] 梶裕貴。日本男性声优…` |

**向量化流程：**
1. 文本清洗（`_clean_text()`）：HTML unescape → 去首尾引号 → 全角空格转半角 → 移除零宽字符 → 统一换行符 → 压缩连续空白
2. 拼接语义前缀
3. 批量调用 Zhipu embeddings API（每批 32 条，符合 API 限制：单条 ≤512 tokens，总输入 ≤8K tokens）

### 1.4 存储与索引 (Storage & Indexing)

**架构：单表多态 (Single Table Polymorphism)**

三类实体共用一张 `rag_entities` 表，通过 `entity_type` 列区分：

```
rag_entities
├── id (PK, str)            — 前缀化主键: subject_10 / character_5 / person_3
├── entity_type (str, idx)   — subject / character / person
├── name (str, idx)          — 原文名称
├── name_cn (str, nullable)  — 中文名称
├── nsfw (bool, idx)         — 安全护栏
├── popularity (int, idx)    — 热度信号（subject→rating_total, 其他→collects）
├── chunk_text (str)         — 文本块内容
├── embedding (Vector(1024)) — pgvector 向量
└── meta_info (JSONB)        — 反范式化元数据（Pydantic v2 契约校验）
```

**索引策略：**
| 索引 | 类型 | 用途 |
|------|------|------|
| `embedding` | HNSW + vector_cosine_ops | 向量语义检索 |
| `entity_type` | B-Tree | 标量前置过滤 |
| `nsfw` | B-Tree | 安全护栏快速剔除 |
| `popularity` | B-Tree | 热度降序排序 |
| `name` | GIN + trigram | 模糊匹配 / 回溯查询 |
| `chunk_text` | GIN + trigram | 全文模糊匹配 |

**关联边内存洗牌与重排 (In-Memory Re-sorting & Pruning)：**

入库前，对照本地数据库中 Subject 的 `rating_total` 热度数据，在 Python 内存中对 Character 的 `casts` 列表和 Person 的 `works` 列表按关联作品热度降序重排，截断至 Top 10。同一作品不同版本（TV/总集篇）去重。

**Pydantic v2 契约校验：**
- `SubjectMeta`：score, rank, rating_total, rating_count[10], collection, date, year, platform, eps, tags[]
- `CharacterMeta`：role, collects, summary, info, casts[]（关联边，`CharacterCast` 契约）
- `PersonMeta`：career[], type, collects, summary, info, works[]（关联边，`PersonWork` 契约）

---

## 2. 查询理解与路由 (Query Understanding & Routing)

### 2.1 意图识别与路由 (Query Routing)

查询路由不在 RAG 模块内部完成，而是由上游 Agent 编排层的意图分类器（`orchestrate/classifier.py`）处理：

- 用户输入 → `classify_node` → 7 intent 分类（chat / fetch / explore / discuss / realtime / profile / fallback）
- `fetch` / `explore` / `discuss` / `fallback` 等 intent 触发 LLM function calling → 调用 RAG 搜索工具
- 检索层通过 `entity_type` 参数支持域限定：`"subject"` / `"character"` / `"person"` / `"all"`
- LLM 根据用户意图自主选择检索域（如"找个角色"→ character，"这个番怎么样"→ subject）

### 2.2 查询重写 (Query Rewrite)

**无。** 依赖上游 Agent 的 LLM reasoning 能力，由 reasoning_node 在 multi-turn 对话中自然完成隐式查询重写，RAG 模块只接收最终的自然语言查询字符串。

### 2.3 查询拆解 (Query Decomposition)

**无。** 复杂查询由上游 reasoning_node 通过 function calling 拆解为多个独立工具调用（如分别查 subject 后再查其 character），每次调用独立走一遍 RAG 检索。

---

## 3. 检索与重排 (Retrieval & Reranking)

### 3.1 初步检索 (Retrieval)

`RagEntityRetriever.hybrid_search()` — **标量前置过滤 + 向量语义检索**：

```
┌─────────────────────────────────────────────────────┐
│  输入: query (自然语言), entity_type, limit          │
├─────────────────────────────────────────────────────┤
│  Step 1: 查询向量化                                  │
│    query → _clean_text() → Zhipu embedding-2 → vec  │
├─────────────────────────────────────────────────────┤
│  Step 2: 标量前置过滤 + 向量召回                      │
│    WHERE entity_type = ?  (硬编码，B-Tree 快速过滤)   │
│    WHERE nsfw = false      (安全护栏)                │
│    ORDER BY embedding <=> query_vec  (余弦距离升序)   │
│    LIMIT limit * 2          (2x 候选集)              │
├─────────────────────────────────────────────────────┤
│  Step 3: 距离阈值防爆                                │
│    cosine_distance > 0.65 → 丢弃（防语义不相关幻觉）   │
├─────────────────────────────────────────────────────┤
│  Step 4: 多态阶梯分桶排序 (见 3.2)                   │
├─────────────────────────────────────────────────────┤
│  Step 5: MMR 同名去重                                │
│    name_cn / name 相同 → 仅保留首条                  │
├─────────────────────────────────────────────────────┤
│  Step 6: 截断至 limit 条返回                         │
└─────────────────────────────────────────────────────┘
```

**关键设计决策：**
- 余弦距离由 PGVector 在 SQL 层直接计算并返回，Python 层不重算（避免 1024 维浮点重复计算与精度偏差）
- `entity_type != "all"` 时的硬编码 WHERE 子句将向量比对限制在特定领域内，大幅缩减候选集
- 召回 `limit * 2` 候选集，给后续分桶排序和去重留出裁量空间

### 3.2 重排序 (Reranking)

**多态阶梯分桶排序 (Multi-Morphic Tiered Bucket Ranking)：**

```
梯队 ID = int(cosine_distance / semantic_bucket_size)   # bucket_size = 0.03

排序键 = (梯队 ID 升序, -heat_signal 降序)
         \___________/    \________________/
         语义优先        同梯队内热度打破平局
```

**三原则：**

1. **语义优先：** 梯队 ID 是第一主键。相邻梯队之间语义有质的分野，绝不因热度跨越梯队
2. **对数归一化防碾压：** 热度信号经 `log(1 + raw)` 归一化，避免头部热门作品在同语义梯队内对冷门作品形成数量级碾压
3. **分域动态热度信号：**
   - Subject → `popularity` 列 = `rating_total`（评分人数）
   - Character / Person → `popularity` 列 = `collects`（收藏数）
   - 热度值已从 `meta_info` JSONB 冗余提取为列级字段，走 B-Tree 索引高速排序

**消融开关（面向评估，生产环境全开）：**
- `enable_threshold`：距离阈值过滤
- `enable_bucketing`：语义分桶 + 对数归一化（关闭时降级为纯 cosine 排序）
- `enable_mmr`：同名去重

### 3.3 上下文过滤与压缩 (Context Compression)

**无独立 LLM 压缩环节。** 通过距离阈值过滤（丢弃 > 0.65）+ top-N 截断完成隐式压缩。实际 RAG 结果作为工具返回值注入 Agent 的 reasoning node system prompt，由 LLM 在推理过程中自主判断信息相关性。

---

## 4. 增强、生成与反思 (Augmentation, Generation & Reflection)

### 4.1 提示词增强 (Augmentation)

RAG 检索结果注入 Agent 系统 prompt 的方式：
- `RagSearchResult` 模型承载 `chunk_text` + `meta_info`（含评分、标签、关联边等结构化字段）
- 上游 `tools/bgm_tools.py` 中的工具函数（如 `search_local_bangumi`）封装 RAG 检索调用，返回结构化 dict
- Agent 的 `reasoning_node` 或 Pipeline 的 `synthesize` 节点将工具返回的多条 RAG 结果整合到 prompt 中

### 4.2 回答生成 (Generation)

生成阶段完全在 RAG 模块之外，由 Agent 编排层处理：
- **Pipeline intent**（fetch/realtime/profile）：`synthesize` 节点用独立 prompt 对多个工具结果做汇总生成
- **ReAct intent**（explore/discuss/fallback）：`reasoning_node` 自主决定何时终止探索并生成最终回复
- **Chat intent**：跳过 RAG，`main.py` 中直接 render

### 4.3 评估与反思 (Reflection / Self-Correction)

**无。** Critic 节点（对 RAG 检索质量做 self-check 并可能触发重检索）已在 Phase 4 从 graph 中移除。当前 pipeline 中没有显式的检索质量反思或自我修正环节。

---

## 5. 规范化测评与监控 (Standardized Evaluation & Monitoring)

### 5.1 检索质量评估 (Retrieval Evaluation)

**消融实验框架已内置，正式评审体系待建立。**

`hybrid_search()` 提供三个消融开关（`enable_threshold`、`enable_bucketing`、`enable_mmr`），设计上支持 A/B 对照实验以量化各组件的独立贡献。但当前仅依赖：
- `test/test_rag.py` 中的集成测试（端到端 ingest → search roundtrip 验证）
- 消融开关的默认全开策略（生产环境下不进行对比）

**暂无指标：** Context Precision、Context Recall 等标准化检索质量指标未被系统性采集。

### 5.2 生成质量评估 (Generation Evaluation)

**无。** Faithfulness、Answer Relevance 等生成质量指标不由 RAG 模块负责，也未在 Agent 层建立系统性评测管线。

### 5.3 端到端效用评估 (End-to-End Evaluation)

**无。** 当前无端到端 RAG 效用的标准化评估流程。

---

## 6. 竞品对比与改进方向 (Benchmarks & Improvement Roadmap)

### 6.1 同类项目

ACG 数据库 RAG 领域有两个成熟度较高的参考项目：

#### AiMi — 大规模 Anime RAG 推荐引擎

开源项目（Dev.to, 2025），从 AniDB + MyAnimeList 聚合 8,248 部作品（1917–2025），构建完整的 RAG 推荐 pipeline。与我们最相关的设计决策：

| 维度 | AiMi | BGM Agent |
|------|------|-----------|
| 语料规模 | 8,248 部 | ~450 条（Subject + Character + Person） |
| 检索 | Dense 向量 + BM25 关键词加权 | 纯 Dense 向量 |
| Query 增强 | HyDE（本地 Qwen-2.5-1.5B） | 无 |
| Embedding | Nomic v1.5（本地部署） | 智谱 embedding-2 (API) |
| 定位 | 推荐引擎 | Companion agent |

**核心差异：** AiMi 的定位是推荐系统（"给我找一部治愈系动画"），对检索的精准度要求比问答型 agent 更高——推荐错一部就是一次糟糕的用户体验。因此他们在向量检索之上加了 BM25 和 HyDE 两层保险。

#### AniContacts — Bangumi 角色扮演 Chatbot

小红书上的中文项目（2025），**直接基于 Bangumi API**，与我们数据源相同。技术栈：LangChain + FAISS 本地向量库 + SQLite + 通义千问 API。

**核心差异：** AniContacts 额外爬取了萌娘百科和 MyAnimeList 的角色背景文本，使得 `chunk_text` 的信息密度远高于纯 Bangumi `summary`。定位是角色扮演对话，而非通用 ACG 问答。

### 6.2 四个改进方向

从竞品对比和自身架构 review 中提炼出四条按优先级排列的改进方向。

#### P0 — 关键词加权层：补充词法匹配能力

**问题：** 当前检索是纯 Dense 向量，对 ACG 领域的精确专有名词（"京阿尼""PA社""水星的魔女"）召回能力弱。`name` / `name_cn` 列的 GIN trigram 索引建成后从未被 `hybrid_search()` 使用。

**启示来源：** AiMi 的 BM25 关键词加权——提取 query 中长度 > 4 的稀有名词，在召回结果的 `chunk_text` 中做子串匹配，每命中一个词将向量距离缩小 5%（上限 15%）。

**可行方案：** 不引入外部 BM25 引擎。在 `hybrid_search()` 的分桶排序后、MMR 去重前，加一层轻量字符串匹配：

```
对 query 分词 → 过滤停用词和短词（≤3 字符）
  → 对每条候选结果: 检查 name / name_cn / chunk_text 是否包含该词
  → 命中则缩小 cosine_distance（提升排名）
```

约 40 行 Python，零新依赖。直接利用了表里已有但从未被检索使用的 `name` 和 `name_cn` 列。

#### P0 — chunk_text 多源富化：提升被向量化文本的信息密度

**问题：** 当前 `chunk_text` 仅包含 `summary + info`（自然语言简介）。对于 Character 和 Person，简介常常只有一句话甚至为空，导致向量检索几乎没有有效的语义信号。与此同时，`meta_info` JSONB 中存储的 tags、casts、works 等结构化数据在检索阶段完全不可见——它们只作为结果返回，不参与检索。

**启示来源：** AniContacts 从萌娘百科和 MyAnimeList 多源抓取角色背景文本，使 `chunk_text` 远丰富于纯 Bangumi summary。

**可行方案（不引入新数据源，仅利用已有 JSONB 数据）：**

| 实体 | 当前 chunk_text | 富化后 |
|------|----------------|--------|
| Subject | `[作品名] {name_cn}。{summary}。{info}` | 追加：`标签: {top5 tags}。` |
| Character | `[角色] {name_cn}，出自《{subject_name}》。{summary}。{info}` | 追加：`主要作品: {casts 中的 subject 名称列表}。饰演({role_type})。` |
| Person | `[人物] {name_cn}。{summary}。{info}` | 追加：`代表作: {works 中的 subject 名称列表}。职业: {career}。` |

不改 ingestion 外部接口——仅在 `_build_*_chunk_text()` 内部追加字段。re-embed 现有 450 条实体即可生效。**注意：** 富化会增大 `chunk_text` 长度，需确保不超过 embedding-2 的 512 token 限制。

#### P1 — Query Rewrite：用 HyDE 补上查询理解缺口

**问题：** RAG 模块接收的 `query` 来自上游 LLM function calling，可能包含指代词（"那部番的男主"）、否定式表达（"不要后宫"）、或过于口语化的描述。这些 query 与知识库中正式描述的向量距离很远，导致检索失败。

**启示来源：** AiMi 的 HyDE（Hypothetical Document Embeddings）——用本地小模型把用户 query 展开成一段假设的理想文档描述，再对该假设文档做 embedding 检索：

```
用户 query: "不要后宫"
    ↓ LLM 展开
假设文档: "这是一部以纯爱为主题的作品，讲述了一对一恋爱关系..."
    ↓ embedding(假设文档) ⇔ 知识库向量
匹配: "純爱、一対一の恋爱" ← 与假设文档同语义空间
```

**可行方案（两种场景，两种策略）：**

| 问题类型 | 策略 | 成本 |
|---------|------|------|
| 指代词消解（"那部番"→"进击的巨人"） | 注入对话历史上下文，规则化替换 | 零额外 LLM 调用 |
| 语义翻译（"不要后宫"→正面描述） | HyDE：一次轻量 LLM 调用展开 query | ~100 tokens/次 |

建议先在 `search_local_bangumi` 工具函数中做指代词消解（廉价），再评估 HyDE 的延迟增量是否可接受。HyDE 在 deep 模式下打开发挥最大效用，fast 模式下可跳过。

#### P2 — 可观测性：RAG 检索链路 trace

**问题：** 当前 `hybrid_search()` 只有一条汇总 info log。当用户反馈检索结果不对时，无法回溯是哪个环节出了问题——embedding 失准？阈值过滤误杀？分桶排序把正确结果排到后面了？MMR 去重去掉了？

**启示来源：** AniContacts 在 LangSmith 上可视化调试 RAG 链路。通用 RAG 最佳实践中，对检索管道的每个阶段独立埋点是排障的前提。

**可行方案：** 不引入外部平台。在 `hybrid_search()` 返回结果中附带一个轻量 `trace` dict：

```python
{
    "query": "水星的魔女",
    "query_len_chars": 5,
    "candidates_raw": 10,
    "after_threshold": 6,
    "after_bucketing": 6,
    "after_dedup": 4,
    "final": 3,
    "top3": [
        {"name": "機動戦士ガンダム 水星の魔女", "cos_dist": 0.12, "bucket": 4},
        ...
    ],
    "threshold_discarded": [
        {"name": "星方武侠アウトロースター", "cos_dist": 0.72},
        ...
    ]
}
```

生产环境仅在以下条件输出 trace：`cos_dist > 0.4`（top1 与 query 语义距离异常大）或 top1 的 `name` 与 query 无任何子串匹配。DEV_MODE 下全量输出。

### 6.3 优先级总览

| 优先级 | 方向 | 做什么 | 新增依赖 | 收益 |
|--------|------|--------|---------|------|
| **P0** | 关键词加权 | `hybrid_search()` 加 40 行字符串匹配层 | 无 | 精准名词命中率大幅提升 |
| **P0** | embed_text 分离 | 新增 `embed_text` 列，与 `chunk_text` 分离（检索用 vs 消费用） | 无（需 re-embed） | 解除检索与消费的文本格式冲突，chunk_text 富化不再受限 |
| **P1** | Query Rewrite | 指代词消解（规则）+ HyDE（轻量 LLM） | 无（HyDE 复用现有 LLM） | 终结"那部番的男主"类失败 |
| **P2** | 可观测性 | 检索 trace dict + 条件日志输出 | 无 | 从猜测问题变为定位问题 |
| **P2** | meta_info JSONB 索引 | `meta_info` 列加 GIN 索引，支持标签/年份等结构化过滤 | 一条 DDL | 实现"2010 年后的科幻机战番"类精确过滤 |

### 6.4 数据库表设计启示：竞品与行业标准

两个直接竞品均未公开完整数据库 schema（AiMi 闭源，AniContacts 无文档），但 NamuWiki Anime RAG 数据集（HuggingFace）的公开 schema 和 pgvector 行业设计指南提供了有价值的参考。

#### 启示 A（已验证）：单表多态设计是领域最优解

行业标准的 `documents → document_chunks` 双表范式为**多 chunk 文档**设计：

```sql
-- 行业标准范式
documents (id, title, entity_type, source_url, metadata JSONB)
document_chunks (id, document_id FK, content, embedding, chunk_index, metadata JSONB)
```

此范式的驱动力是"一个长文档 → 多个 chunk"的切分需求——每次检索命中一个 chunk，需要通过 `document_id` 回溯到父文档以获取完整元数据。

**此范式不适用于我们的领域：** Bangumi summary 普遍 200–500 tokens，无需切分。一个实体 = 一个 chunk，父子表分离只会增加 JOIN 开销而无实际收益。

`rag_entities` 单表多态（`entity_type` 区分 Subject/Character/Person）在当前数据特征下是**恰好匹配的最优设计**，无需修改。

#### 启示 B：`chunk_text` 和 `embed_text` 应分离

NamuWiki 和 AiMi 共同指向一个关键设计决策：**"被 embedding 的文本"和"返回给用户的文本"应存储为两个独立字段。**

NamuWiki 的显式设计：

```
chunk:
  text:                 "鬼滅の刃は、吾峠呼世晴による..."  ← 消费用
  text_for_embedding:   "[作品名] 鬼滅の刃。[标签] 热血 战斗 時代劇... [简介] ..."  ← 检索用
```

AiMi 用 `canonical_embedding_text` 实现同样的分离——该字段不是自然语言简介，而是人工混合 Themes + Character Archetypes + Emotional Tone 的合成文本。

**我们当前的问题：** `chunk_text` 一个字段同时承担两个冲突的职责：

| | 用于 embedding（检索） | 用于 context（Agent 消费） |
|---|---|---|
| 需要什么 | 关键词密集、语义信号强 | 可读性好、格式干净 |
| 适合长度 | ≤512 tokens | 越长越好（给 LLM 更多上下文） |
| 格式 | 可拼接 tags/casts/works 强化检索信号 | 自然语言优先 |

混用导致两件事都做不好：
- 不敢往 `chunk_text` 加 tags/casts（怕破坏消费侧可读性）→ 检索信号不足
- `meta_info` JSONB 中存储的结构化数据检索时完全不可见 → 信息浪费

**改进方案：** 新增 `embed_text` 列（仅用于 embedding 向量化），`chunk_text` 保持纯自然语言格式（仅用于返回 Agent）。

```
rag_entities:
  chunk_text:  TEXT       -- [消费用] 自然语言，返回给 Agent 做上下文
  embed_text:  TEXT       -- [检索用] 关键词密集合成文本，仅用于 embedding
  embedding:   Vector ←   -- 对 embed_text 做向量化
```

`embed_text` 可以激进拼接：
- Subject：`[作品名] {name_cn}。[标签] {top tags}。[评分] {score}。[年代] {year}。[简介] {summary}`
- Character：`[角色] {name_cn}，出自《{subject_name}》。[出演] {casts 列表}。[简介] {summary}`
- Person：`[人物] {name_cn}。[代表作] {works 列表}。[职业] {career}。[简介] {summary}`

**成本：** `rag_tables.py` 加一列 + ingestion 加 `_build_embed_text()` 构造函数 + re-embed 一次。检索链路接口不变，仅 embedding 的输入文本从 `chunk_text` 切换到 `embed_text`。

#### 启示 C：GIN 索引应覆盖 meta_info JSONB，而非仅 name/chunk_text

当前索引策略：

```sql
-- 现有：GIN trigram 在文本列上
CREATE INDEX ON rag_entities USING gin (name gin_trgm_ops);
CREATE INDEX ON rag_entities USING gin (chunk_text gin_trgm_ops);
```

行业标准建议增加：

```sql
-- 建议增加：GIN 在 meta_info JSONB 上，用于运行时结构化过滤
CREATE INDEX ON rag_entities USING gin (meta_info jsonb_path_ops);
```

这将使 `WHERE meta_info @> '{"tags": [{"name": "科幻"}]}'::jsonb` 或 `WHERE meta_info->>'year' >= '2010'` 这类结构化过滤条件走索引而非全表扫描，在 ACG 场景下实用（"找 2010 年之后的科幻机战番"）。

#### 启示 D：稳定 ID 体系已自然具备

NamuWiki 和行业标准强调的 SHA1 稳定 ID 用于：
- 幂等重索引（同一实体多次摄入不会产生重复记录）
- 跨系统引用（chunk_id 可安全用作外键）

我们的 `subject_10` / `character_5` / `person_3` 前缀化 Bangumi ID 天然满足此需求——Bangumi ID 是官方分配的唯一标识，不会变动。`session.merge()` 行为保证幂等。无需改动。

#### 数据库设计结论

| 改动 | 优先级 | 成本 | 状态 |
|------|--------|------|------|
| 单表多态保持不动 | — | — | **已有，设计正确** |
| 新增 `embed_text` 列，与 `chunk_text` 分离 | **P0** | 加一列 + re-embed | **新发现，应纳入计划** |
| meta_info JSONB 加 GIN 索引 | P2 | 一条 DDL | **新发现，低优先** |
| 稳定 ID 体系 | — | — | **已有，无需改动** |

---

## 附录：模块文件清单

| 文件 | 职责 | 状态 |
|------|------|------|
| `rag/ingestion.py` | 文本清洗、语义前缀构造、embedding 批量化、`rag_entities` 表写入 | **活跃** |
| `rag/retriever.py` | 标量前置过滤 + 向量检索 + 多态分桶排序 + MMR 去重 | **活跃** |
| `rag/enricher.py` | Bangumi API 数据富化（Character/Person multi-hop + Subject 单步采集） | **活跃** |
| `rag/text_processor.py` | 滑动窗口 chunking + Parent-Child Retriever | **已废弃** |
| `rag/__init__.py` | 模块导出 | **活跃** |
| `database/rag_tables.py` | `RagEntity` ORM 定义 + Pydantic v2 Meta 契约模型 + 索引 DDL | **活跃** |
| `rag/cli/ingest.py` | 批量语料灌入脚本（ID 发现 → 富化 → 灌入） | **活跃** |
| `rag/cli/discover.py` | 语料 ID 发现脚本（p1 API + HTML 解析） | **活跃** |
| `rag/eval/` | RAG 评测管线（--build / --evaluate） | **活跃** |
| `rag/_utils.py` | 内部共享工具（_clean_text, _first_sentence） | **活跃** |
| `test/test_rag.py` | 集成测试（retriever 正确性 + ingest→search roundtrip） | **活跃** |
