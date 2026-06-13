# 记忆系统架构

## 三层记忆模型

```
┌─────────────────────────────────────────────────────────┐
│                    System Prompt                        │
│  ┌──────────────────────────────────────────────────┐   │
│  │  L2 跨 session 语义召回（双通道）                  │   │
│  │  "3 天前你问过高达Seed... 上周讨论过机战番..."      │   │
│  ├──────────────────────────────────────────────────┤   │
│  │  L3 用户画像（冷启动保护: ≥5 sessions）             │   │
│  │  "偏好机战/科幻类作品，关注高达Seed/星际牛仔"       │   │
│  ├──────────────────────────────────────────────────┤   │
│  │  L1 同 session 滑动窗口                            │   │
│  │  HumanMessage + AIMessage + ToolMessage ...        │   │
│  └──────────────────────────────────────────────────┘   │
│  BASE System Prompt + intent 变体 + critic_feedback     │
└─────────────────────────────────────────────────────────┘
```

### L1 短记忆 — 滑动窗口截断

**文件**: `agent/memory.py`

**职责**: 确保 token 预算不超限，保留最近消息。

**两步策略**:
1. **单条截断**: ToolMessage 超过 2000 tokens → 截断内容（不丢弃整条）
2. **列表截断**: 总 token 超预算时，从头部丢弃旧消息。SystemMessage 始终保留

**Token 预算分配**:

| Agent | 总预算 | System Prompt | L2 注入 | 对话历史 | 输出缓冲 |
|-------|--------|--------------|---------|---------|---------|
| Research | 8000 | ~1200 | ≤500 | ~5300 | ~1000 |
| Dialogue | 4000 | ~600 | ≤300 | ~2500 | ~600 |

**触发时机**: 每个 `reasoning_node` 开头，在 LLM 调用前。

### L2 长记忆 — 跨 session 语义召回

**文件**: `agent/memory_manager.py`

**职责**: 将历史对话的 LLM 摘要向量化存储，新对话时语义检索相关历史。

**写入路径（fire-and-forget）**:
```
Agent 返回 final_reply
  → main.py: asyncio.create_task(_remember_session())
    → MemoryManager.remember_session()
      → LLM 摘要（~200 字中文）
      → 正则提取实体名（「」"" 引号内）
      → embedding API（Zhipu embedding-3, 2048 维）
      → INSERT session_memories
      → UPSERT user_profiles（增量更新偏好/亲和度）
```

**读取路径（同步）**:
```
reasoning_node 首轮
  → MemoryManager.recall_for_prompt(user_id, query)
    → embedding API 向量化查询
    → pgvector cosine_distance 语义检索
    → 主阈值过滤（≤0.5）+ 时间衰减
    → 不足 TOP_K → recency fallback + 锚定过滤（≤0.70）
    → 合并 + combined_score 降序 → top-K
    → 读取 user_profiles
    → 格式化注入文本（≤ max_tokens）
```

### L3 用户画像 — 增量偏好聚合

**文件**: `agent/memory_manager.py`（同 L2 管理器）

**职责**: 跨 session 聚合用户偏好，提供"用户是谁"的长期上下文。

**画像结构** (`preferences_json`):
```json
{
  "favorite_genres": [
    {"genre": "机战", "count": 12},
    {"genre": "科幻", "count": 8}
  ],
  "entity_affinities": {
    "高达Seed": {"name": "高达Seed", "interest_score": 0.9}
  },
  "activity_profile": {
    "query_types": {"discovery": 15, "lookup": 8},
    "total_sessions": 26
  }
}
```

**冷启动保护**: `total_sessions < 5` 时不注入画像（`MEMORY_MIN_SESSIONS_FOR_PROFILE=5`）。

**增量更新**:
- 类型频率: 从实体名关键词推断（"高达"→"机战"），累加 count
- 实体亲和度: 指数移动平均 `new = 0.9 × old + 0.1`
- 意图分布: 累加 query_types 计数

---

## 召回策略：双通道 + 时间衰减

### 通道 1: 语义通道

