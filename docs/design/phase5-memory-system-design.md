# Phase 5: 三层记忆系统 — 完整设计方案

> 状态: 设计阶段 | 最后更新: 2026-06-10 | 作者: Lithium + Claude

---

## 目录

1. [执行摘要](#1-执行摘要)
2. [架构总览](#2-架构总览)
3. [L1 短记忆：临时窗口](#3-l1-短记忆临时窗口)
4. [L2 长记忆：用户持久化记忆](#4-l2-长记忆用户持久化记忆)
5. [L3 公共记忆：社区共识蒸馏](#5-l3-公共记忆社区共识蒸馏)
6. [数据模型设计](#6-数据模型设计)
7. [记忆生命周期](#7-记忆生命周期)
8. [召回算法设计](#8-召回算法设计)
9. [Agent 集成方案](#9-agent-集成方案)
10. [配置与 Token 预算](#10-配置与-token-预算)
11. [边界情况与故障模式](#11-边界情况与故障模式)
12. [性能预算](#12-性能预算)
13. [分步实施计划](#13-分步实施计划)
14. [测试策略](#14-测试策略)
15. [未来扩展](#15-未来扩展)

---

## 1. 执行摘要

### 1.1 问题

当前系统虽有 `session_id` 和 `user_id` 字段，但它们是纯粹的"预留字段"——每次 `/chat` 请求都是**金鱼记忆**。用户连续两次询问"上次推荐的第三个作品叫什么"，系统无法回答。同一个用户跨 session 的偏好、评分倾向、历史关注主题全部丢失。

### 1.2 目标

构建一个专业的三层记忆系统，让 Agent 从"无状态函数"演进为"有记忆的助手"：

| 层级 | 定位 | 生命周期 | 存储引擎 |
|------|------|---------|---------|
| **L1** | 当前对话的注意力窗口 | 单 session | 内存（已有 `manage_memory()`） |
| **L2** | 用户跨 session 的记忆与偏好 | 永久，按 `user_id` | PostgreSQL + pgvector |
| **L3** | 全局社区共识快照 | 长期，全局共享 | PostgreSQL + pgvector（Phase 6 写入，Phase 5 建表） |

### 1.3 核心设计原则

1. **非侵入**：L1 保持不变，L2/L3 作为新增模块叠加，现有 438 tests 一个不改
2. **异步写，同步读**：记忆写入用 fire-and-forget 不阻塞用户响应；记忆召回在推理前同步完成（< 200ms）
3. **优雅降级**：记忆系统的任何故障（DB 断连、embedding API 超时）不阻塞主流程，静默回退为无记忆模式
4. **Token 预算感知**：注入的记忆文本有硬上限（500 tokens），永不挤占工具调用和对话上下文
5. **双 Agent 统一**：L2 记忆层同时服务于 Research Agent 和 Dialogue Agent，共用 `MemoryManager`

---

## 2. 架构总览

### 2.1 三层数据流

```
                     ┌──────────────────────────────────────┐
                     │           POST /chat 请求              │
                     │  {message, user_id, session_id,       │
                     │   agent_type}                         │
                     └──────────────┬───────────────────────┘
                                    │
                    ┌───────────────▼──────────────────────┐
                    │  MemoryManager.recall(user_id, msg)  │  ← 同步，< 200ms
                    │  ├─ 语义检索 session_memories         │
                    │  └─ 读取 user_profiles                │
                    └───────────────┬──────────────────────┘
                                    │
                    ┌───────────────▼──────────────────────┐
                    │  构建 System Prompt                   │
                    │  ├─ BASE + intent 变体（不变）        │
                    │  └─ ## 用户历史 区块（新增）          │
                    │     注入召回的记忆摘要                │
                    └───────────────┬──────────────────────┘
                                    │
                    ┌───────────────▼──────────────────────┐
                    │  Agent Graph 执行                     │
                    │  (research_reasoning → tool → critic → END) │
                    │  L1 manage_memory() 滑动窗口          │
                    └───────────────┬──────────────────────┘
                                    │
                    ┌───────────────▼──────────────────────┐
                    │  提取 final_reply                     │
                    └───────────────┬──────────────────────┘
                                    │
                    ┌───────────────▼──────────────────────┐
                    │  MemoryManager.remember(...)          │  ← 异步，fire-and-forget
                    │  ├─ LLM 摘要 → session_memories       │
                    │  └─ 增量更新 user_profiles            │
                    └───────────────┬──────────────────────┘
                                    │
                                    ▼
                           返回 ChatResponse
```

### 2.2 模块结构

```
agent/
├── memory.py              # L1 滑动窗口（不变）
├── memory_manager.py      # [NEW] L2/L3 记忆管理器
│   ├── MemoryManager      # 统一入口
│   ├── remember_session() # 写入 session 摘要
│   ├── recall_sessions()  # 语义召回历史 session
│   ├── update_profile()   # 增量更新用户画像
│   └── remember_public()  # 写入公共记忆（Phase 6）
│
database/
├── models.py              # 现有 RagEntity（不变）
├── memory_tables.py       # [NEW] L2/L3 表定义
│   ├── SessionMemory      # ORM: session_memories
│   ├── UserProfile        # ORM: user_profiles
│   └── PublicMemory       # ORM: public_memories
├── engine.py              # 增补索引 DDL（init_db 加 2 条 CREATE INDEX）
│
tests/
└── test_memory_manager.py # [NEW] 记忆系统专用测试
```

### 2.3 与现有系统的关系

```
L1 (memory.py)         L2 (memory_manager.py)        L3 (memory_manager.py)
manage_memory()   ←──→  remember_session()     ←──→  remember_public()
滑动窗口 + 截断         recall_sessions()             recall_public()
                        update_profile()
     │                       │                            │
     ▼                       ▼                            ▼
 内存中列表           PostgreSQL session_memories    PostgreSQL public_memories
 (不持久化)           + user_profiles                (Phase 6 写入)
```

---

## 3. L1 短记忆：临时窗口

### 3.1 现状

`agent/memory.py` 的 `manage_memory()` 提供两层截断：

- **第一层**：单条消息 > 2000 tokens → 内容截断
- **第二层**：总列表 > 8000 tokens → 滑动窗口丢弃旧消息，SystemMessage 始终保留

在 `research_reasoning_node` 和 `dialogue_reasoning_node` 开头调用。

### 3.2 Phase 5 中的变化

**L1 本身不做结构性改动**。唯一的调整：

1. **入口增强**：`manage_memory()` 现在接收的不只是 `state["messages"]`，而是 System Prompt 构建后的完整消息列表（含新 SystemMessage + 注入的用户历史），因此截断在 prompt 构建之后、LLM 调用之前执行。当前 `research_reasoning_node` 的做法已经满足这一点——`manage_memory` 先于 prompt 构建，但新的 SystemMessage 替换旧的，不影响 token 计数。

2. **Token 预算重新分配**：总预算 8000 tokens 中，显式划出 500 tokens 给 L2 记忆注入：

   ```
   总预算: 8000 tokens
   ├─ System Prompt (BASE + intent):  ~1200 tokens
   ├─ L2 记忆注入:                    ≤500 tokens   ← 新增
   ├─ 对话历史:                       ~5300 tokens
   └─ LLM 输出缓冲:                   ~1000 tokens
   ```

3. **Tiktoken 容错**：`count_tokens()` 已含 try/except，无需额外处理。但增加 `tiktoken` 版本不兼容的告警日志。

### 3.3 不复用 L1 做 L2 的原因

L1 的滑动窗口是**时间维度**截断（最近 N tokens），L2 需要的是**语义维度**召回（最相关 M 条历史摘要）。两种截断逻辑完全正交，不应混合。

---

## 4. L2 长记忆：用户持久化记忆

L2 是整个 Phase 5 的核心。它解决"同一个用户，不同 session，记忆连续"的问题。

### 4.1 子模块一：Session 摘要记忆

**写入时机**：一个 session 完成后（Agent 输出 final_reply 之后）

**写入流程**：

```
session 消息历史 (truncated to last 3000 tokens)
        │
        ▼
┌─────────────────────────────┐
│  LLM 摘要 (temperature=0)   │  轻量调用，目标 < 1s
│  max_tokens=300             │
│  prompt: SUMMARIZE_PROMPT   │
└─────────────┬───────────────┘
              │
              ▼
    结构化摘要 (~200 字中文)
    + key_entities (提取的实体名)
    + intent_distribution
        │
        ├──▶ embedding (Zhipu embedding-3, 2048d)
        │
        └──▶ INSERT INTO session_memories
             (session_id, user_id, summary_text, embedding,
              key_entities, intent_distribution, ...)
```

**摘要 Prompt 设计**：

```
你是 Bangumi 助手的记忆编码器。请将以下对话历史压缩为一段不超过200字的摘要。

摘要应包含：
1. 用户的核心问题或需求
2. 你给出的关键回答、推荐的作品
3. 用户表现出的偏好信号（喜欢/不喜欢什么、评分倾向等）
4. 涉及的关键实体（作品名、角色名、声优名等）

对话历史：
{conversation_history}

请直接输出摘要文本，不要包含"摘要："等前缀。使用简体中文。
```

**为什么用 LLM 摘要而非存原始消息？**
- Token 效率：原始消息可能 3000+ tokens，摘要仅 200 字（~300 tokens）
- 语义质量：摘要提取了用户意图和偏好信号，比原始文本更适合语义检索
- 隐私友好：摘要不保留用户的具体措辞，仅保留语义要点

**写入可靠性**：
- 异步 fire-and-forget，3 秒超时
- 写入失败 → 仅记录 WARNING 日志，不阻塞用户响应
- embedding API 失败 → 存储摘要但 embedding=NULL，回退为仅按 recency 召回

### 4.2 子模块二：用户画像

用户画像不同于 session 摘要——它是**跨 session 聚合的稳定特征**，而非单次对话快照。

**画像字段设计**：

```python
{
    "favorite_genres": [         # 偏好类型（频率统计）
        {"genre": "机战", "count": 12, "last_seen": "2026-06-10"},
        {"genre": "科幻", "count": 8, "last_seen": "2026-06-08"}
    ],
    "era_preference": {          # 年代偏好
        "1980s": 5, "1990s": 3, "2000s": 7, "2010s": 4
    },
    "rating_tendency": {         # 评分倾向
        "avg_rating_given": 7.8, # 用户打分的平均值（如有）
        "prefers_high_rated": True  # 是否偏好高分作品
    },
    "entity_affinities": {       # 实体亲和度
        "subject_10": {"name": "高达Seed", "interest_score": 0.9},
        "person_5": {"name": "富野由悠季", "interest_score": 0.7}
    },
    "activity_profile": {        # 行为特征
        "query_types": {"discovery": 15, "lookup": 8, "chitchat": 3},
        "avg_session_length": 4.2,  # 平均每 session 轮次
        "total_sessions": 26
    },
    "language_style": "casual"   # 对话风格偏好
}
```

**更新策略**：增量更新，不重建

每次 `remember_session()` 完成后，同步调用 `update_profile()`：

```
session 摘要 + 本轮 intent_distribution + 本轮涉及实体
        │
        ▼
1. 读取现有 user_profile（如无则创建空画像）
2. 增量更新各字段：
   - favorite_genres: 合并频率计数，保留 top-10
   - entity_affinities: 增加 interest_score，衰减旧实体
   - activity_profile: 更新计数器和平均值
3. UPSERT INTO user_profiles
```

**更新算法细节**：

- **类型频率**：`favorite_genres[genre].count += 1`，按 count 降序截断 top-10
- **实体亲和度**：涉及到的实体 `interest_score *= 0.9 + 0.1`（衰减旧分数 + 增加新分数），min 0.1, max 1.0
- **平均 session 长度**：指数移动平均 `new_avg = 0.8 * old_avg + 0.2 * current_length`
- **年代偏好**：从 session 摘要中提取年份关键词（"80年代"、"2000年"等），对应计数 +1

### 4.3 子模块三：记忆召回

**触发时机**：每次 `/chat` 请求开始时，在 `research_reasoning_node` / `dialogue_reasoning_node` 首轮推理前

**召回流程**：

```
user_message (当前用户输入)
        │
        ├──▶ 向量化 (Zhipu embedding-3)
        │
        ├──▶ 语义检索 session_memories
        │    WHERE user_id = $1
        │    ORDER BY embedding <=> query_embedding
        │    LIMIT 5
        │
        ├──▶ 距离阈值过滤 (cosine_distance <= 0.5)
        │    丢弃不相关的结果
        │
        ├──▶ 读取 user_profile
        │    WHERE user_id = $1
        │
        └──▶ 格式化注入文本 (≤ 500 tokens)
             → 注入 System Prompt
```

**召回排序算法**（详细见第 8 节）：

```
final_score = 0.6 * semantic_similarity + 0.3 * recency_boost + 0.1 * entity_overlap
```

**注入格式**：

```
## 用户历史

你之前和该用户有过以下相关对话：

- [2天前] 用户询问了"类似星际牛仔的番剧"，你推荐了《混沌武士》《黑之契约者》《ACCA13区监察课》。用户对《混沌武士》表现出兴趣。
- [5天前] 用户搜索了"80年代机器人动画"，你推荐了《机动战士高达》《超时空要塞》《装甲骑兵》。用户偏好硬科幻机战。

**用户偏好摘要**：喜欢科幻/机战类型，偏好80-90年代作品，倾向于高分条目（≥7.5）。

请结合以上历史信息回答当前问题。如果历史和当前问题无关，可以忽略。
```

### 4.4 用户画像召回的特殊处理

用户画像的注入比 session 摘要更简洁——它直接注入偏好摘要，不需要嵌入检索：

```
## 用户偏好

该用户偏好以下类型：机战(12次)、科幻(8次)、原创动画(5次)。
评分倾向：平均 7.8/10，偏好高分作品。
活跃度：共 26 次对话，以发现推荐类查询为主。
```

**注入条件**：`total_sessions >= 3`（避免冷启动时注入无意义的空画像）

---

## 5. L3 公共记忆：社区共识蒸馏

### 5.1 定位

L3 不是关于"某个用户"的记忆，而是关于"Bangumi 社区整体"的记忆：

- 社区热议话题快照（"2026年6月新番满意度调查"）
- 特定类型/标签下的共识排名（"Bangumi 评分最高的 10 部机战番"）
- 周期性事件（"每季度新番放送表"）

这些信息对**所有用户**都有价值，且不会频繁变化。

### 5.2 Phase 5 vs Phase 6

| 事项 | Phase 5 | Phase 6 |
|------|---------|---------|
| 数据库表 | ✅ 建 `public_memories` 表 | - |
| 写入管道 | ❌ 留空（`remember_public()` 为桩） | ✅ Group 分析结果写入 |
| 召回管道 | ❌ 留空 | ✅ 注入 System Prompt |
| ORM 模型 | ✅ `PublicMemory` 类 | - |
| 索引 | ✅ HNSW + GIN | - |

### 5.3 数据结构预览

```sql
CREATE TABLE public_memories (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    topic VARCHAR(300) NOT NULL,              -- 话题标题
    summary_text TEXT NOT NULL,                -- 摘要（200-500 字）
    embedding VECTOR(2048),                    -- 语义向量
    source_type VARCHAR(50) NOT NULL,          -- 'group_discussion' | 'trending' | 'editorial'
    source_id VARCHAR(100),                    -- 来源 group_id / topic_id
    heat_score INTEGER DEFAULT 0,              -- 热度信号
    tags JSONB DEFAULT '[]',                   -- 标签 ['新番', '2026Q2']
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ,                    -- 过期时间（可自动清理）
    is_active BOOLEAN DEFAULT TRUE
);
```

---

## 6. 数据模型设计

### 6.1 表结构 DDL

```sql
-- ============================================================================
-- L2: Session 摘要记忆表
-- ============================================================================
CREATE TABLE IF NOT EXISTS session_memories (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id VARCHAR(255) NOT NULL,
    user_id VARCHAR(255) NOT NULL,

    -- 核心摘要
    summary_text TEXT NOT NULL,               -- LLM 生成的 ~200 字摘要
    embedding VECTOR(2048),                   -- 摘要的向量嵌入，可为 NULL（embedding 失败时）

    -- 结构化元数据
    key_entities JSONB DEFAULT '[]',          -- 提取的关键实体
        -- 格式: [{"type": "subject", "id": "subject_10", "name": "高达Seed"}, ...]
    intent_distribution JSONB DEFAULT '{}',   -- 意图分布
        -- 格式: {"lookup": 1, "discovery": 2}
    tools_used JSONB DEFAULT '[]',            -- 使用的工具列表
    message_count INTEGER DEFAULT 0,          -- 对话轮数

    -- 时间戳
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- 索引
    CONSTRAINT fk_session_memories_user FOREIGN KEY (user_id)
        REFERENCES user_profiles(user_id) ON DELETE CASCADE
);

-- 索引：按 user_id 过滤 + 向量语义检索
CREATE INDEX IF NOT EXISTS ix_session_memories_user_id
    ON session_memories (user_id);
CREATE INDEX IF NOT EXISTS ix_session_memories_embedding
    ON session_memories USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS ix_session_memories_created_at
    ON session_memories (user_id, created_at DESC);


-- ============================================================================
-- L2: 用户画像表
-- ============================================================================
CREATE TABLE IF NOT EXISTS user_profiles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id VARCHAR(255) UNIQUE NOT NULL,

    -- 核心画像（JSONB 灵活 schema）
    preferences_json JSONB DEFAULT '{}',
    -- 结构见 4.2 节画像字段设计

    -- 聚合统计（冗余列，加速常用查询）
    total_sessions INTEGER DEFAULT 0,
    avg_session_length REAL DEFAULT 0.0,
    dominant_intent VARCHAR(50),              -- 最频繁的意图类型

    -- 时间戳
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_active_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 索引
CREATE INDEX IF NOT EXISTS ix_user_profiles_user_id
    ON user_profiles (user_id);
CREATE INDEX IF NOT EXISTS ix_user_profiles_last_active
    ON user_profiles (last_active_at DESC);


-- ============================================================================
-- L3: 公共记忆表（Phase 6 写入）
-- ============================================================================
CREATE TABLE IF NOT EXISTS public_memories (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    topic VARCHAR(300) NOT NULL,
    summary_text TEXT NOT NULL,
    embedding VECTOR(2048),
    source_type VARCHAR(50) NOT NULL,
        -- 'group_discussion' | 'trending' | 'editorial'
    source_id VARCHAR(100),
    heat_score INTEGER DEFAULT 0,
    tags JSONB DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ,
    is_active BOOLEAN DEFAULT TRUE
);

CREATE INDEX IF NOT EXISTS ix_public_memories_embedding
    ON public_memories USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS ix_public_memories_active
    ON public_memories (is_active, created_at DESC)
    WHERE is_active = TRUE;
```

### 6.2 关键设计决策

**为什么用 UUID 主键而非自增 ID？**
- 分布式友好：未来多实例部署无冲突
- 安全：不暴露用户数量/增长速度
- pgvector HNSW 索引基于向量列而非主键，UUID vs 自增对检索性能无影响

**为什么 user_profiles 用 JSONB 而非宽表？**
- 画像维度未来会扩展（如增加"声优偏好"、"动画公司偏好"），JSONB 避免频繁 DDL
- PostgreSQL JSONB 支持索引和部分更新（`jsonb_set`），性能不输宽表
- 冗余列 `total_sessions` / `dominant_intent` 用于加速常见过滤和排序查询

**为什么 session_memories 有 user_id 外键但允许 user_id 不存在于 user_profiles？**
- 实际上应该用外键约束。但如果 user_profile 写入比 session_memory 写入晚（异步更新），会出现短暂不一致。可以考虑软外键（仅索引、无约束），或保证写入顺序。
- **最终决定**：先用普通索引（非外键），在 `update_profile()` 中用 UPSERT 保证 user_profile 行存在，避免写入顺序依赖。

### 6.3 ORM 模型

`database/memory_tables.py`：

```python
class SessionMemory(SQLModel, table=True):
    __tablename__ = "session_memories"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    session_id: str = Field(index=True)
    user_id: str = Field(index=True)
    summary_text: str
    embedding: list[float] = Field(
        default=None,
        sa_column=Column(Vector(2048), nullable=True)
    )
    key_entities: dict = Field(default_factory=dict, sa_column=Column(JSONB))
    intent_distribution: dict = Field(default_factory=dict, sa_column=Column(JSONB))
    tools_used: list[str] = Field(default_factory=list, sa_column=Column(JSONB))
    message_count: int = Field(default=0)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class UserProfile(SQLModel, table=True):
    __tablename__ = "user_profiles"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: str = Field(unique=True, index=True)
    preferences_json: dict = Field(default_factory=dict, sa_column=Column(JSONB))
    total_sessions: int = Field(default=0)
    avg_session_length: float = Field(default=0.0)
    dominant_intent: str | None = Field(default=None)
    first_seen_at: datetime = Field(default_factory=datetime.utcnow)
    last_active_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class PublicMemory(SQLModel, table=True):
    __tablename__ = "public_memories"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    topic: str
    summary_text: str
    embedding: list[float] = Field(
        default=None,
        sa_column=Column(Vector(2048), nullable=True)
    )
    source_type: str
    source_id: str | None = Field(default=None)
    heat_score: int = Field(default=0)
    tags: list[str] = Field(default_factory=list, sa_column=Column(JSONB))
    created_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: datetime | None = Field(default=None)
    is_active: bool = Field(default=True)
```

---

## 7. 记忆生命周期

### 7.1 写入路径（remember）

```
                 ┌────────────────────┐
                 │  Agent 返回 final   │
                 │  _reply 到 main.py  │
                 └─────────┬──────────┘
                           │
              ┌────────────▼────────────┐
              │ main.py:                │
              │ response = ChatResponse │
              │ asyncio.create_task(    │  ← fire-and-forget
              │   _remember_session(    │
              │     session_id,         │
              │     user_id,            │
              │     messages,           │
              │     final_reply,        │
              │     query_intent        │
              │   ))                    │
              └────────────┬───────────┘
                           │
              ┌────────────▼───────────┐
              │ _remember_session():   │
              │ 1. 截断消息到 3000 tok │
              │ 2. LLM 摘要 (≤1s)      │
              │ 3. 提取 key_entities   │
              │ 4. 生成 embedding      │
              │ 5. INSERT session_mem  │
              │ 6. await update_profile│
              │ 7. 异常 → WARNING log  │
              └────────────────────────┘
```

### 7.2 读取路径（recall）

```
        ┌────────────────────────┐
        │ research_reasoning_node  │
        │ / dialogue_reasoning    │
        │ (iterations == 0)       │
        └───────────┬────────────┘
                    │
        ┌───────────▼────────────┐
        │ MemoryManager          │
        │ .recall_for_prompt(    │
        │   user_id,             │
        │   user_message         │
        │ ) → str | None         │
        └───────────┬────────────┘
                    │
        ┌───────────▼────────────┐
        │ 1. 检查缓存命中         │
        │    (同一 user_id        │
        │     session 内复用)     │
        │ 2. 向量化 user_message  │
        │ 3. 检索 top-5 session_  │
        │    memories             │
        │ 4. 距离阈值 0.5 过滤    │
        │ 5. 读取 user_profile    │
        │ 6. 格式化 ≤ 500 tokens  │
        │ 7. 缓存到 request state │
        └───────────┬────────────┘
                    │
                    ▼
            注入 build_system_prompt()
```

### 7.3 更新路径（profile increment）

```
update_profile(user_id, session_summary, intent_dist, key_entities):
  1. SELECT preferences_json FROM user_profiles WHERE user_id = $1
  2. 如不存在 → INSERT 初始空画像
  3. 增量更新各字段（见 4.2 节算法）
  4. UPSERT (ON CONFLICT user_id DO UPDATE)
  5. 耗时 < 50ms（单行更新）
```

### 7.4 遗忘/衰减（Phase 5 不做，保留设计）

Phase 5 不做主动遗忘，但预留机制：

- `session_memories` 保留全部历史。数量巨大时（> 1000 条/user），召回时加 `created_at` 排序，优先返回最近的 50 条做语义检索
- `user_profiles` 的 `entity_affinities` 每次更新对旧分数乘以 0.95 衰减因子
- `public_memories` 有 `expires_at` 字段，可用 cron job 定期清理

---

## 8. 召回算法设计

### 8.1 检索管道

```
user_query ──▶ embedding ──▶ cosine_distance ──▶ top-5 candidates
                                                    │
                                          ┌─────────▼─────────┐
                                          │ 距离阈值 0.5 过滤   │
                                          │ (丢弃语义不相关)    │
                                          └─────────┬─────────┘
                                                    │
                                          ┌─────────▼─────────┐
                                          │ 多因子重排序        │
                                          │ score =             │
                                          │   0.6 * sim_score  │
                                          │ + 0.3 * recency    │
                                          │ + 0.1 * entity_ovp │
                                          └─────────┬─────────┘
                                                    │
                                                    ▼
                                            top-3 最终结果
```

### 8.2 多因子评分

**语义相似度 (0.6)**：
```
sim_score = 1.0 - (cosine_distance / 0.5)  # 归一化到 [0, 1]
# cosine_distance=0 → sim_score=1.0
# cosine_distance=0.5 → sim_score=0.0（阈值边界）
```

**时间衰减 (0.3)**：
```
days_ago = (now - created_at).days
recency = max(0.1, 1.0 - days_ago / 30)   # 30天内线性衰减，不低于 0.1
# 今天 → 1.0, 15天前 → 0.5, 30天前+ → 0.1
```

**实体重叠 (0.1)**：
```
# 从 user_query 中提取实体名（使用 search_bangumi_subject 的关键词或 LLM 提取）
query_entities = extract_entities(user_query)
overlap = len(query_entities ∩ memory.key_entities) / max(len(query_entities), 1)
entity_score = min(1.0, overlap)
```

**最终得分**：
```
final_score = 0.6 * sim_score + 0.3 * recency + 0.1 * entity_score
```

### 8.3 冷启动处理

当用户的 `session_memories` 为空（新用户）时：
- `recall_sessions()` 返回空列表
- `get_user_profile()` 返回 `None`
- `recall_for_prompt()` 返回 `None`
- System Prompt **不注入**"用户历史"区块（而非注入空区块或"暂无历史"）

### 8.4 缓存策略

同一 HTTP 请求内，`recall_for_prompt()` 结果缓存在请求级别（通过 `request.state` 或简单地在 `research_reasoning_node` / `dialogue_reasoning_node` 中只调用一次）。避免同一个 session 内的多轮 ReAct 迭代重复检索。

```python
# research_reasoning_node / dialogue_reasoning_node 中
if state.get("iterations", 0) == 0:
    memory_context = await memory_manager.recall_for_prompt(user_id, user_input)
    # memory_context 存入 state 或作为 prompt 参数传递
else:
    memory_context = state.get("_memory_context", None)  # 复用首轮结果
```

---

## 9. Agent 集成方案

### 9.1 Research Agent 集成

**修改文件**：`agent/research/nodes.py`（仅 `research_reasoning_node`）

**修改点**（在 Step 1 意图分类之后、Step 2 构建 System Prompt 之前）：

```python
# ── Step 1.5: L2 记忆召回（仅首轮） ──────────────────
memory_context = ""
if state.get("iterations", 0) == 0:
    user_id = state.get("user_id", "anonymous")
    if user_id and user_id != "anonymous":
        try:
            memory_context = await memory_manager.recall_for_prompt(
                user_id=user_id,
                query=user_input,
                max_tokens=500,
            )
            if memory_context:
                logger.info("[Memory] 召回 %d 条相关历史", ...)
        except Exception as e:
            logger.warning("[Memory] 召回失败，降级为无记忆模式: %s", e)
            memory_context = ""
```

**System Prompt 注入**（在 `build_system_prompt()` 中）：
```python
system_content = build_system_prompt(
    intent=query_intent,
    critic_feedback=critic_feedback,
    memory_context=memory_context,  # 新增参数
)
```

**prompts.py 修改**：`build_system_prompt()` 增加 `memory_context` 参数，非空时追加到 BASE + intent 之后、critic_feedback 之前：

```python
def build_system_prompt(intent, critic_feedback="", memory_context=""):
    parts = [BASE_SYSTEM_PROMPT]
    parts.append(INTENT_PROMPTS.get(intent, INTENT_PROMPTS["unknown"]))
    if memory_context:
        parts.append(memory_context)
    if critic_feedback:
        parts.append(f"\n## ⚠️ 上一轮回复需要改进\n{critic_feedback}\n...")
    return "\n".join(parts)
```

### 9.2 Dialogue Agent 集成

**修改文件**：`agent/dialogue/nodes.py`（仅 `dialogue_reasoning_node`）

**修改方式**：与 Research Agent 相同——首轮推理前调用 `recall_for_prompt()`，注入到 `build_dialogue_prompt()` 的结果之后。

**特殊考虑**：Dialogue Agent 的 System Prompt 已较长（~600 tokens），记忆注入可能挤占回复空间。因此 Dialogue 的记忆注入上限降至 **300 tokens**，且仅在 `intent ∈ {lookup, discovery}` 时注入（闲聊和常识问答不需要历史）。

### 9.3 main.py 集成

**新增函数** `_remember_session()`：

```python
async def _remember_session(
    session_id: str,
    user_id: str,
    messages: list,
    final_reply: str,
    query_intent: str,
) -> None:
    """Fire-and-forget: 写入 session 摘要 + 更新用户画像。"""
    if user_id == "anonymous":
        return  # 匿名用户不记忆

    try:
        memory_manager = get_memory_manager()
        await asyncio.wait_for(
            memory_manager.remember_session(
                session_id=session_id,
                user_id=user_id,
                messages=messages,
                final_reply=final_reply,
                query_intent=query_intent,
            ),
            timeout=5.0,  # 5 秒硬超时
        )
    except asyncio.TimeoutError:
        logger.warning("[Memory] remember_session 超时 (user=%s)", user_id)
    except Exception as e:
        logger.warning("[Memory] remember_session 失败 (user=%s): %s", user_id, e)
```

**修改 `_chat_research()` 和 `_chat_dialogue()`**：在返回 `ChatResponse` 之前插入：

```python
# 在 return ChatResponse(...) 之前
asyncio.create_task(
    _remember_session(
        session_id=request.session_id,
        user_id=request.user_id,
        messages=result.get("messages", []),
        final_reply=final_reply,
        query_intent=result.get("query_intent", "unknown"),
    )
)
```

### 9.4 MemoryManager 单例

```python
# agent/memory_manager.py

_memory_manager: MemoryManager | None = None

def get_memory_manager() -> MemoryManager:
    global _memory_manager
    if _memory_manager is None:
        settings = get_settings()
        engine = get_engine()  # 复用 database/engine.py 的 engine
        _memory_manager = MemoryManager(
            engine=engine,
            zhipu_api_key=settings.ZHIPU_API_KEY,
            llm_model=settings.LLM_MODEL,  # 用于摘要生成
        )
    return _memory_manager
```

---

## 10. 配置与 Token 预算

### 10.1 新增配置项

```python
# core/config.py 新增

# ── 记忆系统配置 ────────────────────────────
MEMORY_ENABLED: bool = True
"""是否启用 L2 记忆系统。关闭后所有记忆操作变为 no-op。"""

MEMORY_MAX_INJECT_TOKENS: int = 500
"""注入 System Prompt 的记忆文本最大 Token 数。Research 用 500，Dialogue 用 300。"""

MEMORY_RECALL_TOP_K: int = 5
"""语义检索召回的候选 session 摘要数。"""

MEMORY_RECALL_THRESHOLD: float = 0.5
"""语义检索的余弦距离阈值。超过此值的 session 摘要被视为不相关。"""

MEMORY_SUMMARY_MAX_TOKENS: int = 300
"""LLM 生成 session 摘要时的最大 Token 数。"""

MEMORY_LLM_MODEL: str = ""
"""记忆摘要专用 LLM 模型。留空则复用 LLM_MODEL。"""

MEMORY_MIN_SESSIONS_FOR_PROFILE: int = 3
"""开始注入用户画像的最低 session 数（冷启动保护）。"""
```

### 10.2 Token 预算分配

| 组件 | Research Agent | Dialogue Agent |
|------|---------------|----------------|
| L1 总预算 | 8000 tokens | 4000 tokens |
| System Prompt (BASE + intent) | ~1200 | ~600 |
| **L2 记忆注入** | **≤500** | **≤300** |
| 对话历史 | ~5300 | ~2500 |
| LLM 输出缓冲 | ~1000 | ~600 |

Dialogue Agent 的总预算较低（4000），因为它不做深度链式工具调用，历史消息更短。

---

## 11. 边界情况与故障模式

### 11.1 故障矩阵

| 故障场景 | 影响 | 降级策略 |
|---------|------|---------|
| PostgreSQL 断连 | 记忆读写全部失败 | 记录 WARNING，返回无记忆响应 |
| embedding API 超时 | session_memory 无向量 | 存储摘要但 embedding=NULL；召回回退为按 created_at 排序 |
| 摘要 LLM 超时 | session_memory 不写入 | 记录 WARNING，跳过本次记忆 |
| user_profiles UPSERT 冲突 | 画像更新丢失 | 重试 1 次；仍失败则跳过，下次 session 再更新 |
| 新用户（无记忆） | 正常 | 不注入"用户历史"区块 |
| 匿名用户 (user_id="anonymous") | 不记忆 | 跳过所有 L2 操作 |
| 超大对话历史（> 100 轮） | 摘要输入过长 | 先调 `manage_memory()` 截断到 3000 tokens 再送入摘要 LLM |
| 并发 session（同 user_id） | user_profile 竞态 | UPSERT 原子操作，先到先得 |
| 恶意用户（超高频请求） | 大量 session_memories | 不做限流（那是 API 层的事），但每个 user 的召回只扫描最近 100 条 |

### 11.2 语义检索失效场景

- **用户换话题**：本次查询"推荐恋爱番"，历史全是"机战番" → 语义距离大 → 阈值过滤 → 不注入历史 → 正确
- **模糊查询**："那个很火的番" → embedding 质量差 → 召回可能不相关 → 阈值过滤兜底
- **跨语言查询**：历史中文摘要 vs 用户日文查询 → embedding-3 多语言兼容 → 大概率正确

### 11.3 隐私与安全

- `user_id` 由调用方管理——不存储 PII（个人身份信息），仅作为逻辑标识
- 摘要不保留用户原始措辞
- 不提供"按 user_id 查询所有记忆"的 API（外部不可访问）
- 未来可增加 `DELETE /chat/memory` 端点实现"遗忘权"

---

## 12. 性能预算

### 12.1 延迟预算

| 操作 | 目标延迟 | 测量方法 |
|------|---------|---------|
| `recall_sessions()` 向量检索 | < 100ms | pgvector HNSW 索引 + LIMIT 5 |
| `recall_sessions()` 总耗时（含 embedding） | < 200ms | embedding API ~80ms + DB ~100ms |
| `remember_session()` 摘要 LLM | < 1000ms | temperature=0, max_tokens=300 |
| `remember_session()` embedding | < 100ms | Zhipu embedding-3 |
| `remember_session()` 总耗时 | < 1500ms | 异步执行，不阻塞用户 |
| `update_profile()` | < 50ms | 单行 UPSERT |

### 12.2 存储预算

| 数据 | 每用户预估 | 1000 活跃用户预估 |
|------|-----------|-----------------|
| session_memories（每 session） | ~3 KB（摘要 + 向量 + 元数据） | - |
| session_memories（100 sessions/user） | ~300 KB | ~300 MB |
| user_profiles（每用户） | ~5 KB | ~5 MB |
| public_memories（全局） | - | ~50 MB（Phase 6） |
| **总计** | **~305 KB/user** | **~355 MB** |

### 12.3 并发考量

- `recall_sessions()` 是只读操作，无需锁
- `update_profile()` 是单行 UPSERT，PostgreSQL 行级锁自动处理并发
- `remember_session()` 的 INSERT + UPDATE 是独立的——两个并发 session 各自写入自己的 session_memory，user_profile update 使用原子 UPSERT
- 极端情况：同一 user 同时结束 10 个 session → 10 次并发的 `update_profile()` → PostgreSQL 行锁串行化，总耗时 < 500ms

---

## 13. 分步实施计划

### Step 5.1: 数据库表与索引（预计 1-2 小时）

**新建** `database/memory_tables.py`：
- 定义 `SessionMemory`、`UserProfile`、`PublicMemory` 三个 ORM 模型
- 完整字段、类型、注释

**修改** `database/engine.py`：
- `init_db()` 中新表的 HNSW 索引 DDL 加入 `_INDEX_DDL_STATEMENTS` 列表
- 确保 `SQLModel.metadata.create_all(engine)` 自动建表

**交付物**：
- 3 个 ORM 模型
- 数据库迁移（自动通过 `create_all`）
- 索引创建

### Step 5.2: MemoryManager 核心实现（预计 2-3 小时）

**新建** `agent/memory_manager.py`：

```python
class MemoryManager:
    """L2/L3 记忆管理器。

    生命周期管理：
    - 写入：remember_session() → session_memories + user_profiles
    - 读取：recall_for_prompt() → 格式化的记忆文本
    - 公共：remember_public() / recall_public()（Phase 6 桩）
    """

    def __init__(self, engine, zhipu_api_key, llm_model): ...

    # ── 核心 API ──────────────────────────

    async def recall_for_prompt(
        self, user_id: str, query: str, max_tokens: int = 500
    ) -> str:
        """召回并格式化记忆文本，用于注入 System Prompt。
        返回空字符串表示无相关记忆。
        """

    async def remember_session(
        self, session_id: str, user_id: str,
        messages: list, final_reply: str,
        query_intent: str,
    ) -> None:
        """写入 session 摘要并更新用户画像。异常不抛出。"""

    # ── 内部方法 ──────────────────────────

    async def _summarize_session(self, messages, final_reply) -> str: ...
    async def _extract_key_entities(self, summary) -> list[dict]: ...
    async def _embed_text(self, text) -> list[float] | None: ...
    async def _search_similar_sessions(
        self, user_id, query_embedding, limit=5
    ) -> list[SessionMemory]: ...
    async def _update_user_profile(
        self, user_id, session_summary, intent_dist, entities
    ) -> None: ...
    def _format_memory_context(
        self, sessions, profile, max_tokens
    ) -> str: ...

    # ── Phase 6 桩 ────────────────────────

    async def remember_public(self, ...) -> None:
        """[Phase 6] 写入公共记忆。当前为 no-op。"""

    async def recall_public(self, query) -> list[PublicMemory]:
        """[Phase 6] 召回公共记忆。当前返回空列表。"""
```

**依赖**：
- 复用 `database/engine.py` 的 engine（通过 `get_engine()` 或注入）
- 复用 `rag/utils.py` 的 Zhipu 客户端初始化逻辑
- 使用 `create_llm(temperature=0, max_tokens=300)` 生成摘要

### Step 5.3: Agent 节点集成（预计 1-2 小时）

**修改** `agent/research/nodes.py`：
- `research_reasoning_node` 中 Step 1（意图分类）之后插入 L2 召回
- 将 `memory_context` 传入 `build_system_prompt()`

**修改** `agent/research/prompts.py`：
- `build_system_prompt()` 增加 `memory_context` 参数
- 非空时追加在 intent 变体之后

**修改** `agent/dialogue/nodes.py`：
- `dialogue_reasoning_node` 中 Step 2 之后插入 L2 召回
- 仅在 `intent ∈ {lookup, discovery}` 且非匿名用户时召回
- `max_tokens=300`（Dialogue 的预算更紧）

**修改** `agent/dialogue/prompts.py`：
- `build_dialogue_prompt()` 增加 `memory_context` 参数

### Step 5.4: main.py 管道集成（预计 1 小时）

**修改** `main.py`：
- 新增 `_remember_session()` 异步函数
- `_chat_research()` 和 `_chat_dialogue()` 返回前 `asyncio.create_task(_remember_session(...))`
- 确保 `user_id == "anonymous"` 时跳过记忆

**新增**（可选）`GET /chat/history?user_id=xxx` 端点：
- 返回用户最近 N 条 session 摘要（仅文本，不含向量）
- 用于调试和用户查看自己的历史

### Step 5.5: 测试（预计 2-3 小时）

**新建** `test/test_memory_manager.py`：

**测试类 1: SessionMemory CRUD**
- `test_write_and_recall_session`：写入 → 语义检索召回 → 验证相关性
- `test_recall_empty_for_new_user`：新用户返回空
- `test_recall_irrelevant_query`：不相关查询被阈值过滤
- `test_anonymous_user_skipped`：匿名用户不记忆

**测试类 2: UserProfile**
- `test_profile_creation_and_update`：画像创建和增量更新
- `test_profile_cold_start`：少于 3 个 session 不注入画像
- `test_genre_frequency_update`：类型频率正确累加

**测试类 3: Agent 集成**
- `test_memory_context_in_system_prompt`：验证记忆文本被注入
- `test_research_agent_with_memory`：端到端 research + 记忆
- `test_dialogue_agent_with_memory`：端到端 dialogue + 记忆
- `test_memory_graceful_degradation`：记忆系统故障不阻塞主流程

**测试类 4: 边界情况**
- `test_very_long_conversation_summary`：超长对话摘要不超时
- `test_embedding_api_failure`：embedding 失败时优雅降级
- `test_concurrent_session_writes`：并发写入不冲突

### Step 5.6: 文档更新（预计 0.5 小时）

- 更新 `CLAUDE.md` 的记忆系统描述
- 更新 `ROADMAP.md` 标记 Phase 5 完成

---

## 14. 测试策略

### 14.1 测试分层

```
┌─────────────────────────────────────────┐
│          E2E: 端到端记忆连续性            │
│  POST /chat → 记忆写入 → POST /chat     │
│  → 验证 Agent 引用了历史信息              │
├─────────────────────────────────────────┤
│      Integration: Agent + MemoryManager │
│  research_reasoning_node + recall/remember │
│  验证 System Prompt 注入正确             │
├─────────────────────────────────────────┤
│      Unit: MemoryManager 各方法          │
│  recall_for_prompt / remember_session   │
│  / update_profile / 格式化               │
├─────────────────────────────────────────┤
│      Unit: 数据库 ORM 操作               │
│  SessionMemory CRUD / UserProfile UPSERT│
└─────────────────────────────────────────┘
```

### 14.2 测试基础设施

- **测试数据库**：复用现有测试 PostgreSQL（`test/` 目录已有 Docker 脚本）
- **Mock LLM**：摘要 LLM 调用使用 mock（`unittest.mock.AsyncMock`），返回固定摘要文本
- **Mock Embedding**：embedding 调用使用 mock，返回随机但不全为零的向量
- **真实 DB 测试**：CRUD 和召回测试使用真实 PostgreSQL + pgvector（HNSW 索引需要真实数据库）

### 14.3 必须通过的检查点

1. 现有 438 tests 全部通过（无回归）
2. 新测试 ≥ 15 个
3. 匿名用户 (user_id="anonymous") 不触发任何记忆操作
4. 记忆系统故障（DB 断连、embedding 超时）不阻塞 `/chat` 响应
5. 记忆注入的 prompt 不超过设定的 `max_tokens` 上限

---

## 15. 未来扩展

### Phase 5.5: 记忆优化

- **摘要质量 LLM 评估**：用一个轻量 judge 评估摘要是否保留了关键信息
- **重复记忆去重**：相似度 > 0.95 的 session 摘要合并
- **画像定期重建**：每月用全量 session 摘要重新生成画像（而非仅增量更新）
- **记忆衰减**：超过 90 天未访问的 session 摘要权重减半

### Phase 6: 公共记忆写入

- 小组讨论分析结果 → `remember_public()`
- 热门趋势快照 → `remember_public()`
- 在 `research_reasoning_node` / `dialogue_reasoning_node` 中调用 `recall_public()` 注入社区共识
- 公共记忆的过期和刷新机制

### 更远期

- **协同过滤**：基于相似用户的偏好做推荐
- **知识图谱记忆**：从 session 摘要中提取实体关系，构建用户级知识图谱
- **多模态记忆**：用户上传的图片、评分截图也纳入记忆
- **记忆可解释性**：展示"我为什么推荐这个"的记忆溯源

---

## 附录 A: 与现有 ROADMAP 的对比

| 方面 | 原 ROADMAP | 本设计文档 |
|------|-----------|-----------|
| 表设计 | 3 张表，简要描述 | 完整 DDL + ORM + 索引策略 + 设计决策说明 |
| 记忆管理器 | 4 个方法，无实现细节 | 完整 API + 内部方法 + 算法 + 错误处理 |
| 召回策略 | "语义检索" 一笔带过 | 多因子评分 + 时间衰减 + 实体重叠 + 冷启动处理 |
| Agent 集成 | 3 个要点 | 完整流程图 + 两个 Agent 的区别化策略 + 代码插入点 |
| 边界情况 | 无 | 11 种故障场景 + 降级策略 |
| 性能预算 | 无 | 每操作延迟目标 + 存储预估 + 并发分析 |
| 测试策略 | "测试" | 4 层测试分类 + 具体测试用例列表 |

---

## 附录 B: 关键设计决策记录

| 决策 | 选项 A | 选项 B | 选择 | 理由 |
|------|--------|--------|------|------|
| 摘要 vs 存原始消息 | 存原始消息 | LLM 摘要 | **LLM 摘要** | Token 效率、语义质量、隐私友好 |
| 摘要 LLM | 专用小模型 | 复用主 LLM | **复用主 LLM** | 简化架构，摘要调用频率低（每 session 1 次） |
| 画像更新 | 全量重建 | 增量更新 | **增量更新** | 快、不丢失历史、适合高频更新 |
| 写入时机 | 同步（阻塞响应） | 异步（fire-and-forget） | **异步** | 记忆写入不应影响用户体验 |
| 外键约束 | 强制外键 | 软关联（仅索引） | **软关联** | 避免写入顺序依赖，异步场景更鲁棒 |
| 向量检索框架 | pgvector 原生 | 外部向量库（Milvus） | **pgvector** | 已有基础设施，减少运维复杂度 |
| user_profiles 结构 | 宽表（多列） | JSONB | **JSONB** | 画像维度会扩展，JSONB 灵活且性能不差 |

---

> **下一步**：此设计文档提交 review。获批准后启动 Step 5.1 实施。
