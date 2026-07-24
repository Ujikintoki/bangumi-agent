# RAG 系统全面审查

## 背景

这是一个 Bangumi (bgm.tv) AI Agent 项目，技术栈 FastAPI + LangGraph + PostgreSQL/pgvector。

### 已完成的架构改造

项目进行了两轮宏观审查 + 数据层重构：

**Agent 层** (`docs/design/architecture-review-2026-07-22.md`)：发现 LLM 调用链倒置、Critic 硬编码阈值、记忆系统只基于用户输入等问题。

**基建层 + 数据层**：将工具返回从自然语言 `str` 改为高信号密度结构化 `dict`，建立了 [A/B/C/D 字段决策方法论](docs/design/bangumi-api-schema-methodology.md)。核心变化：sanitizer 现在保留更多字段（staff、infobox、summary 300 chars、tags 全量 30 条、rating_count 全量 10 档），工具直接 return dict 而不是 `_format_*()` 转自然语言。

**工具层迁移进度**：16 个工具中 13 个已返回 `dict`。仅剩 `get_blog`、`get_user_timeline`、`search_local_bangumi` 仍返回 `str`。

### RAG 系统的当前位置

RAG 系统**从未被 review 过**，且当前数据库是空的（`rag_entities` 表无数据）。但 sanitizer 的改动会影响 RAG——sanitizer 现在保留的字段（staff、summary、relations 等）需要同步到 `database/rag_tables.py` 的 Meta 模型中。

### 关键依赖关系

```
sanitizer 保留更多字段 (✅ 已改)
  → rag_tables.py Meta 模型需同步（可能缺字段）
    → rag/ingestion.py 摄入管道（字段映射、语义前缀、embedding）
      → rag/retriever.py 检索（hybrid_search、MMR、heat signal）
        → rag/text_processor.py 文本分块策略
```

如果 Meta 模型缺字段，摄入时会静默丢弃数据，embedding 质量受损。
如果摄入管道有问题，检索即使对也没数据可查。
如果检索逻辑有硬编码信号分析，和 `_compute_subject_signals` 同款问题。

---

## 要求

以 `docs/design/architecture-review-2026-07-22.md` 的诊断方法（穷举模块、画依赖图、找"弱组件决定强组件"的模式、发现并发问题），对 RAG 系统做**全面审查**。

---

## 审查范围

### 必查文件

| 文件 | 行数 | 角色 |
|------|------|------|
| `database/rag_tables.py` | ~324 | `RagEntity` ORM + `SubjectMeta`/`CharacterMeta`/`PersonMeta` + `SearchResult` |
| `rag/ingestion.py` | ~299+ | 摄入管道：API→sanitizer→embedding→INSERT |
| `rag/retriever.py` | ~653 | 检索器：hybrid search + MMR + heat signal + bucket sort |
| `rag/text_processor.py` | ? | 文本分块策略（决定 embedding 质量） |
| `database/memory_tables.py` | ? | memory 表是否和 RAG 表共享 embedding 维度 |

### 关联文件（需对照）

| 文件 | 原因 |
|------|------|
| `clients/sanitizers.py` | sanitizer 新保留的字段 → Meta 模型是否匹配 |
| `clients/client.py` | 摄入时调用的 API 方法 → sanitizer 链路是否一致 |
| `tools/bgm_tools.py` | `search_local_bangumi` 仍返回 `str`，用到了 `_compute_subject_signals`、`_ROLE_MAP`、`_TYPE_ICONS` |
| `core/config.py` | `EMBEDDING_DIMENSION`、`EMBEDDING_MODEL`、`ZHIPU_API_KEY` |
| `database/engine.py` | DDL 中 HNSW 索引创建（已知 2048d > pgvector 2000d 限制） |

---

## 已知问题清单（需逐一确认是否仍存在）

