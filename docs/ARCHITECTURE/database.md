# 数据库层 (database/)

> 数据层子系统 · ~785 行源码 | 状态：🟡 无 Alembic

---

## 一、架构概览

```
┌──────────────────────────────────────────────────┐
│              database/engine.py                   │
│                                                   │
│  create_engine()  → 全局 Engine 单例               │
│  init_db()        → 扩展 + 建表 + 索引 + 迁移      │
│  get_session()    → FastAPI Depends 注入           │
└──────────────────────┬───────────────────────────┘
                       │
         ┌─────────────┼─────────────┐
         ▼             ▼             ▼
┌──────────────┐ ┌────────────┐ ┌────────────────┐
│ rag_tables   │ │ memory_    │ │ models.py      │
│              │ │ tables.py  │ │ (re-export 桩)  │
│ RagEntity    │ │ Session-   │ │                │
│ BangumiChunk │ │ Memory     │ │                │
│ SubjectMeta  │ │ UserProfile│ │                │
│ CharacterMeta│ │ Public-    │ │                │
│ PersonMeta   │ │ Memory     │ │                │
└──────────────┘ └────────────┘ └────────────────┘
```

---

## 二、表结构

### 2.1 `rag_entities` — RAG 实体（活跃，v1）

```sql
CREATE TABLE rag_entities (
    id          TEXT PRIMARY KEY,             -- "subject_10" / "character_5" / "person_3"
    entity_type TEXT NOT NULL,                -- "subject" / "character" / "person"
    name        TEXT NOT NULL,
    name_cn     TEXT,
    nsfw        BOOLEAN NOT NULL DEFAULT FALSE,
    chunk_text  TEXT NOT NULL,
    embedding   VECTOR(2048),
    meta_info   JSONB DEFAULT '{}'
);
```

索引：HNSW (embedding) ⚠️ 2048d失败 | GIN trigram (name) | GIN trigram (chunk_text) | B-Tree (nsfw) | B-Tree (entity_type, name)

### 2.2 `bangumi_chunks` — 旧 RAG 表（已弃用，v0）

```sql
CREATE TABLE bangumi_chunks (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_type TEXT NOT NULL,
    entity_id   INTEGER NOT NULL,
    chunk_text  TEXT NOT NULL,
    embedding   VECTOR(2048),
    meta_info   JSONB DEFAULT '{}'
);
```

索引：B-Tree (entity_type, entity_id)。**无 HNSW、无 GIN trigram。**

### 2.3 `session_memories` — L2 会话摘要记忆

```sql
CREATE TABLE session_memories (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id          TEXT NOT NULL,
    user_id             TEXT NOT NULL,
    summary_text        TEXT NOT NULL,            -- LLM ~200 字摘要
    embedding           VECTOR(2048),             -- 可为 NULL
    key_entities        JSONB,                    -- 对话中涉及的关键实体
    intent_distribution JSONB,                    -- 意图分布
    tools_used          JSONB,                    -- 工具名称列表
    message_count       INTEGER DEFAULT 0,
    created_at          TIMESTAMP DEFAULT now(),

    CONSTRAINT uq_session_memories_user_session UNIQUE (user_id, session_id)
);
```

索引：HNSW (embedding) ⚠️ 2048d失败 | B-Tree composite (user_id, created_at DESC) | B-Tree (session_id, user_id)

**设计决策**：
- `user_id` 无外键约束 → 软关联。异步写入时 `user_profile` 可能晚于 `session_memory` 到达，有 FK 则 INSERT 失败
- `embedding` 可为 NULL → embedding API 失败时仍能存储摘要文本，召回时回退为按 recency 排序
- `key_entities` JSONB → 灵活 schema，避免为每种实体类型建关联表

### 2.4 `user_profiles` — L2 用户画像

```sql
CREATE TABLE user_profiles (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id            TEXT NOT NULL UNIQUE,
    preferences_json   JSONB DEFAULT '{}',
    total_sessions     INTEGER DEFAULT 0,
    avg_session_length FLOAT DEFAULT 0.0,
    dominant_intent    TEXT,
    first_seen_at      TIMESTAMP DEFAULT now(),
    last_active_at     TIMESTAMP DEFAULT now(),
    updated_at         TIMESTAMP DEFAULT now()
);
```

索引：B-Tree (user_id) | B-Tree (last_active_at DESC)

