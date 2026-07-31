# 宏观技术决策记录

> 2026-08-01 | 梳理全部宏观架构决策，回答面试中"为什么选 X 而不是 Y"类问题。
> 每个决策包含：我们选了什么、替代品有哪些、理论依据、已知 trade-off。

---

## 一、Agent 编排框架：LangGraph StateGraph

### 我们用的

LangGraph `StateGraph(AgentState)` → `add_node()` → `add_conditional_edges()` → `compile()`。

纯 ReAct 拓扑：

```
START → reasoning_node ⇄ tool_node → render_node → END
```

### 替代方案对比

| 方案 | 控制流 | 中间状态 | 节点插入 | 生产稳定性 |
|------|--------|---------|---------|-----------|
| **LangGraph StateGraph**（我们） | 显式图，完全可控 | 每步 State 可读写 | `add_node()` 一行 | API 相对稳定 |
| LangChain AgentExecutor | 黑盒 while loop | 不可观测 | 不支持 | 2024 年每月 breaking change |
| CrewAI / AutoGen | Multi-agent team | Agent 间通信 | 设计为多 agent | 快速迭代中 |
| 裸调 LLM API + while | 手写循环 | 完全可控 | 自行实现 | 自行维护 |
| Dify / Coze | 低代码平台 | 不透明 | 受限于平台 | 平台负责 |

### 为什么是 StateGraph

