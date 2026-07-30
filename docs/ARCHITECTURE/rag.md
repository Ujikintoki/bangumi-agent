# RAG 子系统 (rag/)

> 数据层子系统 · ~1557 行源码 | 状态：🟡 v0/v1 共存

---

## 一、架构概览

```
                      search_local_bangumi (tools/)
                             │
                             ▼
┌────────────────────────────────────────────────────┐
│                 rag/retriever.py                    │
│  ┌─────────────────────┐  ┌──────────────────────┐ │
│  │ RagEntityRetriever  │  │ BangumiRetriever     │ │
│  │ (v1, 活跃)          │  │ (v0, 已弃用)          │ │
│  │                     │  │                      │ │
│  │ hybrid_search():    │  │ hybrid_search():     │ │
│  │  1. query→向量      │  │  1. query→向量       │ │
│  │  2. 标量前置过滤     │  │  2. JSONB tags 硬过滤 │ │
│  │  3. 余弦距离召回     │  │  3. 余弦距离召回      │ │
│  │  4. 距离阈值过滤     │  │  4. 距离阈值过滤      │ │
│  │  5. 多态分桶排序     │  │  5. 分桶排序          │ │
│  │  6. MMR 同源去重     │  │                      │ │
│  └─────────────────────┘  └──────────────────────┘ │
└──────────────────────┬─────────────────────────────┘
                       │ 查询
                       ▼
┌────────────────────────────────────────────────────┐
│            PostgreSQL + pgvector                    │
│  ┌──────────────────┐  ┌─────────────────────────┐ │
│  │ rag_entities (v1)│  │ bangumi_chunks (v0)     │ │
│  │ 单表多态          │  │ 已弃用                   │ │
│  │ entity_type 区分  │  │ entity_type + entity_id │ │
│  │ 前缀化主键        │  │ UUID 主键               │ │
│  └──────────────────┘  └─────────────────────────┘ │
└────────────────────────────────────────────────────┘
                       ▲
                       │ 写入
┌────────────────────────────────────────────────────┐
│                rag/ingestion.py                     │
│  ┌─────────────────────┐  ┌──────────────────────┐ │
│  │ RagEntityIngestor   │  │ BangumiIngestor      │ │
│  │ (v1, 活跃)          │  │ (v0, 已弃用)          │ │
│  │                     │  │                      │ │
│  │ ingest_subjects()   │  │ ingest_chunks()      │ │
│  │ ingest_characters() │  │                      │ │
│  │ ingest_persons()    │  │                      │ │
│  └─────────────────────┘  └──────────────────────┘ │
└────────────────────────────────────────────────────┘
                       ▲
                       │ 文本预处理
┌────────────────────────────────────────────────────┐
│            rag/text_processor.py                    │
│            BangumiTextProcessor                     │
│  clean_text() → split_text() →                     │
│  create_entity_documents()                         │
│  (tiktoken cl100k_base, chunk_size=300, overlap=50)│
└────────────────────────────────────────────────────┘
```

---

## 二、RagEntity (v1) — 单表多态

### 2.1 设计动机

放弃传统多表 JOIN 方案，采用**单表多态（Single Table Polymorphism）**：

| 设计决策 | 原因 |
|---------|------|
| Subject/Character/Person 三种实体存同一张表 | 避免多表 JOIN 检索时的性能开销 |
| `entity_type` 列区分类型 | SQL 层标量前置过滤，缩减候选集 |
| 前缀化主键 `"subject_10"` | 防不同实体类型 ID 碰撞 |
| JSONB `meta_info` 存各实体特有字段 | 灵活 schema，避免空列 |
| Pydantic 契约模型校验 JSONB | 入库前强类型校验保证结构一致性 |

### 2.2 表结构

```sql
CREATE TABLE rag_entities (
    id          TEXT PRIMARY KEY,       -- "subject_10" / "character_5" / "person_3"
    entity_type TEXT NOT NULL,          -- "subject" / "character" / "person"
    name        TEXT NOT NULL,
    name_cn     TEXT,
    nsfw        BOOLEAN DEFAULT FALSE,
    chunk_text  TEXT NOT NULL,          -- 带语义前缀的文本块
    embedding   VECTOR(2048),           -- pgvector, 可为 NULL
    meta_info   JSONB DEFAULT '{}'      -- 反范式化元数据
);

-- 索引（由 init_db() 创建）
CREATE INDEX ix_rag_entities_embedding ON rag_entities
    USING hnsw (embedding vector_cosine_ops);    -- ⚠️ 2048d→创建失败
CREATE INDEX ix_rag_entities_name_trgm ON rag_entities
    USING gin (name gin_trgm_ops);
CREATE INDEX ix_rag_entities_chunk_text_trgm ON rag_entities
    USING gin (chunk_text gin_trgm_ops);
CREATE INDEX ix_rag_entities_nsfw ON rag_entities (nsfw);
```