`preferences_json` 结构：
```json
{
  "favorite_genres": [{"genre": "机战", "count": 12, "last_seen": "2026-06-10"}],
  "entity_affinities": {"subject_10": {"name": "高达Seed", "interest_score": 0.9}},
  "activity_profile": {"query_types": {"discovery": 15, "lookup": 8}, "total_sessions": 26}
}
```

注入条件：`total_sessions >= 3`（冷启动保护）。

### 2.5 `public_memories` — 公共记忆（Phase 6 建表，待写入）

```sql
CREATE TABLE public_memories (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    topic        TEXT NOT NULL,
    summary_text TEXT NOT NULL,              -- 200-500 字
    embedding    VECTOR(2048),
    source_type  TEXT NOT NULL,              -- 'group_discussion' | 'trending' | 'editorial'
    source_id    TEXT,
    heat_score   INTEGER DEFAULT 0,
    tags         JSONB,
    created_at   TIMESTAMP DEFAULT now(),
    expires_at   TIMESTAMP,
    is_active    BOOLEAN DEFAULT TRUE
);
```

索引：HNSW (embedding) ⚠️ 2048d失败 | B-Tree partial (is_active, created_at DESC) WHERE is_active = TRUE

---

## 三、连接池配置

**文件**：`database/engine.py:28-34`

```python
engine = create_engine(
    database_url,
    pool_size=10,       # 常驻连接数
    max_overflow=20,    # 溢出连接数（峰值 pool_size + max_overflow = 30）
    pool_pre_ping=True, # 取连接前 ping 验证
    echo=(settings.ENVIRONMENT == "development"),
)
```

| 参数 | 值 | 评估 |
|------|-----|------|
| `pool_size` | 10 | 开发环境合理。生产环境若 4 worker × 10 = 40 DB 连接 |
| `max_overflow` | 20 | 峰值 30 连接/进程。并发高时可能不足 |
| `pool_pre_ping` | True | ✅ 检测断开连接，防止 stale connection |
| `pool_recycle` | **未设置** | ⚠️ 缺失。长时间空闲连接可能被 DB/防火墙断开 |
| `connect_timeout` | **未设置** | ⚠️ 缺失。DB 不可达时默认等待可能过长 |
| `pool_timeout` | **未设置** | 默认 30s（等可用连接）。当前可接受 |

**建议生产配置**：
```python
engine = create_engine(
    database_url,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    pool_recycle=3600,      # 1 小时回收连接
    connect_timeout=10,     # 10 秒连接超时
    pool_timeout=30,        # 显式声明
)
```

---

## 四、索引策略

### 4.1 索引全景

| 表 | 索引名 | 类型 | 列 | 用途 | 状态 |
|----|--------|------|-----|------|------|
| `rag_entities` | `ix_rag_entities_embedding` | HNSW | `embedding vector_cosine_ops` | 语义检索加速 | ⚠️ 失败 |
| `rag_entities` | `ix_rag_entities_name_trgm` | GIN trigram | `name` | 模糊名称匹配 | ✅ |
| `rag_entities` | `ix_rag_entities_chunk_text_trgm` | GIN trigram | `chunk_text` | 模糊文本匹配 | ✅ |
| `rag_entities` | `ix_rag_entities_nsfw` | B-Tree | `nsfw` | 安全护栏过滤 | ✅ |
| `session_memories` | `ix_session_memories_embedding` | HNSW | `embedding vector_cosine_ops` | 记忆语义检索 | ⚠️ 失败 |
| `session_memories` | `ix_session_memories_user_created` | B-Tree composite | `(user_id, created_at DESC)` | 按用户时间查询 | ✅ |
| `user_profiles` | `ix_user_profiles_user_id` | B-Tree | `user_id` | 按用户查找画像 | ✅ |
| `user_profiles` | `ix_user_profiles_last_active` | B-Tree | `last_active_at DESC` | 活跃度排序 | ✅ |
| `public_memories` | `ix_public_memories_embedding` | HNSW | `embedding vector_cosine_ops` | 公共记忆检索 | ⚠️ 失败 |
| `public_memories` | `ix_public_memories_active` | B-Tree partial | `(is_active, created_at DESC) WHERE is_active` | 活跃记忆查询 | ✅ |

**索引创建模式**：每条索引独立 try/except，失败不阻塞其他索引或系统启动。

### 4.2 距离度量

所有向量操作使用 **cosine_distance**（余弦距离）：

```python
# RAG 检索
RagEntity.embedding.cosine_distance(query_embedding)

# 记忆召回
SessionMemory.embedding.cosine_distance(query_embedding)

# HNSW 算子
USING hnsw (embedding vector_cosine_ops)
```