1. **`SubjectMeta` 缺少字段**：sanitizer 现在保留 staff、summary(300 chars)、info、infobox、relations、rating_count(全量)。Meta 模型是否同步？
2. **HNSW 索引 2048d 超限**：pgvector HNSW 上限 2000d，智谱 embedding-3 是 2048d。CLAUDE.md 已记录。当前索引创建会静默失败吗？
3. **RAG v0/v1 共存**：`BangumiChunk` 系列（旧）和 `RagEntity` 系列（新）并存。可以安全清理吗？
4. **`_compute_subject_signals` 仍在 RAG 中使用**：`search_local_bangumi` 内部调用它做评分分析。这套硬编码 Python 逻辑应该移到 LLM 侧（和工具层改造同理）。
5. **`rag/ingestion.py` 的 `ingest_subjects()` 参数签名**：可能缺少 sanitizer 新保留的字段（staff、relations 等）。
6. **`database/rag_tables.py` 的 `SearchResult`**：有 `core_staff`、`main_cv` 字段，但 Meta 模型不存——这些字段去哪了？
7. **`rag/retriever.py` 的 `_extract_heat_signal()`**：用硬编码阈值做热度分析。这是不是 `_compute_subject_signals` 的翻版？
8. **模块级 `_EMBEDDING_DIM` 变量**：`rag_tables.py` 在 import 时取 `get_settings().EMBEDDING_DIMENSION`——模块加载顺序问题？

---

## 方法论

### 第一步：画出 RAG 数据流

从 Bangumi API → sanitizer → Meta 模型 → embedding → pgvector → hybrid_search → search_local_bangumi 工具 → LLM 的完整链路。标注每个环节的输入/输出数据结构。找出哪些环节在做"不应该它做"的事。

### 第二步：逐模块诊断

对每个模块，找出：
- **结构问题**：弱组件决定强组件（比如 sanitizer 改了字段但 Meta 模型是瓶颈）
- **并发问题**：asyncio.create_task、连接池耗尽、fire-and-forget 无超时
- **失效假设**：写代码时假设成立但现在已经不成立的（比如"RAG 数据来自 v0 API"）
- **硬编码**：阈值、维度、字段名

### 第三步：对照 sanitizer 新字段

sanitizer 改动后保留了哪些字段 → `SubjectMeta`/`CharacterMeta`/`PersonMeta` 各缺哪些 → `ingestion.py` 的 ingest 函数是否接了这些字段。

### 第四步：RAG 启用条件评估

数据库目前是空的。从零到"能跑起来"需要做什么？需要哪些配置（ZHIPU_API_KEY、EMBEDDING_MODEL 等）？

### 第五步：`search_local_bangumi` dict schema 设计

基于检索真实返回数据（需要先摄入测试数据），按 A/B/C/D 方法论设计 dict schema。删除 `_compute_subject_signals` 依赖。

---

## 输出要求

1. **诊断报告**：按严重程度排（🔴必须修 / 🟡应该修 / ℹ️建议），每项含"为什么是问题"+"建议怎么修"
2. **依赖图**：RAG 各模块的数据流向
3. **Meta 模型对照表**：sanitizer 保留字段 vs `SubjectMeta`/`CharacterMeta`/`PersonMeta` 现有字段的 diff
4. **启用路径**：从空数据库到可用的最小步骤（可以包含在讨论中而不必立即实施）
5. **与之前 review 的关联**：哪些是同一个问题在不同层的表现

---

## 项目文件索引

| 文件 | 用途 |
|------|------|
| `CLAUDE.md` | 项目架构全览、已知问题、技术债 |
| `docs/design/architecture-review-2026-07-22.md` | Agent 层宏观评审 |
| `docs/design/data-layer-redesign-discussion.md` | 数据层从白名单到信号/噪音分析的演进 |
| `docs/design/bangumi-api-schema-methodology.md` | A/B/C/D 字段决策方法论 |
| `.env.example` | 环境变量模板 |

先讨论再动代码——不要直接写诊断文档，先和我对话确认发现。