### 2.3 JSONB meta_info 契约模型

入库前每种实体类型经过专用 Pydantic 模型校验：

```
Subject 入库:
  API 数据 → SubjectMeta.model_validate() → model_dump() → meta_info JSONB

Character 入库:
  API 数据 → CharacterMeta.model_validate() → model_dump() → meta_info JSONB
             └── casts: list[CharacterCast] (每项经校验)

Person 入库:
  API 数据 → PersonMeta.model_validate() → model_dump() → meta_info JSONB
             └── works: list[PersonWork] (每项经校验)
```

所有契约模型使用 `ConfigDict(extra="ignore")` — 未知字段静默丢弃。

**SubjectMeta 字段**：
```python
class SubjectMeta(BaseModel):
    score: float = 0.0
    rank: int = 0
    rating_total: int = 0           # 热度信号
    rating_count: list[int] = [0]*10  # 10 档评分分布
    collection: dict[int, int] = {}   # 5 种收藏状态分布
    date: Optional[str] = None
    year: Optional[int] = None
    platform: str = ""
    eps: int = 0
    tags: list[dict] = []            # [{name, count}, ...]
```

---

## 三、检索流水线（RagEntityRetriever.hybrid_search）

**文件**：`rag/retriever.py:134-296`

```
输入: query="80年代评分最高的机战番", entity_type="subject", limit=5

Step 1: 查询向量化
  query → 智谱 embedding-3 → query_embedding (2048d)

Step 2: 标量前置过滤 + 向量召回
  SELECT * FROM rag_entities
  WHERE entity_type = 'subject'    ← 标量硬过滤
    AND nsfw = FALSE               ← 安全护栏
  ORDER BY embedding <=> query_embedding  ← cosine_distance
  LIMIT 10                         ← limit × 2 = 候选池

Step 3: 组装候选集
  原始 SQL 行 → RagSearchResult Pydantic 模型

Step 4: 距离阈值过滤
  丢弃 cosine_distance > 0.65 的候选

Step 5: 多态阶梯分桶排序
  第 1 主键: int(cosine_distance / 0.03) 升序   ← 语义梯队
  第 2 主键: -log(1 + rating_total) 降序         ← 热度信号（对数归一化）

Step 5.5: MMR 同源去重
  同 name_cn 只保留热度最高的（防 TV/OVA/剧场版刷屏）

Step 6: 截断
  返回 deduped[:5]
```

### 3.1 热度信号对数归一化

```python
def _extract_heat_signal(meta: dict, entity_type: str) -> float:
    if entity_type == "subject":
        val = meta.get("rating_total", 0)
    elif entity_type in ("character", "person"):
        val = meta.get("collects", 0)
    return math.log(1 + val)  # 50,000→10.8, 200→5.3, 差距从 250×→2×
```

防止头部热门作品在同语义梯队内对冷门作品形成数量级碾压。

### 3.2 距离阈值含义

| 阈值 | 含义 | 使用场景 |
|------|------|---------|
| `0.65` (默认) | RAG 检索 | 防止完全不相关的结果进入候选 |
| `0.50` | 记忆召回 (deep) | 高语义相关 |
| `0.35` | 记忆召回 (非 deep) | 严格—对话跳跃性大 |

余弦距离范围 [0, 2]：0 = 完全相同，2 = 完全相反。

---

## 四、摄入流水线（RagEntityIngestor）

**文件**：`rag/ingestion.py:115-486`

### 4.1 通用流程

```
输入数据 (subjects/characters/persons)
  │
  ▼
Step 1: 语义前缀拼接
  subject:   "[作品名] 高达Seed。{chunk_text}"
  character: "[角色] キラ・ヤマト，出自《高达Seed》。{chunk_text}"
  person:    "[人物] 花泽香菜。{chunk_text}"

Step 2: 批量 Embedding
  texts[] → 智谱 embedding-3 → embeddings[][2048]

Step 3: 关联边重排 (仅 character/person)
  查询本地 rag_entities 中关联作品的 rating_total
  → 按热度降序重排 casts/works
  → 截断至 Top 10
  → Pydantic 模型校验

Step 4: 写入 rag_entities
  每条: id + entity_type + name + name_cn + nsfw +
        chunk_text(含前缀) + embedding + meta_info(经 Pydantic 校验)
  → session.commit()
```

