# 记忆系统实现细节

## 核心函数速查

### L1 短记忆 (`agent/memory.py`)

| 函数 | 签名 | 用途 |
|------|------|------|
| `count_tokens` | `(text: str) -> int` | 精确 token 计数（tiktoken cl100k_base） |
| `estimate_tokens` | `(messages: list[BaseMessage]) -> int` | 消息列表总 token 数 |
| `trim_messages` | `(messages, max_tokens) -> list` | 滑动窗口截断（SystemMessage 保留） |
| `manage_memory` | `(messages, max_tokens) -> list` | **入口**: 两步截断 → 返回安全消息列表 |

**调用位置**: 每个 `reasoning_node` 开头，LLM invoke 前。

### L2/L3 长记忆 (`agent/memory_manager.py`)

| 方法 | 签名 | 用途 |
|------|------|------|
| `recall_for_prompt` | `(user_id, query, max_tokens) -> str` | **读入口**: 召回 + 格式化 |
| `remember_session` | `(session_id, user_id, messages, final_reply, query_intent) -> None` | **写入入口**: 摘要 + 存储 |
| `_compute_combined_score` | `(cosine_distance, created_at, half_life_days) -> float` | 时间衰减评分 |
| `_search_similar_sessions` | `(user_id, embedding, limit) -> list` | pgvector 语义检索 |
| `_summarize_session` | `(messages, final_reply) -> tuple[str, list[dict]]` | LLM 摘要 + 实体联合提取（JSON 输出） |
| `_extract_key_entities` | `(summary) -> list[dict]` | ⛔ 已废弃 — 实体提取已合并到 `_summarize_session` (LLM JSON 联合输出) |
| `_update_user_profile` | `(user_id, summary, intent, entities) -> None` | 增量画像更新 |
| `_format_memory_context` | `(sessions, profile, max_tokens) -> str` | 格式化注入文本 |
| `_format_profile_summary` | `(profile) -> str` | 画像摘要文本 |

**模块级单例**: `get_memory_manager() -> MemoryManager`

---

## 关键算法详解

### 1. 时间衰减组合评分

**文件**: `agent/memory_manager.py:734-776`

```python
similarity = 1.0 - cosine_distance
days_ago = (now - created_at).total_seconds() / 86400
decay = 0.5 ** (days_ago / half_life_days)
combined_score = similarity * decay
```

**半衰期效应** (half_life=14):

| 天数 | decay 因子 | perfect match 分数 | threshold match 分数 |
|------|-----------|-------------------|---------------------|
| 0 (今天) | 1.00 | 1.00 | 0.50 |
| 1 | 0.95 | 0.95 | 0.48 |
| 7 | 0.71 | 0.71 | 0.35 |
| 14 | 0.50 | 0.50 | 0.25 |
| 30 | 0.23 | 0.23 | 0.11 |
| 60 | 0.05 | 0.05 | 0.03 |

**边缘保护**:
- `half_life_days ≤ 0` → clamp 到 1
- `created_at` 无时区 → 视为 UTC
- 未来时间戳 → `days_ago` clamp 到 0

### 2. 双通道召回

**文件**: `agent/memory_manager.py:84-236`

```
┌─ Step 1: embedding API ──────────────────────┐
│  成功 → 语义检索 (_search_similar_sessions)    │
│  失败 → scored=[]，跳转到 recency fallback      │
└──────────────────────────────────────────────┘
                    ↓
┌─ Step 2: 语义通道 ───────────────────────────┐
│  for (session, distance) in raw_sessions:     │
│    if distance ≤ 0.5:                         │
│      combined = _compute_combined_score(...)  │
│      scored.append((session, combined))       │
└──────────────────────────────────────────────┘
                    ↓
┌─ Step 2.5: recency fallback ─────────────────┐
│  if len(scored) < TOP_K:                      │
│    if query_embedding is not None:             │
│      锚定分支: 计算每条候选 cosine_distance     │
│      过滤 ≤ 0.70 → 计算 combined_score         │
│    else:                                      │
│      纯时效回退: cos_dist=0.0, 仅时间衰减      │
└──────────────────────────────────────────────┘
                    ↓
┌─ Step 2.75: 合并排序 ────────────────────────┐
│  scored.sort(key=combined_score, reverse=True)│
│  sessions = top-K                             │
└──────────────────────────────────────────────┘
                    ↓
┌─ Step 3-4: 画像 + 格式化 ────────────────────┐
│  读取 user_profiles → _format_memory_context  │
│  → token 截断 → 返回字符串或 ""                │
└──────────────────────────────────────────────┘
```

### 3. 会话摘要生成（含实体提取）

**文件**: `agent/memory_manager.py:455-540`

```
输入: messages (完整对话) + final_reply (最终回复)
  ↓
_format_conversation_text: 过滤 SystemMessage + ToolMessage
  → "用户: ...\n助手: ..." 纯文本
  → token 截断到 3000
  ↓
LLM (temperature=0, max_tokens=500, timeout=10s)
  → SUMMARIZE_PROMPT_V2 模板填充（要求 JSON 输出）
  ↓
成功 → 解析 JSON → 返回 (summary, entities) 元组
失败 → 回退为 (final_reply[:200], [])
```

