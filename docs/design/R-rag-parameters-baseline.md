# RAG 管线参数 Baseline

> **文档性质**：当前生产环境的 RAG 参数快照，用于 eval 对比和调参追踪。
> **更新日期**：2026-08-03
> **快照范围**：切分策略、Embedding 模型、向量索引、检索管线、语义前缀策略

---

## 1. 文本切分（Chunking）

| 参数 | 值 | 位置 |
|------|-----|------|
| 切分策略 | 滑动窗口（基于 tiktoken） | `rag/text_processor.py:56` |
| Tokenizer | `cl100k_base` (GPT-4 同款) | `rag/text_processor.py:46` |
| chunk_size | 300 tokens | `rag/text_processor.py:28` |
| chunk_overlap | 50 tokens | `rag/text_processor.py:29` |
| 清洗步骤 | HTML unescape → 去引号 → 全角空格→半角 → 连续换行折叠 → 连续空格折叠 | `clean_text()` |

**理论依据**：
- `cl100k_base` 对中日文混合文本的 token 计数比 `len()//4` 精确 10-15%（实测误差），避免低估日文文本的 token 消耗。
- 300 tokens 约等于 ~200 中文字或 ~450 日文假名——足够承载一个作品的简介段落 + 核心标签。
- 50 tokens overlap ≈ 17% 重叠率，在语义连贯性和存储冗余之间取平衡。

---

## 2. Embedding 模型

| 参数 | 值 | 位置 |
|------|-----|------|
| 模型 | 智谱 `embedding-2` | `core/config.py:EMBEDDING_MODEL` |
| 维度 | **1024** | `core/config.py:EMBEDDING_DIMENSION` |
| API | `zai-sdk` → `open.bigmodel.cn/api/paas/v4/embeddings` | `rag/ingestion.py:152` |
| 批量 | 全量 batch（受单次 API token 上限约束） | `rag/ingestion.py:_embed_batch()` |

**历史注记**：
- 原使用 `embedding-3` (2048d)，但 pgvector HNSW 索引硬上限 2000d → 降级至 `embedding-2` (1024d)。
- pgvector 0.7.0+ 已解除 2000d 限制，未来可考虑升回 2048d。

---

## 3. 向量索引

| 参数 | 值 | 位置 |
|------|-----|------|
| 索引类型 | **HNSW** (Hierarchical Navigable Small World) | `database/rag_tables.py:46` |
| 距离算子 | `vector_cosine_ops` (余弦距离) | `database/rag_tables.py:46` |
| 索引列 | `rag_entities.embedding` (1024d) | `database/rag_tables.py:111` |
| 全文辅助索引 | GIN + `pg_trgm`（name, chunk_text） | `database/rag_tables.py:50-53` |
| 标量索引 | `entity_type`, `name`, `nsfw` B-Tree | `database/rag_tables.py:86-105` |

**HNSW 参数**：使用 pgvector 默认值（`m=16, ef_construction=64`）。

**为什么不是 IVFFlat**：
- IVFFlat 需要预建聚类中心，数据量小（<10K）时聚类质量差。
- HNSW 图结构在小数据量下即有效，且插入新数据不需重建索引。
- pgvector HNSW 查询延迟 O(log N)，IVFFlat O(sqrt(N))。

---

## 4. 检索管线（5 阶段）

| 阶段 | 参数 | 值 | 位置 |
|------|------|-----|------|
| Step 1: 标量预过滤 | `entity_type` | WHERE 硬过滤 | `retriever.py:hybrid_search()` |
| Step 1: 安全护栏 | `nsfw` | `WHERE nsfw = FALSE` | 同上 |
| Step 2: Embedding | query → embedding-2 (1024d) | 同 §2 | 同上 |
| Step 3: 向量召回 | candidate_limit | `limit * 2` (=10) | `retriever.py` |
| Step 4: 距离阈值 | `distance_threshold` | **0.65** (余弦距离) | `retriever.py` |
| Step 5: 语义分桶 | `semantic_bucket_size` | **0.03** | `retriever.py` |
| Step 5: 桶内排序 | 热度对数归一化 | `log(1 + rating_total)` | `_extract_heat_signal()` |
| Step 5.5: MMR 去重 | λ (diversity weight) | 1.0 (纯去重) | `retriever.py` |
| Step 7: 截断 | limit | **5** | `retriever.py` |

### 5 阶段详解

```
Query → [Embedding]
  → Step 1: 标量预过滤 (entity_type=subject, nsfw=FALSE)
  → Step 3: 向量召回 (cosine, LIMIT 10)
  → Step 4: 距离阈值 (丢弃 cosine_distance > 0.65)
  → Step 5: 语义分桶 (步长 0.03 → 梯队 ID)
     → 桶内对数热度降序 (log(1 + rating_total))
  → Step 5.5: MMR 同名去重 (同 name → 保留距离更近的一条)
  → 截断至 top 5
```

### 消融开关

每个阶段都有独立开关（默认全开），用于消融实验：

```python
retriever.hybrid_search(
    query="進撃の巨人",
    enable_threshold=False,   # 关 Step 4
    enable_bucketing=False,   # 关 Step 5
    enable_mmr=False,         # 关 Step 5.5
)
```

---

## 5. 语义前缀策略（Anti-Dilution）

在 embedding 前拼接自然语言定调前缀，防止机械键值对模板词稀释语义质心。

| 实体类型 | 前缀模板 | 位置 |
|----------|---------|------|
| Subject | `[作品名] {name_cn}。{chunk_text}` | `ingestion.py:47-58` |
| Character | `[角色] {name_cn}，出自《{subject_name}》。{chunk_text}` | `ingestion.py:61-78` |
| Person | `[人物] {name_cn}。{chunk_text}` | `ingestion.py:81-92` |

---

## 6. 关联边内存重排

| 参数 | 值 | 位置 |
|------|-----|------|
| 排序依据 | 本地 `rag_entities` 中关联 subject 的 `rating_total` 降序 | `ingestion.py:159-179` |
| 截断上限 | Top **10** 代表作 | `ingestion.py:219` |
| 去重策略 | 同 `subject_id` 只保留热度更高的一条 | 同上 |

---

## 7. 查询侧参数

| 参数 | 值 | 说明 |
|------|-----|------|
| `limit` | 5 | 返回 top 5 结果 |
| `distance_threshold` | 0.65 | 余弦距离 > 0.65 丢弃（防幻觉） |
| `semantic_bucket_size` | 0.03 | 语义梯队步长 |
| `candidate_limit` | `limit * 2 = 10` | 向量召回初始候选数 |
| MMR λ | 1.0 | 当前为纯硬去重（不调 diversity） |

---

## 8. 已知局限

| 问题 | 影响 | 备注 |
|------|------|------|
| chunk_text 长度无硬上限 | person 简介可达 5000+ chars，超出 embedding API token 限制 | 需在 ingestion 前截断 |
| MMR λ=1.0 可能过度去重 | 同系列不同季的合法同名作品（如 TV版 vs 剧场版）被误删 | 可设 λ=0.7 平衡相关性与多样性 |
| HNSW 默认参数未调优 | `ef_search` 和 `m` 对小数据量可能非最优 | 待 eval 后决策 |
| 无 query 侧语义前缀 | 用户 query "EVA" → embedding 倾向于通用语义而非 ACGN 语义 | 可考虑 query 侧加 `[作品名]` 前缀 |