余弦距离范围 [0, 2]。与智谱 embedding-3 兼容（该模型输出归一化向量，余弦距离等价于欧氏距离但更高效）。

---

## 五、迁移策略

### 5.1 当前：手动 DDL

项目**不使用 Alembic**。所有迁移通过 `init_db()` 中的内联 DDL 执行：

```python
def init_db():
    # 1. 扩展
    CREATE EXTENSION IF NOT EXISTS vector;
    CREATE EXTENSION IF NOT EXISTS pg_trgm;

    # 2. 建表
    SQLModel.metadata.create_all(engine)

    # 3. 迁移：Phase 5.4 — 添加 nsfw 列
    ALTER TABLE rag_entities ADD COLUMN IF NOT EXISTS nsfw BOOLEAN...;
    UPDATE rag_entities SET nsfw = TRUE WHERE meta_info @> '{"nsfw": true}'...;

    # 4. 索引
    CREATE INDEX IF NOT EXISTS ... (每条独立 try/except)

    # 5. 迁移：Bug 1 — session_memories 去重 + UNIQUE 约束
    DELETE FROM session_memories WHERE ... (去重)
    ALTER TABLE session_memories ADD CONSTRAINT ... UNIQUE ...;
```

### 5.2 优点

- 零依赖（不需要 Alembic 学习成本）
- 启动即迁移（无手动 `alembic upgrade head` 步骤）
- 所有 DDL 可幂等（`IF NOT EXISTS`）

### 5.3 缺点

- 无版本追踪（不知道当前 DB 处于哪个迁移状态）
- 无回滚路径（所有迁移向前不可逆）
- 表结构变更困难（`create_all` 不修改已有列）
- 迁移代码与业务代码混合在 `init_db()` 中

### 5.4 何时需要引入 Alembic

- 需要回滚迁移时
- 需要在多环境（dev/staging/prod）间管理 schema 版本时
- 需要修改已有列类型或重命名时
- **开发者预览版不需要。** 可以推迟到阶段 2（公开预览版）。

---

## 六、会话管理

```python
# FastAPI 依赖注入
def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        try:
            yield session
        finally:
            session.close()
```

但项目当前**不使用 `Depends(get_session)`**。实际使用模式：

```python
# RAG 检索器、记忆管理器、摄入器中
with Session(self.engine) as session:
    result = session.execute(stmt).fetchall()
    # 只读：不 commit
    # 写入：session.commit()
```

**正确性**：
- 只读操作（检索）不 commit → ✅ 自动回滚
- 写入操作（记忆、摄入）显式 commit → ✅
- 无长事务 → ✅ 短期会话

---

## 七、pgvector 扩展

`init_db()` 幂等启用：

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
```

- `vector` — 提供 `VECTOR(n)` 类型 + `cosine_distance` 运算符 + HNSW/IVFFlat 索引
- `pg_trgm` — 提供 GIN trigram 索引支持，用于 `name` 和 `chunk_text` 模糊匹配

---

## 八、启动行为

```
FastAPI lifespan 启动
  → init_db()
    → CREATE EXTENSION vector (幂等)
    → CREATE EXTENSION pg_trgm (幂等)
    → SQLModel.metadata.create_all(engine) (幂等建表)
    → Phase 5.4 迁移 (幂等)
    → 10 条 CREATE INDEX IF NOT EXISTS (每条独立 try/except)
    → Phase 5 Bug Fix 迁移 (幂等)
  → 日志: "系统启动成功"

若数据库不可达:
  → init_db() raise OperationalError
  → FastAPI 启动失败 (进程退出)
  → 无优雅降级——无 DB 则系统不可用
```

---

## 九、已知问题

| # | 问题 | 文件 | 严重度 |
|---|------|------|--------|
| 1 | **HNSW 索引创建失败** — 2048d > 2000d，3 张表受影响 | `engine.py:97-148` | 🔴 |
| 2 | **`pool_recycle` 未设置** — 长空闲连接可能被断开 | `engine.py:28` | 🟡 |
| 3 | **`connect_timeout` 未设置** — DB 不可达时的等待行为不确定 | `engine.py:28` | 🟡 |
| 4 | **无 Alembic** — 无迁移版本管理，无回滚 | 全局 | 🟡 |
| 5 | `UserProfile` 表存在但 L3 画像已废弃 — 表仍被 `create_all` 创建 | `memory_tables.py:121` | 🟢 |
| 6 | `print()` 语句在 lifespan 中，生产环境应用 logger | `main.py:161,163` | 🟢 |