### 4.2 语义前缀设计

直接对裸文本做 embedding 会稀释语义——"高达Seed"和"キラ・ヤマト"的 embedding 缺乏实体类型信息。前缀在 embedding 前注入自然语言上下文：

| 前缀 | 效果 |
|------|------|
| `[作品名]` | 告诉 embedding 模型"这是作品名"，区分于同名的角色/人物 |
| `[角色]，出自《X》` | 关联角色与作品，让"同一作品的角色"在向量空间中聚集 |
| `[人物]` | 标记实体类型 |

### 4.3 关联边内存重排

角色的 `casts`（出演作品列表）和人物理的 `works`（代表作列表）在内存中按关联作品的本地热度排序，截断至 Top 10。防止 100+ 条关联边稀释角色的向量表达。

```python
def _rerank_casts(self, session, raw_casts) -> list[CharacterCast]:
    # 1. 查本地 rag_entities 中各作品的 rating_total
    rating_map = self._lookup_subject_rating_map(session, subject_ids)
    # 2. 按 rating_total 降序排列
    sorted_casts = sorted(raw_casts, key=lambda c: rating_map.get(...), reverse=True)
    # 3. 同作品去重 + 截断至 10 条
    for c in sorted_casts[:10]:
        if prefixed_subject_id not in seen:
            casts.append(CharacterCast(...))
    return casts
```

---

## 五、BangumiChunk (v0) — 已弃用旧架构

### 5.1 状态

```python
class BangumiChunk(SQLModel, table=True):
    """[DEPRECATED] 番剧文本块的向量化存储模型。
    .. deprecated:: 此模型将在后续 Phase 中移除，请迁移至 RagEntity。"""
    __tablename__ = "bangumi_chunks"
    id: uuid.UUID          # UUID v4 主键
    entity_type: str       # "subject" / "character"
    entity_id: int         # Bangumi 数字 ID
    chunk_text: str
    embedding: Vector(2048)
    meta_info: JSONB
```

**v0 vs v1 对比**：

| 维度 | BangumiChunk (v0) | RagEntity (v1) |
|------|-------------------|----------------|
| 状态 | 已弃用，待移除 | 活跃 |
| 主键 | UUID | 前缀化字符串 |
| 实体 ID | 整数 `entity_id` | 字符串 `id` 含前缀 |
| 多态 | 仅 subject | subject + character + person |
| nsfw | meta_info JSONB 内 | 列级 BOOLEAN |
| 索引 | 仅 B-Tree (entity_type, entity_id) | B-Tree + GIN trigram + HNSW |
| Retriever | `BangumiRetriever` | `RagEntityRetriever` |
| Ingestor | `BangumiIngestor` | `RagEntityIngestor` |
| 语义前缀 | 无 | 有 (`[作品名]` / `[角色]` / `[人物]`) |
| 关联边重排 | 无 | 有 (热度降序，Top 10 截断) |

### 5.2 清理计划

`search_local_bangumi` 工具仅使用 `RagEntityRetriever` (v1)。旧版 `BangumiRetriever` 和 `BangumiIngestor` 无运行时消费者。`BangumiChunk` 表存在但为空。

清理步骤：
1. 确认所有测试仅依赖 `RagEntityRetriever`
2. 删除 `BangumiRetriever`、`BangumiIngestor`、`SearchResult`、`BangumiChunk` 类
3. 从 `rag/__init__.py` 移除导出
4. `DROP TABLE IF EXISTS bangumi_chunks`（通过迁移）

---

## 六、文本处理

**文件**：`rag/text_processor.py`（265 行）

### 6.1 BangumiTextProcessor

```python
class BangumiTextProcessor:
    def __init__(self, chunk_size=300, chunk_overlap=50):
        self.tokenizer = tiktoken.get_encoding("cl100k_base")

    def clean_text(self, text: str) -> str:
        # html.unescape → 去首尾引号 → 全角空格 → 零宽字符
        # → 统一换行符 → 折叠连续空格
        # 明确不剥离 BBCode（保留结构语义信号给 embedding）

    def split_text(self, text: str | None) -> list[str]:
        # Token 编码 → 滑动窗口 (步长=250) → Token 解码 → 裁剪乱码
```

