# RAG 策略与混合检索设计文档

> 版本 0.2.0 | 2026-06-02

---

## 目录

- [1. 核心设计理念](#1-核心设计理念)
- [2. 数据库架构：单表多态](#2-数据库架构单表多态)
- [3. 摄入管道](#3-摄入管道)
  - [3.1 语义前缀防稀释](#31-语义前缀防稀释)
  - [3.2 关联边内存重排与剪枝](#32-关联边内存重排与剪枝)
  - [3.3 摄入入口](#33-摄入入口)
- [4. 检索管道](#4-检索管道)
  - [4.1 标量前置过滤](#41-标量前置过滤)
  - [4.2 多态阶梯分桶排序](#42-多态阶梯分桶排序)
- [5. Embedding 策略](#5-embedding-策略)
- [6. 索引策略](#6-索引策略)
- [7. 数据流全景](#7-数据流全景)

---

## 1. 核心设计理念

本项目的 RAG 系统围绕以下原则构建：

| 原则 | 说明 |
|---|---|
| **正文与 Metadata 分离** | Embedding 仅基于 `chunk_text` 纯文本；结构化字段全部存入 `meta_info` JSONB，绝不拼入正文 |
| **单一事实来源** | Subject / Character / Person 三类实体共用同一张 `rag_entities` 表，避免多表 JOIN |
| **全局唯一 ID** | 前缀化主键 `"subject_10"` / `"character_5"` / `"person_3"` 防止跨类型 ID 碰撞 |
| **适度反范式化** | 关联边（casts / works）压入 `meta_info` JSONB，入库前经 Pydantic 强类型契约校验 |
| **SQL 硬过滤优先** | 向量检索前先用标量 WHERE 条件大幅缩减候选集，降低无效向量比对开销 |

---

## 2. 数据库架构：单表多态

### 2.1 表结构

```sql
-- rag_entities: 单表承载三类实体的向量化存储
CREATE TABLE rag_entities (
    id          TEXT PRIMARY KEY,        -- 前缀化 ID: "subject_10" / "character_5"
    entity_type TEXT NOT NULL,           -- "subject" / "character" / "person"
    name        TEXT NOT NULL,           -- 实体原文名称（B-Tree 索引）
    name_cn     TEXT,                    -- 实体中文名称
    chunk_text  TEXT NOT NULL,           -- 带语义前缀的分块文本
    embedding   VECTOR(2048),            -- pgvector 向量嵌入（智谱 embedding-3）
    meta_info   JSONB DEFAULT '{}'       -- 反范式化元数据（Pydantic 契约校验后写入）
);

-- B-Tree 索引：加速精确匹配与标量过滤
CREATE INDEX ix_rag_entities_type ON rag_entities (entity_type);
CREATE INDEX ix_rag_entities_name ON rag_entities (name);

-- HNSW 向量索引：加速余弦距离最近邻查询
CREATE INDEX ix_rag_entities_embedding
    ON rag_entities USING hnsw (embedding vector_cosine_ops);

-- GIN trigram 索引：加速 name / chunk_text 的模糊匹配
CREATE INDEX ix_rag_entities_name_trgm
    ON rag_entities USING gin (name gin_trgm_ops);
CREATE INDEX ix_rag_entities_chunk_text_trgm
    ON rag_entities USING gin (chunk_text gin_trgm_ops);
```

### 2.2 meta_info JSONB 契约

#### SubjectMeta

```json
{
    "score": 8.19,
    "rating_total": 9438,
    "date": "2008-04-06",
    "eps": 25,
    "nsfw": false,
    "tags": [
        {"name": "科幻", "count": 1523},
        {"name": "原创", "count": 987}
    ]
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `score` | `float` | 条目评分 |
| `rating_total` | `int` | 评分人数（热度信号，桶内降序用） |
| `date` | `str\|null` | 播出/发售日期 `YYYY-MM-DD` |
| `eps` | `int` | 总集数/话数 |
| `nsfw` | `bool` | R18 安全护栏 |
| `tags` | `list[dict]` | 原始 `[{name, count}]` 格式，不下沉为纯 str |

#### CharacterMeta

```json
{
    "role": 1,
    "collects": 4200,
    "casts": [
        {
            "subject_id": "subject_8",
            "subject_name": "コードギアス 反逆のルルーシュR2",
            "person_id": "person_100",
            "person_name": "福山潤",
            "role_type": 1
        }
    ]
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `role` | `int` | 角色类型编号 |
| `collects` | `int` | 收藏数（桶内降序用） |
| `casts` | `list[CharacterCast]` | 出演作品 Top 10（按关联作品热度降序） |

`CharacterCast` 子结构：

| 字段 | 类型 | 说明 |
|---|---|---|
| `subject_id` | `str` | 前缀化作品 ID |
| `subject_name` | `str` | 作品名称 |
| `person_id` | `str\|null` | 饰演者 ID |
| `person_name` | `str\|null` | 饰演者名称 |
| `role_type` | `int` | 1=主角 / 2=配角 / 3=客串 |

#### PersonMeta

```json
{
    "career": "seiyu",
    "type": 1,
    "collects": 8500,
    "works": [
        {
            "subject_id": "subject_8",
            "subject_name": "コードギアス 反逆のルルーシュR2",
            "character_id": "character_1000",
            "character_name": "ルルーシュ・ランペルージ",
            "role_type": 1
        }
    ]
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `career` | `str` | 职业标签（seiyu / producer / artist 等） |
| `type` | `int` | 人物类型编号 |
| `collects` | `int` | 收藏数（桶内降序用） |
| `works` | `list[PersonWork]` | 代表作 Top 10（按关联作品热度降序） |

---

## 3. 摄入管道

### 3.1 语义前缀防稀释

**问题**：直接将裸摘要文本送给 Embedding 模型，大模型无法从词汇统计中区分"这是一部番剧的介绍"还是"这是一个声优的人物简介"，导致不同类型实体的语义向量在空间中混叠。

**方案**：在 Embedding 前拼接极简自然语言定调前缀，作为"语义锚点"。前缀本身是自然语言短句（如 `[番剧] `），而非机械的 key-value 模板，不会稀释语义质心。

| 实体类型 | 前缀模板 | chunk_text 示例 |
|---|---|---|
| Subject | `[番剧] {name_cn}。{chunk}` | `[番剧] 进击的巨人。在巨人支配的世界中，少年艾伦...` |
| Character | `[角色] {name_cn}，出自《{subject_name}》。{chunk}` | `[角色] 艾伦·耶格尔，出自《进击的巨人》。憧憬外面世界的少年...` |
| Person | `[人物/声优] {name_cn}。{chunk}` | `[人物/声优] 梶裕贵。日本男性声优，代表作包括...` |

关键设计：前缀文本 **存入** `chunk_text` 列（即 Embedding 的输入与存储文本一致），保证检索时的语义对齐。

### 3.2 关联边内存重排与剪枝

**问题**：Bangumi API 返回的 `/casts` 或人物 `/works` 列表按数据库原始顺序排列，混杂了大量冷门路人角色。直接全量存储会稀释代表作质量，且浪费存储。

**方案**：在 Python 内存中完成"查找本地热度 → 降序重排 → 去重 → 截断"的清洗管道。

```
Raw casts/works from Bangumi API
        │
        ▼
_lookup_subject_rating_map()
  查询本地 RagEntity WHERE entity_type="subject"
  提取各作品的 meta_info.rating_total
        │
        ▼
sorted(key=rating_total, reverse=True)     ← 降序重排
        │
        ▼
seen_subjects 去重                           ← 同一作品 TV/总集篇只保留一条
        │
        ▼
[:10] 截断                                  ← 仅保留 Top 10 热门代表作
        │
        ▼
Pydantic 契约校验 (CharacterCast / PersonWork)
        │
        ▼
存入 meta_info JSONB
```

**热度查询的优雅降级**：若关联作品尚未入本地库（`rating_total` 不存在），默认视为 0，排在列表末尾。

### 3.3 摄入入口

| 方法 | 实体类型 | 使用的 Pydantic Meta | 特殊处理 |
|---|---|---|---|
| `RagEntityIngestor.ingest_subjects()` | Subject | `SubjectMeta` | — |
| `RagEntityIngestor.ingest_characters()` | Character | `CharacterMeta` | casts 经 `_rerank_casts` 重排 |
| `RagEntityIngestor.ingest_persons()` | Person | `PersonMeta` | works 经 `_rerank_works` 重排 |

---

## 4. 检索管道

检索器 `RagEntityRetriever.hybrid_search()` 实现"标量前置过滤 → 向量召回 → 多态分桶排序 → 阈值防爆"的四阶段管道。

### 4.1 标量前置过滤

在 SQL 层硬编码 WHERE 条件，仅在指定领域内做向量比对：

```python
# entity_type="subject" → 只在番剧中检索
stmt = select(RagEntity).where(RagEntity.entity_type == "subject")

# entity_type="all" → 跨域全量检索（无此 WHERE 子句）
stmt = select(RagEntity)
```

NSFW 安全护栏同样在 SQL 层前置（仅对 Subject 类型生效）：

```python
if exclude_nsfw and entity_type in ("subject", "all"):
    stmt = stmt.where(~RagEntity.meta_info.contains({"nsfw": True}))
```

### 4.2 多态阶梯分桶排序

保留向量距离分桶逻辑，在极近的距离桶内进行次级热度降序时，根据 `entity_type` 动态路由到不同的热度信号：

```
语义梯队分桶（不变）
  梯队 ID = int(cosine_distance / bucket_size)
  第一主键: 梯队 ID 升序
       │
       ▼
同梯队内次级热度动态路由:
  ┌──────────┬────────────────────────────────┐
  │ Subject   │ meta_info.rating_total DESC    │  ← 评分人数
  │ Character │ meta_info.collects DESC        │  ← 角色收藏数
  │ Person    │ meta_info.collects DESC        │  ← 人物收藏数
  └──────────┴────────────────────────────────┘
```

**设计意图**：不同实体类型的"热度"内涵不同——番剧看评分人数，角色/人物看收藏数。动态路由确保每种实体的排序语义正确。热度信号仅在同梯队内起消歧作用，不破坏全局语义匹配度。

**阈值防爆**：`cosine_distance > 0.65` 的候选直接丢弃，防止语义无关结果污染输出。

---

## 5. Embedding 策略

| 维度 | 说明 |
|---|---|
| **模型** | 智谱 embedding-3 |
| **维度** | 2048 |
| **输入** | `chunk_text`（已拼接语义前缀的完整文本） |
| **正文与 Metadata 分离** | tags / score 等结构化字段绝不被拼入 embedding 输入，避免语义稀释 |
| **批量处理** | 一次性发送整批文本到 API，减少网络往返 |
| **编码** | tiktoken `cl100k_base` 做 Token 级分块，`chunk_size=300, chunk_overlap=50` |

---

## 6. 索引策略

| 索引 | 类型 | 用途 |
|---|---|---|
| `ix_rag_entities_type` | B-Tree | 加速 `entity_type = 'subject'` 的标量前置过滤 |
| `ix_rag_entities_name` | B-Tree | 加速按名称精确查找 |
| `ix_rag_entities_embedding` | HNSW + cosine_ops | 加速向量最近邻检索（核心检索索引） |
| `ix_rag_entities_name_trgm` | GIN + pg_trgm | 加速 `name LIKE '%关键词%'` 模糊匹配 |
| `ix_rag_entities_chunk_text_trgm` | GIN + pg_trgm | 加速 chunk_text 全文模糊搜索 |

索引通过 `init_db()` 在应用启动时幂等创建（`CREATE INDEX IF NOT EXISTS`），不阻塞启动流程。

---

## 7. 数据流全景

```
┌─ 摄入阶段 ──────────────────────────────────────────────────┐
│                                                              │
│  Bangumi API (v0 + p1)                                       │
│       │                                                      │
│       ▼                                                      │
│  BangumiTextProcessor                                        │
│    · clean_text()  清洗 HTML/全角空格/零宽字符                │
│    · split_text()  Token 滑动窗口分块 (300/50)                │
│    · create_entity_documents()  按实体类型组装父文档          │
│       │                                                      │
│       ▼                                                      │
│  RagEntityIngestor                                           │
│    · 拼接语义前缀 → chunk_text                                │
│    · _rerank_casts / _rerank_works  关联边内存重排            │
│    · Pydantic Meta 契约校验                                   │
│    · 批量 Embedding → 写入 rag_entities                       │
│                                                              │
└──────────────────────────────────────────────────────────────┘

┌─ 检索阶段 ──────────────────────────────────────────────────┐
│                                                              │
│  用户自然语言查询                                             │
│       │                                                      │
│       ▼                                                      │
│  RagEntityRetriever.hybrid_search()                          │
│    Step 1: 查询向量化 (智谱 embedding-3)                      │
│    Step 2: SQL 标量前置过滤 + 向量召回 (limit × 2)            │
│    Step 3: 候选集组装 (RagSearchResult)                      │
│    Step 4: 距离阈值防爆 (>0.65 丢弃)                          │
│    Step 5: 多态阶梯分桶排序                                   │
│       │                                                      │
│       ▼                                                      │
│  search_local_bangumi (Agent Tool)                           │
│    · 多态格式化 (📺/🧑/🎤)                                    │
│    · 按 entity_type 渲染不同信息维度                          │
│       │                                                      │
│       ▼                                                      │
│  返回给 LLM / 用户                                            │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

### 关键文件索引

| 文件 | 职责 |
|---|---|
| `database/models.py` | `RagEntity` 表定义 + Pydantic Meta 契约 |
| `database/engine.py` | 连接池、扩展启用、索引 DDL |
| `rag/text_processor.py` | 文本清洗、Token 分块、父子文档创建 |
| `rag/ingestion.py` | 语义前缀拼接、关联边重排、批量写入 |
| `rag/retriever.py` | 多态检索、分桶排序、阈值防爆 |
| `tools/bgm_tools.py` | Agent Tool 封装、多态结果格式化 |