```
pgvector cosine_distance(query_embedding, session_embedding)
  → 过滤: distance ≤ 0.5 (MEMORY_RECALL_THRESHOLD)
  → 评分: combined_score = (1 - distance) × 0.5^(days_ago / 14)
```

### 通道 2: 时效回退

触发条件: 语义通道命中数 < `MEMORY_RECALL_TOP_K` (5)

```
按 created_at DESC 取最近 session
  → 计算 cosine_distance
  → 过滤: distance ≤ 0.70 (MEMORY_RECENCY_FALLBACK_THRESHOLD) — 最小语义锚定
  → 评分: combined_score = (1 - distance) × 0.5^(days_ago / 14)
```

**最小语义锚定**: 回退通道不是为了"凑数"——即使是最新 session，如果语义完全不相关（cosine_dist > 0.70），也不会注入。这防止了"昨天聊高达，今天问轻音少女，注入高达无关记忆"的噪音。

### 合并排序

```
scored = 语义通道结果 + 回退通道结果
scored.sort(key=combined_score, reverse=True)
return scored[:TOP_K]
```

### embedding API 不可用时

回退到纯时效排序：按 `created_at` 降序取最近 session，cos_dist 按 0.0 计（退化为纯时间衰减排序）。没有锚定过滤（无法计算距离）。

---

## 数据模型

### session_memories

```
id (UUID, PK)
session_id (str, indexed)
user_id (str, indexed)
summary_text (str)              ← LLM 生成的 ~200 字中文摘要
embedding (vector(2048))        ← Zhipu embedding-3 向量，可为 NULL
key_entities (JSONB)            ← [{"type":"subject","name":"高达Seed"}]
intent_distribution (JSONB)     ← {"lookup":1, "discovery":2}
tools_used (JSONB)              ← ["search_bangumi_subject", "get_bangumi_subject_detail"]
message_count (int)             ← 对话轮数
created_at (datetime)
```

**索引**:
- `ix_session_memories_embedding` — HNSW vector_cosine_ops（语义检索）
- `ix_session_memories_user_created` — B-tree (user_id, created_at DESC)（时效回退）

### user_profiles

```
id (UUID, PK)
user_id (str, UNIQUE, indexed)
preferences_json (JSONB)        ← 画像数据（灵活 schema）
total_sessions (int)            ← 冷启动保护阈值
avg_session_length (float)      ← EMA 更新
dominant_intent (str)           ← 最频繁意图
first_seen_at (datetime)
last_active_at (datetime)
updated_at (datetime)
```

### public_memories (Phase 6 桩)

表已建，索引已建，代码为 no-op。Phase 6 实现群体智慧注入。

---

## Agent 集成点

### Research Agent (`agent/research/nodes.py`)

```
reasoning_node (首轮 iterations==0):
  ├── manage_memory (L1 截断)
  ├── classify_intent
  ├── memory_manager.recall_for_prompt (L2 + L3 召回)  ← 仅首轮
  ├── build_system_prompt (含 memory_context)
  └── LLM invoke

main.py 响应返回后:
  └── asyncio.create_task(_remember_session)  ← fire-and-forget
```

### Dialogue Agent (`agent/dialogue/nodes.py`)

```
dialogue_reasoning_node (首轮 iterations==0):
  ├── manage_memory (L1 截断)
  ├── classify_intent
  ├── memory_manager.recall_for_prompt (L2 + L3 召回)  ← 仅首轮
  ├── build_dialogue_prompt (含 memory_context)
  └── LLM invoke
```

**设计决策**: 记忆召回仅在首轮执行。后续轮次（工具消化、Critic REVISE）跳过——记忆已在首轮消费，重复注入浪费 embedding API 和 token 预算。

### 为什么 dialogue 全意图召回？

Research Agent 按意图过滤记忆召回，但 Dialogue Agent 不区分意图——即使是 chitchat 也召回。原因：分类器只看当前消息，"你怎么看？"被判为 chitchat，但实际是上下文追问。recency fallback 确保短追问也能找回上一轮的话题。
