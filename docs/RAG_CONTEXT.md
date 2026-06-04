# RAG 模块上下文文档

> 为 RAG 模块优化 session 提供的项目全景快照 | 2026-06-04

---

## 1. 项目定位

Stateful AI Agent for Bangumi（番组计划）——自然语言理解 + 多工具编排 + 长期记忆的动漫/漫画/音乐/游戏发现助手。

## 2. 三层信息架构

```
用户查询
    │
    ├─ LLM 内置知识（不触发 Tool）
    │   "顶上战争是哪两方？" / "什么是三集定律？"
    │
    ├─ Bangumi API Tools（动态/实时数据）  ← Phase 2 已完成
    │   搜索、详情、日历、趋势、评论、讨论、角色/声优、用户画像、日志、时光机
    │
    └─ RAG 语义检索（静态内容发现）       ← 当前 session 在优化
        "类似命运石之门的烧脑番"           ← API 关键词搜不到
        "配过最多主角的声优"              ← 跨实体关联
        API 搜索未命中时的回退
```

**RAG 的定位**：API 搜索（MeiliSearch 关键词匹配）的语义补充层，不是替代品。两者不重叠。

---

## 3. 当前架构全景

```
main.py                     # FastAPI + /health（/chat 端点待 Phase 3）
    │
    ├─ core/config.py        # pydantic-settings, 全局单例
    │   • DATABASE_URL, BANGUMI_ACCESS_TOKEN, ZHIPU_API_KEY
    │   • EMBEDDING_MODEL="embedding-3", EMBEDDING_DIMENSION=2048
    │
    ├─ agent/                # LangGraph ReAct Agent（⏳ 占位，Phase 3 激活）
    │   • state.py  — AgentState TypedDict
    │   • nodes.py — reasoning / tool / critic（硬编码占位）
    │   • graph.py — 图谱拓扑：reasoning → tool/critic → END/retry
    │
    ├─ tools/bgm_tools.py    # 12 个 @tool 函数 ✅
    │   • 搜索/详情/日历/趋势/单集讨论/条目讨论/角色人物评论/角色声优/
    │     用户画像/日志/时光机/RAG本地搜索
    │   • 无条件注册 9 个 + Token 门控 3 个
    │
    ├─ clients/              # HTTP 通信层 ✅
    │   • base.py      — BaseClient（httpx, 重试, auth, async ctx mgr）
    │   • client.py    — BangumiClient（10 个业务方法，全部走 p1 API）
    │   • sanitizers.py — 纯函数清洗器（白名单 + 截断 + 噪音过滤 + BBCode剥离）
    │
    ├─ schemas/tools_input.py # 12 个 Pydantic v2 Tool Schema ✅
    │
    ├─ database/             # PostgreSQL + pgvector
    │   • engine.py  — 连接池 + HNSW/GIN 索引 DDL
    │   • models.py — RagEntity（单表多态）+ Pydantic Meta 契约
    │
    └─ rag/                  # RAG 模块 ← 当前优化目标
        • text_processor.py  — tiktoken 滑动窗口分块
        • ingestion.py       — 语义前缀 + 关联边重排 + 批量 embedding
        • retriever.py       — hybrid_search: 标量过滤→向量召回→分桶排序
```

## 4. RAG 表结构（核心契约）

### `rag_entities` 表（单表多态，Subject/Character/Person 共用）

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | `TEXT PK` | 前缀化: `"subject_10"` / `"character_5"` / `"person_3"` |
| `entity_type` | `TEXT` | `"subject"` / `"character"` / `"person"` |
| `name` | `TEXT` | 原文名称（B-Tree 索引） |
| `name_cn` | `TEXT` | 中文名称，可为空 |
| `chunk_text` | `TEXT` | 带语义前缀的分块文本，参与 embedding |
| `embedding` | `VECTOR(2048)` | 智谱 embedding-3 向量（HNSW + cosine_ops 索引） |
| `meta_info` | `JSONB` | 反范式化元数据，入库前经 Pydantic 契约校验 |

### meta_info JSONB 契约

**SubjectMeta:**
```python
score: float          # 评分
rank: int             # 全站排名
rating_total: int     # 评分人数（热度信号，桶内降序用）
date: Optional[str]   # YYYY-MM-DD
year: Optional[int]   # 播出年份（从 airtime.year 提取）
platform: str         # TV / Movie / OVA / Web 等
eps: int              # 集数
nsfw: bool            # R18 护栏
tags: list[dict]      # [{name: str, count: int}]
```

**CharacterMeta:**
```python
role: int             # CharacterType: 1=角色, 2=机体, 3=舰船, 4=组织机构
collects: int         # 收藏数（桶内降序用）
casts: list[CharacterCast]
# CharacterCast: {subject_id, subject_name, person_id, person_name, role_type}
```

**PersonMeta:**
```python
career: list[str]     # 职业标签列表，如 ["seiyu", "actor"]
type: int             # PersonType: 1=个人, 2=公司, 3=组合
collects: int         # 收藏数（桶内降序用）
works: list[PersonWork]
# PersonWork: {subject_id, subject_name, positions: [{type_cn, summary, appear_eps}]}
```

### 索引