### 6.2 分块参数

| 参数 | 值 | 说明 |
|------|-----|------|
| `chunk_size` | 300 tokens | 每块 Token 上限 |
| `chunk_overlap` | 50 tokens | 相邻块重叠 |
| 步长 | 250 tokens | `chunk_size - chunk_overlap` |
| 编码器 | `cl100k_base` | 与 GPT-4 / Claude 兼容 |
| 短文本 | 不分块 | 若 ≤ 300 tokens 则直接返回单块 |

### 6.3 BBCode 处理

- `clean_text()` **不剥离** BBCode — 摘要中的 `[b]` 等标签保留，提供结构语义信号
- `sanitizers._strip_bbcode()` 在评论/日志层处理 BBCode，将 `[mask]` → `【剧透】`、`[quote]` → `【引用】`

**分工**：
```
RAG 文本处理:  保留 BBCode（embedding 阶段利用结构语义）
Sanitizers:    转换 BBCode（呈现给 LLM 的最终文本）
```

---

## 七、HNSW 索引失败：根因分析

### 7.1 问题

```
(psycopg2.errors.ProgramLimitExceeded)
column cannot have more than 2000 dimensions for hnsw index
```

**原因**：pgvector 的 HNSW 索引实现有 2000 维硬限制。项目使用智谱 embedding-3 输出 2048 维向量。

### 7.2 影响

所有 3 张含向量列的表（`rag_entities`、`session_memories`、`public_memories`）的 HNSW 索引创建均静默失败。

**降级行为**：
- RAG 检索：余弦距离查询退化为**全表顺序扫描**（无索引加速）
- 记忆召回：`long_term.py` 中无 embedding 时回退为按 `created_at` 时间排序

**当前侥幸**：`rag_entities` 表为空（0 行），顺序扫描无性能影响。一旦摄入数据，性能将随行数线性下降。

### 7.3 解决方案

| 方案 | 改动量 | 风险 | 推荐 |
|------|--------|------|------|
| **A. 降维到 1536d** | 中：换模型 + 重建表 | 丢失语义精度 | 🟡 |
| **B. 用 IVFFlat 替代 HNSW** | 小：改 DDL | IVFFlat 无维度限制但需训练数据 | ✅ 推荐短期 |
| **C. 升级 pgvector** | 无代码改动 | 取决于 pgvector 社区是否解除限制 | 🟡 长期 |
| **D. 维度截断到 2000d** | 小：截断 embedding | 丢失 48 维信息（~2.3%） | 🟢 可接受 |

**推荐短期方案 B+D**：将向量截断到前 2000 维 + 改用 IVFFlat 索引。

```sql
-- 替代 HNSW 的方案
CREATE INDEX IF NOT EXISTS ix_rag_entities_embedding
    ON rag_entities USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
```

---

## 八、当前数据状态

| 表 | 行数 | 状态 |
|----|------|------|
| `rag_entities` (v1) | **0** | 🔴 空——`search_local_bangumi` 永远返回"无结果" |
| `bangumi_chunks` (v0) | **0** | 空——无数据摄入过 |

**对用户的影响**：`search_local_bangumi` 工具对用户完全不可用。所有"80年代机战番"这类模糊语义查询不会返回结果。

**开发者预览版对策**：在已知问题中明确标注"本地语义搜索暂不可用（数据索引建设中）"。

---

## 九、已知问题

| # | 问题 | 文件 | 严重度 |
|---|------|------|--------|
| 1 | **RAG 数据库为空** — `search_local_bangumi` 对用户不可用 | `rag_entities` 表 | 🔴 功能不可用 |
| 2 | **HNSW 索引创建失败** — 2048d > pgvector 2000d 上限 | `database/engine.py:97-101` | 🔴 性能降级 |
| 3 | **v0/v1 共存** — `BangumiChunk` + `BangumiRetriever` + `BangumiIngestor` 未清理 | `rag/retriever.py`, `rag/ingestion.py`, `database/rag_tables.py` | 🟡 技术债 |
| 4 | `_compute_cosine_distance()` 定义但未使用（PGVector 原生返回距离） | `rag/retriever.py:624` | 🟢 死代码 |
| 5 | 智谱 base_url 在 `rag/` 中硬编码为默认值，非从配置读取 | `rag/retriever.py:119`, `rag/ingestion.py:129` | 🟢 可配置性 |