**联合输出格式** (`SUMMARIZE_PROMPT_V2`):
```json
{
    "summary": "用户询问了类似星际牛仔的作品，助手推荐了混沌武士、黑之契约者。用户对混沌武士表现出兴趣。",
    "entities": ["星际牛仔", "混沌武士", "黑之契约者"]
}
```

**设计要点**: 实体提取不再使用独立的正则步骤——LLM 在一次调用中同时完成摘要和实体识别，消除了正则在中文 ACGN 语境下的盲区（日文原名无引号、英文名混排、中文名无书名号等）。

### 4. 用户画像增量更新

**文件**: `agent/memory_manager.py:548-727`

**新用户**: `_build_initial_preferences()` → 实体初始 interest_score=0.5，query_types 计数=1

**已有用户**:
1. `_update_genres()`: 从实体名关键词推断类型（"高达"→"机战"），累加 count，截断 top-10
2. `_update_affinities()`: 指数移动平均（EMA），新实体 0.5，旧实体 `0.9*old + 0.1`，截断 top-20
3. `activity_profile`: query_types 累加，total_sessions +1，dominant_intent 更新

**类型推断关键词映射**（轻量，优于无）:
```python
genre_hints = {
    "高达": "机战", "机器人": "机战",
    "科幻": "科幻", "赛博": "科幻",
    "恋爱": "恋爱", "校园": "校园",
    "悬疑": "悬疑", "推理": "悬疑", "恐怖": "恐怖",
}
```

### 5. 实体提取（已废弃的正则方法）

**文件**: `agent/memory_manager.py:608-622`

> **⛔ 已废弃 (2026-06-16)**：`_extract_key_entities()` 方法已废弃，始终返回空列表 `[]`。
> 实体提取逻辑已合并到 `_summarize_session()` 的一次 LLM 调用中（见 §3 会话摘要生成）。
> 保留此方法仅为向后兼容——旧测试验证其返回 `[]` 的行为。

**原正则实现（仅供历史参考）**:

正则匹配三类引号内的文本（2-30 字符），自动去重：
- 中文书名号: `「...」`
- 中文双引号: `"..."`  
- 英文双引号: `"..."`

**废弃原因**：正则在中文 ACGN 语境下存在大量盲区——日文原名无引号（`進撃の巨人`）、中文名无书名号（`高达SEED`）、英文名混排（`Violet Evergarden`）等。LLM JSON 联合输出（`SUMMARIZE_PROMPT_V2`）从根源上解决了这一问题。

> **历史对比**：Phase 5 初期采用正则提取作为轻量实现（Bug 3）。在实际测试中发现召回质量受实体缺失严重影响，2026-06-13 修复为 LLM JSON 联合输出。正则方法保留为桩函数以维持接口兼容。

---

## 数据库索引

**文件**: `database/engine.py:68-116`

| 索引名 | 表 | 类型 | 用途 |
|--------|---|------|------|
| `ix_session_memories_embedding` | session_memories | HNSW vector_cosine_ops | 语义检索向量近邻 |
| `ix_session_memories_user_created` | session_memories | B-tree (user_id, created_at DESC) | 时效回退查询 |
| `ix_user_profiles_user_id` | user_profiles | B-tree (user_id) | 画像快速查找 |
| `ix_user_profiles_last_active` | user_profiles | B-tree (last_active_at DESC) | 活跃用户排序 |
| `ix_public_memories_embedding` | public_memories | HNSW | Phase 6 公共记忆检索 |
| `ix_public_memories_active` | public_memories | B-tree partial | Phase 6 活跃条目 |

所有索引为幂等 `CREATE INDEX IF NOT EXISTS`，`init_db()` 调用时自动创建。

---

## 优雅降级路径

记忆系统设计为**不阻塞主流程**——任何环节失败都静默回退：

| 故障点 | 降级行为 | 用户体验 |
|--------|---------|---------|
| embedding API 超时/失败 | embedding=None，回退纯时效排序 | 近期记忆仍可用 |
| 语义检索 DB 异常 | RuntimeError 捕获，scored=[] | 无记忆，agent 正常回复 |
| 摘要 LLM 失败 | 回退 `final_reply[:200]` | 摘要质量略降 |
| session_memory INSERT 失败 | SQLAlchemyError 捕获，skip 画像更新 | 本轮不记，下轮不受影响 |
| 画像更新失败 | 异常捕获，仅 WARNING 日志 | 画像保持旧状态 |
| `MEMORY_ENABLED=False` | recall/remember 全部返回 no-op | Agent 退化回无记忆模式 |
| `user_id="anonymous"` | recall/remember 全部返回 no-op | 匿名用户不触发记忆 |

---

## 线程安全与并发

- **MemoryManager 单例**: `get_memory_manager()` 返回进程级唯一实例，无锁（所有操作为 async I/O 或独立 DB session）
- **DB session 隔离**: 每次读写打开独立 `Session(engine)`，用完即关，不跨请求共享
- **Fire-and-forget 写入**: `asyncio.create_task()` 在后台执行，与用户响应并行，无竞态——写入读取走不同 DB session
- **L1 纯内存**: `manage_memory()` 为纯函数（无副作用），并发安全