```sql
-- HNSW 向量余弦距离索引
CREATE INDEX ix_rag_entities_embedding ON rag_entities USING hnsw (embedding vector_cosine_ops);

-- B-Tree 精确匹配
CREATE INDEX ix_rag_entities_type ON rag_entities (entity_type);
CREATE INDEX ix_rag_entities_name ON rag_entities (name);

-- GIN trigram 模糊匹配
CREATE INDEX ix_rag_entities_name_trgm ON rag_entities USING gin (name gin_trgm_ops);
CREATE INDEX ix_rag_entities_chunk_text_trgm ON rag_entities USING gin (chunk_text gin_trgm_ops);
```

---

## 5. RAG 与外部模块的接口

### 5.1 Tool 层调用 RAG（已实现）

```python
# tools/bgm_tools.py — search_local_bangumi
@tool(args_schema=LocalSearchInput)
def search_local_bangumi(query, entity_type="all", limit=5, nsfw=False):
    from core.config import get_settings
    from database.engine import engine
    from rag.retriever import RagEntityRetriever

    settings = get_settings()
    retriever = RagEntityRetriever(engine=engine, zhipu_api_key=settings.ZHIPU_API_KEY)
    results = retriever.hybrid_search(query=query, entity_type=entity_type, limit=limit, exclude_nsfw=not nsfw)
    # → 多态格式化输出给 LLM
```

### 5.2 摄入管道（离线）

```python
# rag/ingestion.py — RagEntityIngestor
ingestor = RagEntityIngestor(engine=engine, zhipu_api_key=..., embedding_model=...)

# 三个入口
ingestor.ingest_subjects(subjects_data)    # SubjectMeta 契约
ingestor.ingest_characters(characters_data) # CharacterMeta 契约 + casts 重排
ingestor.ingest_persons(persons_data)       # PersonMeta 契约 + works 重排
```

### 5.3 检索管道

```python
# rag/retriever.py — RagEntityRetriever
retriever = RagEntityRetriever(engine=engine, zhipu_api_key=...)

results = retriever.hybrid_search(
    query="80年代评分最高的机战番",
    entity_type="subject",  # "subject" / "character" / "person" / "all"
    limit=5,
    exclude_nsfw=True,
)
# → list[RagSearchResult] (含 cosine_distance, chunk_text, meta_info)
```

---

## 6. 关键设计决策（RAG 相关）

| 决策 | 说明 |
|---|---|
| **正文与 Metadata 分离** | Embedding 仅基于 `chunk_text` 纯文本；tags/score 等结构化字段走 JSONB WHERE 硬过滤，绝不拼入正文 |
| **语义前缀防稀释** | Subject: `[作品名] {name_cn}。{chunk}` / Character: `[角色] {name_cn}，出自《{subject}》。{chunk}` / Person: `[人物] {name_cn}。{chunk}` — 前缀存入 chunk_text 参与 embedding |
| **多态阶梯分桶排序** | 向量距离分桶后，桶内次级热度动态路由：Subject→rating_total DESC, Character→collects DESC, Person→collects DESC |
| **距离阈值防爆** | cosine_distance > 0.65 直接丢弃 |
| **关联边内存重排** | casts/works 在 Python 内存中按本地 RagEntity 热度降序重排，截断 Top 10 |
| **BBCode 剥离** | 评论层的 BBCode 由 `clients/sanitizers._strip_bbcode()` 处理，RAG 文本层不做剥离（标签可能提供语义信号） |

---

## 7. 需要注意的变更（本 session 已做）

RAG 模块引用的上游接口最近有以下变更：

| 变更 | 影响 RAG 的文件 |
|---|---|
| `PersonMeta.career: str → list[str]` | `database/models.py` — 存储格式变化 |
| `PersonWork` 新增 `positions: list[dict]`，删除 `character_id/character_name/role_type` | `database/models.py` + `rag/ingestion.py` — `_rerank_works` 逻辑已更新 |
| `SubjectMeta` 新增 `rank: int`, `year: int`, `platform: str` | `database/models.py` + `rag/ingestion.py` |
| `clients.sanitizers._strip_bbcode()` 新函数 | RAG 文本处理器**不需要**调用它（见决策表） |
| `rag/text_processor.py` BBCode TODO 已更新 | 指向 sanitizers，RAG 层保留原始 BBCode |

## 8. 当前已知问题（RAG 相关）

| 问题 | 状态 |
|---|---|
| `BangumiChunk` + `BangumiIngestor` + `BangumiRetriever` 旧代码共存 | [DEPRECATED] 标记，Phase 4 清理 |
| `rag/retriever.py` 中有 `BangumiChunk` 回退逻辑 | 待 Phase 4 |
| `test/conftest.py` + `test/test_rag.py` + `test/verify_rag_e2e.py` 引用旧表 | 待 Phase 4 |
| `rag/Rag_schemas/bangumi.py` 中 v0 模型 (SlimSubjectResponse) 仅 test 使用 | 待 Phase 4 |

---

## 9. 相关文件快速索引

```
database/models.py          # RagEntity + 5 个 Pydantic Meta 契约
database/engine.py          # init_db(), HNSW/GIN DDL
rag/text_processor.py       # BangumiTextProcessor: clean + split
rag/ingestion.py            # RagEntityIngestor: 摄入三入口
rag/retriever.py            # RagEntityRetriever: hybrid_search
rag/Rag_schemas/bangumi.py  # v0 + p1 API 响应模型（部分已过时）
tools/bgm_tools.py           # search_local_bangumi（RAG Tool 封装）
test/test_rag.py             # 旧 RAG 测试
test/verify_rag_e2e.py       # E2E 验证脚本
docs/RAG_STRATEGY.md         # 完整 RAG 策略文档
docs/RAG_SQL_info.md         # 表结构与摄入流程详解
```