**理论依据 — Pregel 模型**: LangGraph 的设计哲学来自 Google [Pregel](https://research.google/pubs/pub37252/) 和 Apache Beam。核心理念是**显式状态机优于隐式循环**。在 agent 场景中：

1. **可中断性**: 图在每一步都是确定的。Crash recovery 只需要重放 state。
2. **可观测性**: 任何节点都可以在 state 上打 checkpoint。debug 时能看到每一步的完整上下文，而非只拿到最终输出。
3. **可扩展性**: 加一个节点不需要改现有节点。Critic 屏蔽就是证明——删除路由规则即可，节点代码保留原地。

**理论依据 — Gall's Law**: "一个有效的复杂系统总是从一个有效的简单系统演化而来。"我们从一开始就没有假设需要多个 agent。CrewAI/AutoGen 的核心价值是角色分工和 agent 间通信——当你只有一个 companion agent 时，这些是额外负担。

**为什么不用裸调 LLM API**: 裸调在 50 行内可以工作——然后你需要加 memory truncation、intent classification、state persistence、error recovery——不知不觉你就实现了一个更差的 LangGraph。Rich Sutton 的 [The Bitter Lesson](http://incompleteideas.net/IncIdeas/BitterLesson.html) 指出"利用算力的通用方法最终会胜出"。

### Known trade-off

- LangGraph 0.2.x 的 `stream_mode` 在版本间有 breaking change
- `ToolNode` 错误处理依赖 `handle_tool_errors` 参数——我们自维护了 `format_tool_error()` 防止堆栈信息泄漏到 LLM 上下文

---

## 二、LLM 选择：DeepSeek function-calling

### 我们用的

DeepSeek v3/v4，通过 OpenAI SDK compatible API（`base_url` 切换到 `https://api.deepseek.com/v1`），`temperature=0.3`，`bind_tools(tools)` 做 function calling。

### 替代方案对比

| 维度 | DeepSeek v3/v4 | GPT-4o | Claude (Anthropic) | Qwen |
|------|---------------|--------|-------------------|------|
| **Function calling** | 原生，OpenAI 兼容 | 原生（金标准） | 原生（tool_use，格式不同） | 原生 |
| **中文能力** | ⭐⭐⭐⭐⭐ | ⭐⭐⭐ | ⭐⭐⭐ | ⭐⭐⭐⭐ |
| **成本** | ~$0.14/1M input | ~$2.50/1M input (18×) | ~$3/1M input | ~$0.50/1M |
| **已知缺陷** | XML 泄漏 | 偶尔拒绝调用工具 | tool_use 格式非 OpenAI 兼容 | JSON 模式不稳定 |

### 核心 trade-off 分析

**GPT-4o 被排除**: 18× 成本差距。一个 deep 模式对话（12 轮，16000 tok 上下文）DeepSeek ~$0.002，GPT-4o ~$0.04。对免费产品乘以 1000 DAU 不可持续。

**Claude 被排除**: Anthropic 的 tool_use API 格式与 OpenAI `tool_calls` 不兼容。LangChain 的 `bind_tools()` 在 OpenAI 兼容模型上开箱即用——切换到 Claude 需要维护两套 tool schema。且 `ChatAnthropic` 与 `ChatOpenAI` 接口差异较大，`create_llm()` 工厂需要完全重构。

**XML 泄漏是已知代价**: DeepSeek 在解绑工具后有时会在 content 中输出 `<function_calls>` 标签——function-calling 微调数据包含 DSML 格式训练样本的副作用。通过 `guard_xml_leak()` 正则剥离防御。

**Qwen 是备选**: `create_llm()` 支持通过 `base_url` 无缝切换。成本相近，中文能力相近，但 DeepSeek 在 ACGN 领域的 function calling 更稳定。

### 理论依据

**没有"最好"的模型，只有"适合你的成本-能力 trade-off"的模型**。对于 companion agent——不需要代码生成精度（用 Claude）、不需要多模态理解（用 GPT-4V）、不需要超长推理链——DeepSeek 在中文对话 + function calling 上达到 95% 质量 × 1/18 成本。

---

## 三、Token 管理：tiktoken `cl100k_base`

### 我们用的

```python
_ENCODER = tiktoken.get_encoding("cl100k_base")
def count_tokens(text: str) -> int:
    return len(_ENCODER.encode(text))
```

### 替代方案对比

| 方案 | 原理 | 中文准确率 | JSON 准确率 | 开销 |
|------|------|-----------|------------|------|
| **tiktoken**（我们） | BPE tokenizer，模型训练时用的编码器 | ~100% | ~100% | 3MB 包 |
| `len(text)//4` | 1 token ≈ 4 chars（英文经验） | 低估 30-50% | 低估 40-60% | 零 |
| `len(text)//2` | 保守估计 | 低估 10-20% | 低估 20-30% | 零 |
| API 返回 `usage` | 让 LLM 返回真实 token count | 100% | 100% | 一次完整 API 调用 |
| HuggingFace tokenizer | 加载模型 tokenizer | ~100% | ~100% | ~2GB 依赖 |

### 为什么 tiktoken 是唯一正确的选择

**BPE 编码的本质**: `cl100k_base` 和 GPT-4/DeepSeek/Qwen 使用高度相似的 BPE 词汇表。中文单字占 1.5-2.5 tokens，JSON 的大括号/引号/冒号各占 1 token。`len//4` 在英文纯文本下误差较小，但在中文 + 结构化 JSON（ToolMessage 内容）下系统性低估。

**生产环境的教训**: Phase 1-4 用 `len//4`，production 触发 `context_length_exceeded` API 400。根因是 `search_bangumi_subject` 返回的 JSON 含大量中文字段，实际 token 数是估算的 1.4-1.6×。切换到 tiktoken 后该错误归零。

**为什么用 cl100k_base 而不是特定模型编码器**: DeepSeek 未公开 tokenizer。所有 OpenAI SDK 兼容模型的 BPE 词汇表高度相近，差异在 ±5%——对 token 预算管理完全够用。

**为什么不用 HuggingFace tokenizer**: 依赖 PyTorch/TensorFlow，安装包 ~2GB。tiktoken 纯 Rust + Python binding，3MB，零额外依赖。

### 理论依据

**Budget-aware context management**: token 计数的可靠性直接影响对话质量。高估 30% → 过早截断 → 上下文丢失。低估 30% → API 400 → 对话中断。tiktoken 是这条线的唯一正确答案。

---

## 四、向量索引：pgvector HNSW

### 我们用的

```sql
CREATE INDEX IF NOT EXISTS ix_rag_entities_embedding
    ON rag_entities USING hnsw (embedding vector_cosine_ops);
```

### 为什么不是 IVFFlat

| 维度 | IVFFlat | HNSW |
|------|---------|------|
| **原理** | K-means 聚类 → 查最近聚类 → 精确计算 | 多层可导航小世界图 → 贪心搜索 |
| **查询速度** | O(N × probes / nlists) | O(log N) |
| **召回率稳定性** | 依赖 probes 参数调优 | `ef_search=40` 通常 0.99+ recall |
| **参数敏感度** | nlists + probes 都需调 | m + ef_construction 选好即稳定 |
| **数据分布假设** | 假设数据天然聚类 | 无假设 |

### 理论依据

HNSW ([Malkov & Yashunin, 2018](https://arxiv.org/abs/1603.09320)) 构建了**多层跳表结构**：顶层稀疏（长距离跳跃），底层稠密（精确定位）。查询时从顶层贪心下降。理论 O(log N)，实际 2048 维下每个查询只需检查 40-100 个节点。

IVFFlat 的核心假设——数据天然形成紧密聚类——在 ACGN 文本 embedding 空间中不成立。embedding 空间是**弥散的**：《进击的巨人》的语义邻居不是"同类型动画"而是"训练语料中常共现的概念"。IVFFlat 的聚类代表性在这种分布下很差。

### Known trade-off

pgvector HNSW 索引不支持 2000d 以上的向量——这是从 Zhipu embedding-3 (2048d) 降级到 embedding-2 (1024d) 的原因之一。pgvector 社区计划 0.8 版本移除该限制。

---

## 五、向量数据库：pgvector vs 专用向量 DB

### 我们用的

PostgreSQL + pgvector 扩展。向量检索和关系型查询在同一事务中完成。

### 替代方案对比

| 维度 | pgvector（我们） | Pinecone | Weaviate | Milvus | Chroma |
|------|----------------|----------|----------|--------|--------|
| **部署** | PostgreSQL 扩展 | SaaS / 自建 | 自建 | 自建（需 K8s） | 嵌入 Python |
| **标量过滤** | 原生 SQL WHERE | 元数据过滤 | GraphQL | 标量索引 | 元数据过滤 |
| **JSONB 复杂查询** | ✅ PostgreSQL 原生 | ❌ | ❌ | ❌ | ❌ |
| **事务** | ✅ ACID | ❌ | ❌ | ❌ | ❌ |
| **运维成本** | 0（和业务 DB 同一实例） | $70+/月 | 需要运维 | 需要运维 | 0 |
| **规模上限** | ~10M 向量（单机） | 十亿级 | 十亿级 | 十亿级 | ~1M |

### 核心论证

1. **JSONB + pgvector 是 killer feature**: RAG 检索不是纯向量检索——每条查询都需要 `WHERE entity_type = 'subject' AND nsfw = false`。专用向量 DB 中需跨系统 join。pgvector 同一事务内完成——零额外网络 hop。

2. **数据规模不需要专用向量 DB**: rag_entities 目前 ~15K 条。pgvector HNSW 在百万级仍保持亚线性。千万级以上才有迁移 Milvus 的必要。

3. **不需要引入新的基础设施**: 应用已在用 PostgreSQL，引入 Pinecone 增加网络 hop + 认证层 + 故障域 + 月费。

### 理论依据

[Choose Boring Technology](https://boringtechnology.club/)——pgvector 足够 boring（社区维护 5+ 年），专用向量 DB 还在快速迭代。除非有明确规模需求，选择 boring 的。

---

## 六、Embedding 模型：Zhipu embedding-2 (1024d)

### 我们用的

Zhipu embedding-2，1024 维，通过智谱 SDK 调用。

### 替代方案对比

| 模型 | 维度 | 中文 ACGN 质量 | 成本 | 备注 |
|------|------|--------------|------|------|
| **Zhipu embedding-2**（我们） | 1024 | 好 | ~$0.07/1M tok | 远低于 HNSW 2000d 上限 |
| Zhipu embedding-3 | 2048 | 更好（待验证） | ~$0.07/1M tok | HNSW 索引不兼容 |
| OpenAI text-embedding-3-small | 1536 | 中等 | $0.02/1M | 中文弱于英文 |
| BGE-M3 (本地) | 1024 | 好 | 0 | 需要 GPU |
| M3E-large (本地) | 1024 | 好（中文优化） | 0 | 需要 GPU |

### 决策链

**为什么不用 OpenAI embedding**: 中文语料训练量远低于英文。"80年代黑暗机战动画" → "Armored Trooper VOTOMS" 这种跨语言语义映射，中文原生模型匹配质量更高。

**为什么用 embedding-2 (1024d) 而非 embedding-3 (2048d)**:
1. pgvector HNSW 索引硬上限 2000d——2048d 无法直接建 HNSW 索引
2. 维度翻倍 → 存储翻倍 → 检索延迟 +40%
3. "1024d vs 2048d recall 差异"本身是一个未经验证的问题

**为什么不用本地模型**: 15000 条数据一次 ingestion，API 调用成本 < $0.01。本地模型部署复杂度远超成本。百万级数据量时本地模型会更有吸引力。

### 理论依据

[No Free Lunch Theorem](https://en.wikipedia.org/wiki/No_free_lunch_theorem)——没有在所有领域都最优的 embedding 模型。选择的核心问题是"你的文本分布和模型的训练分布有多接近"。Zhipu 在国内中文语料上微调过的 embedding 模型更适配 Bangumi ACGN 中文描述。

---

## 七、记忆架构：L1 + L2（废弃 L3）

### 我们用的

```
L1 (短记忆): 滑动窗口 + 工具压缩 + SystemMessage 免疫 + tiktoken 精确编码
L2 (跨会话): pgvector 语义召回 + 时间衰减双通道
L3 (用户画像): Phase 5 废弃
```

### 为什么废弃 L3

1. **冷启动**: 新用户没有画像 → L3 注入空文本 → 无效果。绝大多数用户是新用户。
2. **偏好漂移**: 用户昨天聊 EVA 的深邃，今天聊异世界的快乐。L3 的 EMA 更新跟不上口味变化。
3. **L2 已覆盖 L3 的价值**: "3 天前聊过意识流"本身就是偏好信号——不需要独立 profile 层聚合。

### 为什么不能只有 L1

纯滑动窗口无跨会话记忆。每轮对话结束上下文全部清空。用户的下一次"你上次推荐的那部叫什么来着？"完全无法回答。

### 理论依据

[Atkinson-Shiffrin 记忆模型](https://en.wikipedia.org/wiki/Atkinson%E2%80%93Shiffrin_memory_model): L1 对应工作记忆（容量有限、快速访问），L2 对应长时记忆（需检索、有遗忘曲线）。时间衰减 `0.5^(days/14)` 模拟 [Ebbinghaus 遗忘曲线](https://en.wikipedia.org/wiki/Forgetting_curve)——记忆在最初几天衰减最快。

### 双通道召回逻辑

- **主通道（语义）**: cosine_distance ≤ threshold + 时间衰减。纯语义可能在新话题上召回为 0
- **Fallback 通道（时效）**: 语义锚定过滤（cosine_distance ≤ 0.60），补齐到 top-K。防止"高达→轻音"这种完全不相关的近期记忆成为噪音

---

## 八、RAG 管线：自定义 vs LangChain/LlamaIndex

### 我们用的

自定义 5 阶段管线：标量前置过滤 → 向量召回 → 距离阈值防爆 → 语义分桶 → MMR 去重。直接写 SQL + pgvector。

### 为什么没用 LangChain RetrievalQA

LangChain RAG 抽象为"通用文档检索"设计，预设同质文档集。我们的数据是三种非同质实体类型，每种需要不同热度信号做次级排序。`VectorStore.as_retriever(search_type="similarity")` 只给 cosine distance——管道到此结束。分桶和 MMR 无法插入。

### 为什么没用 LlamaIndex

`NodePostprocessor` 链适合标准文档后处理。但我们的管线是**领域特化的排序算法**——不是通用的 reranker。套用 LlamaIndex 需要把分桶+MMR 逻辑塞进 `CustomNodePostprocessor`——得到和直接写代码一样的逻辑，但多了一个不控制的抽象层。

### 理论依据

[The Law of Leaky Abstractions (Spolsky, 2002)](https://www.joelonsoftware.com/2002/11/11/the-law-of-leaky-abstractions/)——"所有非平凡的抽象在一定程度上都是泄露的。" LangChain 的 retriever 抽象在"需要 per-entity-type 的分桶排序"时泄露了。

---

## 九、数据模型：单表多态

### 我们用的

一张 `rag_entities` 表包含 Subject/Character/Person 三类实体。`entity_type` 列区分类型，前缀化主键（`subject_10`/`character_5`/`person_3`）防碰撞。实体特有字段存入 JSONB `meta_info`，入库前经 Pydantic Meta 契约校验。

### 替代方案

| 维度 | 单表多态（我们） | 多表继承 | EAV |
|------|----------------|---------|-----|
| **跨类型检索** | 1 条 SQL | UNION ALL 3 张表 | 多表 JOIN |
| **向量索引** | 1 个 HNSW | 3 个 HNSW（3× 内存） | 1 个 HNSW |
| **Schema 演化** | 改 Pydantic 契约 | ALTER TABLE | INSERT attribute |

### 为什么单表多态对我们有利

查询模式是 `向量相似度 → 热度排序 → top-K`——所有筛选在候选集很小（10 条）的内存中完成。不需要在 15000 行上做 JSONB WHERE 过滤。标量过滤仅用于高选择性字段（`entity_type`, `nsfw`——都有 B-Tree 索引）。pgvector HNSW 是 per-table 的——单表方案的 1 个 HNSW 覆盖全部实体就是最强理由。

### 理论依据

[Single Table Inheritance (Martin Fowler, PoEAA)](https://martinfowler.com/eaaCatalog/singleTableInheritance.html)——当子类型差异列较少且查询主要面向所有子类型时，单表继承最简洁。

---

## 十、HTTP 客户端：httpx (async)

### 我们用的

`httpx.AsyncClient`，连接池复用，指数退避重试（最多 3 次），30s 总超时/10s 连接超时。

### 为什么不能是 requests

FastAPI 是 async-first。请求、LangGraph `ainvoke()`、LLM API 都是 async。同步 `requests.get()` 会阻塞整个 event loop。httpx `AsyncClient` 支持连接池——16 个工具函数共享同一个 TCP 连接池，避免每次调用重新握手。

### 为什么不是 aiohttp

aiohttp API 风格自成一派，httpx 更接近 requests，且支持 HTTP/2。

---

## 决策速查表

| # | 决策 | 我们选的 | 最可能的替代 | 核心依据 |
|---|------|---------|------------|---------|
| 1 | Agent 框架 | LangGraph StateGraph | LangChain AgentExecutor | Pregel 模型：显式状态机优于隐式循环 |
| 2 | LLM | DeepSeek | GPT-4o | 18× 成本差距 vs 95% 质量 |
| 3 | Token 管理 | tiktoken cl100k_base | len//4 | 中文+JSON 场景 len//4 低估 30-50% |
| 4 | 向量索引 | HNSW | IVFFlat | ACGN 弥散 embedding 空间不满足 IVFFlat 聚类假设 |
| 5 | 向量 DB | pgvector | Pinecone/Weaviate | JSONB+向量同一事务；15K 规模不需分布式 |
| 6 | Embedding | Zhipu embedding-2 (1024d) | embedding-3 (2048d) | HNSW 2000d 上限 + 中文匹配质量 |
| 7 | 记忆架构 | L1 + L2（废弃 L3） | 纯 L1 | Atkinson-Shiffrin 模型；L3 冷启动+漂移 |
| 8 | RAG 方案 | 自定义 pgvector 管线 | LangChain/LlamaIndex | 5 阶段领域特化排序超出通用抽象 |
| 9 | 数据模型 | 单表多态 (JSONB) | 多表继承 | 1 个 HNSW 索引；跨类型检索零 UNION |
| 10 | HTTP 客户端 | httpx (async) | requests (sync) | FastAPI 生态；连接池复用 |

---

## 设计哲学总结

> **在正确的地方引入复杂度，在不需要的地方保持简单。**

- LangGraph 的图结构（复杂度）→ 可演化的拓扑，6 个月内从双 Agent 演进到纯 ReAct
- pgvector（简单）→ 否定了 Pinecone/Milvus，一个 PostgreSQL 扩展替代一个新服务
- 5 阶段 RAG（复杂度）→ 解决了 Bangumi 冷热悬殊 + 同系列刷屏，通用 retriever 无法表达
- tiktoken（精确）→ token 预算管理的确定性，替代了生产环境中触发 API 400 的字符估算
