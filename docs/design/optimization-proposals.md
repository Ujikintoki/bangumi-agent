# 系统优化方案

> 2026-08-01 | 全系统优化审计，覆盖性能、产品功能、架构三个维度。
> 每个优化项包含：现状、问题、方案、预计工作量、效果。

---

## 一、性能优化

### P0-1: `create_llm()` 实例缓存

**位置**: `agent/llm.py:29`；调用点 `nodes.py:170`, `render.py:232`, `classifier.py:63`, `long_term.py:497`

**现状**: 每次推理轮次都新建 `ChatOpenAI` 实例。一个 deep 模式 12 轮对话中 `create_llm()` 被调用 ~25 次（12 次 reasoning + 可能 12 次 render + 1 次 classifier）。

**问题**: `ChatOpenAI` 内部持有 `httpx.AsyncClient` 连接池。每轮重建实例 → 重建 TCP 连接池 → 每次 LLM 调用多 50-200ms TCP 握手延迟。12 轮 deep 对话累计浪费 1-2s。

**方案**:
```python
# agent/llm.py
from functools import lru_cache

@lru_cache(maxsize=4)
def _get_cached_llm(cache_key: str) -> ChatOpenAI:
    """按 (temperature, max_tokens, model, base_url) 缓存 LLM 实例。"""
    temp, max_tok, model, base_url = json.loads(cache_key)
    return ChatOpenAI(
        model=model, base_url=base_url,
        temperature=temp, max_tokens=max_tok, ...
    )

def create_llm(*, temperature=None, max_tokens=None, model=None, ...):
    key = json.dumps([
        temperature or settings.LLM_TEMPERATURE,
        max_tokens or settings.LLM_MAX_TOKENS,
        model or settings.LLM_MODEL,
        settings.LLM_BASE_URL,
    ])
    llm = _get_cached_llm(key)
    return _maybe_wrap_telemetry(llm, _telemetry_label)
```

LLM 实例是无状态的（所有状态在请求的 messages 里），缓存完全安全。对于不同 label 的 telemetry wrapper，在外部包装即可。

**工作量**: 0.5h | **效果**: deep 模式 -1~2s/请求。

---

### P0-2: `_memory_context` 空字符串缓存

**位置**: `agent/orchestrate/helpers.py:87-89`, `agent/orchestrate/nodes.py:103-113`

**现状**: `_memory_context` 初始值 `""`，但 `""` 是 falsy → `if memory_context:` 永远返回 False → 每次首轮都重复触发 embedding 调用。CLAUDE.md Known Issues 已记录此问题。

**方案**:
```python
# state.py
_UNSET_MEMORY = "__UNSET__"  # sentinel

# 初始 state
"_memory_context": _UNSET_MEMORY,

# helpers.py recall_memory_step
memory_context = state.get("_memory_context", _UNSET_MEMORY)
if memory_context is not _UNSET_MEMORY:
    return memory_context if memory_context != _UNSET_MEMORY else ""
```

**工作量**: 0.25h | **效果**: 消除无效 embedding API 调用。

---

### P1-3: Embedding 结果缓存

**位置**: `agent/memory/long_term.py:407`, `rag/retriever.py:178`

**现状**: 同一用户同一 query 文本在不同请求中多次出现（如用户反复问"推荐好看的动画"），每次都调用 Zhipu embedding API。

**方案**: 进程内 TTL cache（5 分钟）:
```python
import hashlib, time

_EMBED_CACHE: dict[str, tuple[float, list[float]]] = {}

async def embed_with_cache(text: str, ttl: int = 300) -> list[float]:
    key = hashlib.sha256(text.encode()).hexdigest()
    now = time.time()
    if key in _EMBED_CACHE and _EMBED_CACHE[key][0] > now:
        return _EMBED_CACHE[key][1]
    emb = await embed_single(text)
    _EMBED_CACHE[key] = (now + ttl, emb)
    return emb
```

**工作量**: 1h | **效果**: -20-30% embedding API 调用量。

---

### P1-4: Streaming endpoint 无 session cache

**位置**: `main.py:288-301`

**现状**: `/chat/stream` 的 `initial_state` 中没有从 `session_cache.load()` 恢复前序消息。streaming 模式不支持多轮对话。

**方案**: 在 streaming endpoint 的 initial_state 构建前加入:
```python
session_cache = get_session_cache()
cached = await session_cache.load(session_id)
initial_state = {
    "messages": [SystemMessage(...), *cached, HumanMessage(...)],
    ...
}
```

**工作量**: 0.25h | **效果**: streaming 模式支持多轮对话。

---

### P2-5: `init_db()` 启动加速

**位置**: `database/engine.py:38-229`

**现状**: 13 条 `CREATE INDEX IF NOT EXISTS` + 向量维度迁移 + nsfw 迁移 + session_memories 去重。每次启动全量执行。每条 DDL 独立获取数据库连接。

**方案**: 
1. 增加快速检查——`SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'rag_entities')`——已完初始化则跳过所有 DDL
2. 合并 DDL 到同一个连接内执行

**工作量**: 0.5h | **效果**: 启动时间 -50%（非首次）。

---

### P2-6: `manage_memory()` 增量 token 统计

**位置**: `agent/memory/short_term.py:488-525`

**现状**: 每次 `manage_memory()` 调用都从头遍历全部消息计算 token 总数。deep 模式 12 轮对话中消息列表可达 50+ 条，重复计算。

**方案**: 维护 `_last_token_count` 和 `_last_message_count`——只需计算新增消息的 token:
```python
def manage_memory(messages, max_tokens):
    new_count = len(messages)
    if new_count == _state.get('last_count', 0):
        return messages  # 无新消息
    new_tokens = estimate_tokens(messages[_state['last_count']:])
    current = _state.get('last_tokens', 0) + new_tokens
    ...
```

**工作量**: 1h | **效果**: deep 模式 12 轮中减少 ~40% token 统计算量。

---

### P2-7: `_compute_subject_signals()` 懒计算

**位置**: `tools/bgm_tools.py:69-140`

**现状**: `search_bangumi_subject` 返回的每个 subject 都调用 `_compute_subject_signals()` 计算完成率、口碑集中度、热度评分比。但 LLM 只在需要深度分析时才用这些信号——大多数 quick/auto 场景下这些计算被浪费。

**方案**: 将 signals 计算移到 `get_subject_detail`（L2 详情），search 结果（L1 摘要）不计算。或改为 lazily computed property。

**工作量**: 0.5h | **效果**: search 工具返回速度 +10-20ms/条。

---

## 二、产品功能优化

### P0-8: 个性化首页/发现流 `GET /discover`

**用什么食材**: `get_calendar` + `get_trending_subjects` + `get_hot_topics` + L2 记忆

**产品形态**:
```
GET /discover?user_id=xxx

{
  "today_calendar": { "daily_summary": "...", "items": [...] },
  "trending_anime": { "summary": "...", "items": [...] },
  "hot_discussions": { "items": [...] },
  "for_you": [...]   // 基于 L2 记忆的个性化推荐
}
```

**价值**: 用户打开 App 的默认视图——不需要打字就能看到内容。解决当前 `/chat` 的"冷启动"体验问题。`for_you` 字段可以用 L2 召回的实体偏好做 RAG 检索。

**工作量**: 4h | **效果**: 产品形态从"聊天工具"升级为"内容平台"。

---

### P0-9: 作品对比 `POST /compare`

**用什么食材**: `search_bangumi_subject` × 2 + `get_subject_detail` × 2 + RAG + 四种人格

**产品形态**:
```
POST /compare
{
  "subjects": ["EVA", "RahXephon"],
  "dimensions": ["rating", "style", "staff", "reception"]
}

返回并排对比 + agent 风格总结
```

**价值**: Bangumi 用户的自然需求——"EVA 和 RahXephon 哪个更好？"当前只能分两次搜索，无结构化对比。这也是一个很好的 demo 功能。

**工作量**: 3h | **效果**: 高频用户需求 + 演示亮点。

---

### P1-10: "深度解读"模式

**用什么食材**: `get_subject_detail` + `get_subject_characters` + `get_subject_opinions` + `get_subject_episodes` + RAG + deep strategies

**产品形态**:
```
POST /chat { "message": "解读EVA", "depth": "deep", "output_style": "bangumi_cold" }

返回结构化卡片而非纯文本:
{
  "verdict": "一部改变了动画能讨论什么的里程碑",
  "sections": [
    {"title": "时代语境", "content": "1995 年，泡沫经济崩溃后的日本..."},
    {"title": "导演谱系", "content": "庵野秀明在《蓝宝石之谜》之后..."},
    {"title": "评分悖论", "content": "8.7 分但两极分化严重..."},
    {"title": "社区声音", "content": "摘录代表性评论"}
  ],
  "related": ["serial experiments lain", "RahXephon", "少女革命"]
}
```

**价值**: 当前的 deep 模式只是"聊得更深"——输出仍是一段纯文本。结构化深度解读是 Perplexity 式产品体验，可作为简历项目展示。

**工作量**: 4h | **效果**: 简历亮点 + deep 模式差异化。

---

### P1-11: 多轮隐式指代增强

**现状**: E8 场景暴露——用户说"这部"、"那个"时 agent 检索不到前文所指。CLAUDE.md known issues 记录了 render 后历史出现两条连续 AIMessage 的问题。

**方案**: 在 `reasoning_node` 中注入一个纯规则提取的**当前会话作品追踪列表**，从 ToolMessage 中提取被查询过的 subject_id:
```
[当前会话已讨论的作品：EVA (subject_265), RahXephon (subject_xxx)]
```

不需要 LLM 调用——规则从 ToolMessage 的 JSON content 中提取 `"id"` 字段。

**工作量**: 2h | **效果**: 修复 E8 多轮失忆场景。

---

### P2-12: "随便看看"冷启动模式

**用什么食材**: RAG + `get_trending_subjects` + L2 记忆 + 四种人格

**产品形态**: 用户发送空消息或"随便看看" → agent 基于 L2 偏好 + 社区热点主动发起话题:
```
Agent: "你之前聊过今敏——说起来，最近汤浅政明的《犬王》
       在站内评分稳定在 7.8。风格和今敏完全不搭边但都让人
       觉得'动画还能这样拍'。你看过吗？"
```

**实现**: 检测空消息/发现意图 → 注入特殊 scene hint → 从 RAG 按 L2 偏好采样冷门高分 → LLM 生成推荐语。

**工作量**: 2h | **效果**: 冷启动体验 + "主动陪伴"的产品定位。

---

### P2-13: 流式输出升级为 token 级 SSE

**现状**: CLAUDE.md Known Issues——"Streaming endpoint 仅节点级，非逐 token"。

**方案**: 在 render_node 中用 LLM 的 `astream()` 替代 `ainvoke()`，逐 token 推送到前端。前端体验从"等 2 秒 → 看到完整回复"变成逐字出现。

**工作量**: 3h | **效果**: 前端体验质的提升。

---

### P2-14: 每日 digest（推送）

**用什么食材**: `get_calendar` + `get_trending_subjects` + L2 记忆 + 四种人格

**方案**: 用外部定时任务（或 `CronCreate`），每天生成一条个性化 digest:
```
"早上好～今天有 3 部新番更新。另外，你上周说喜欢意识流——
《铃音》的导演新作刚发布了 PV，你要不要看看？"
```

**工作量**: 3h | **效果**: 用户留存 + 产品差异化。

---

### P2-15: 极简前端

**现状**: 只有 REST API + SSE。无前端意味着不能直接展示给面试官，也没有"使用量、下载量"等简历最缺的数字。

**方案**: htmx + 纯 HTML + SSE——不需要 React/Vue。一个 ~100 行 HTML 文件:
```html
<div id="chat">
  <div id="messages"></div>
  <input name="message" hx-post="/chat" hx-target="#messages" />
</div>
```

部署到 Vercel/Cloudflare Pages 可拿公网 URL。

**工作量**: 3h | **效果**: 从 API 升级为可演示产品。

---

## 三、架构优化

### P0-16: LangGraph Checkpointing 持久化

**位置**: `agent/graph.py:114-150`

**现状**: `graph.compile()` 无 checkpoint 配置。服务重启 → 所有进行中对话状态丢失。Session cache 是内存级，重启消失。

**方案**: PostgreSQL-backed checkpointing:
```python
from langgraph.checkpoint.postgres import PostgresSaver

checkpointer = PostgresSaver(engine)
graph = builder.compile(checkpointer=checkpointer)
```

**效果**: 
- 对话可在服务重启后恢复
- 支持 `graph.aget_state(config)` 获取任意时刻的对话快照
- 用 `thread_id` 做分支对话（"等一下，换个方向" → fork 新分支）

**工作量**: 2h | **效果**: 对话可恢复 + 分支对话能力。

---

### P0-17: 数据库 Migration 正规化

**位置**: `database/engine.py:38-229`

**现状**: `init_db()` 里塞了 ~130 行 raw SQL DDL（向量维度迁移、nsfw 列添加、去重、13 条索引），没有 migration tracking。无法知道哪些已执行、无法回滚。

**方案 A（推荐）**: 在数据库维护 `_migrations` 表，每条 migration 执行前检查:
```sql
CREATE TABLE IF NOT EXISTS _migrations (
    name TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ DEFAULT NOW()
);
```

`init_db()` → `for migration in MIGRATIONS: if not applied(migration): run(migration)`

**方案 B**: Alembic（SQLAlchemy 官方 migration 工具）。对于当前项目规模可能过度，但长期更规范。

**工作量**: 2h | **效果**: migration 可追溯、可回滚。

---

### P1-18: RAG v0 + Critic 代码清理

**位置**: 
- `rag/retriever.py` `BangumiRetriever` + `SearchResult` (~350 行 deprecated)
- `database/rag_tables.py` `BangumiChunk` (~60 行 deprecated)
- `rag/ingestion.py` `BangumiIngestor` (~140 行 deprecated)
- `agent/orchestrate/nodes.py` `critic_node` + `_critic_node_rule` + `_critic_node_llm` (~270 行)
- `deep_strategies.py` `CRITIC_SYSTEM_PROMPT` (~30 行)

**总计 ~850 行 dead code**。

**决策**: Critic 设计假设是"数据完整性优先"（Research Agent 遗留），与 Companion Agent "够了就停"定位冲突。建议删除——git history 保留。

**工作量**: 1h | **效果**: -850 行 dead code，降低维护心智负担。

---

### P1-19: per-depth TOOL_GUIDANCE 变体

**位置**: `agent/orchestrate/prompt_builder.py:46-74`

**现状**: `TOOL_GUIDANCE` 是通用 ~400 token 文本，所有 depth 模式共用。deep 需要更详细的工具链指导（"search → detail → characters 串行链"），当前仅通过 `DEEP_SCENE_HINTS` 补充了 intent 层面，未补充工具链层面。

**方案**: 拆分为:
```python
TOOL_GUIDANCE = {
    "quick": "强调 1 次搜索够用就停...",
    "auto": TOOL_GUIDANCE,  # 当前文本
    "deep": TOOL_GUIDANCE + "串行深挖指南：search 拿 ID → detail 拿详情 → 按标签搜同类 → characters 看阵容..."
}
```

`build_system_prompt()` 按 depth 选择对应版本。

**工作量**: 2h | **效果**: deep 模式工具链质量提升。

---

### P1-20: Session Cache 抽象化（可选 Redis 后端）

**位置**: `agent/memory/cache.py`

**现状**: `SessionCache` 是进程内 `dict`。多 worker 部署（uvicorn workers > 1）时 session cache 不共享。服务重启丢失所有进行中对话。

**方案**: `SessionCache` 接口已抽象好（`load`/`store`/`clear`）。拆分为 `BaseSessionCache` → `InMemorySessionCache` + `RedisSessionCache`（`REDIS_URL` 存在时自动选 Redis）。

**但**: 当前项目规模（单机、个人项目）Redis 可能过度工程。建议先做抽象接口，内存实现为默认。

**工作量**: 3h | **效果**: 多 worker 共享 session + 重启不丢失。

---

### P2-21: Topology early exit（跳过 render）

**位置**: `agent/graph.py:75-103`

**现状**: `reasoning → tool → reasoning → ... → render → END`。render 始终执行。

**方案**: 增加 early exit 边——如果 reasoning_node 返回 AIMessage 且 content 已是终端回复（`is_terminal_response()`），跳过 render 直接 END。节省一次 LLM 调用:
```python
def route_after_reasoning(state):
    last_msg = messages[-1]
    if last_msg.tool_calls: return "tool_node"
    if is_terminal_response(last_msg.content): return END  # ← 新增
    return "render_node"
```

**工作量**: 0.5h | **效果**: 终端回复场景 -1 次 LLM 调用。

---

### P2-22: Rate Limiting + 请求超时

**位置**: `main.py:174`

**现状**: 无任何 rate limiting 或 request timeout。deep 模式可长达 60s。恶意/无意大量 deep 请求可占满 worker。

**方案**:
```python
from asyncio import Semaphore

_AGENT_SLOTS = Semaphore(5)  # 最多 5 个并发 agent 调用

@app.post("/chat")
async def chat(request: ChatRequest):
    try:
        async with asyncio.wait_for(
            _AGENT_SLOTS.acquire(), timeout=30
        ):
            result = await agent_app.ainvoke(initial_state)
    except asyncio.TimeoutError:
        return ChatResponse(reply="服务器繁忙，请稍后重试", ...)
    finally:
        _AGENT_SLOTS.release()
```

**工作量**: 1h | **效果**: 生产安全。

---

### P2-23: 增加结构化日志/指标导出

**位置**: 全局

**现状**: 只有 `logging` 到 stdout。无可观测性基础设施——无法知道 P50/P95/P99 延迟、错误率、LLM token 消耗趋势。

**方案**: 
1. 将 `RequestTelemetry` 的数据结构化导出（JSON 行格式 → 可用 `jq` 分析）
2. 增加关键指标: `/chat` 请求延迟分布、depth 模式分布、工具调用成功率、render 跳过率、L2 召回命中率

**工作量**: 2h | **效果**: 可观测性。长期支撑 eval 体系建设。

---

## 优先级矩阵

| # | 类型 | 优化项 | 工作量 | 效果 |
|---|------|--------|--------|------|
| 🔴 P0-1 | 性能 | `create_llm()` 缓存 | 0.5h | deep 模式 -1~2s |
| 🔴 P0-2 | 性能 | `_memory_context` bug | 0.25h | 消除无效 embedding 调用 |
| 🔴 P0-8 | 产品 | `GET /discover` | 4h | 冷启动体验 |
| 🔴 P0-9 | 产品 | `POST /compare` | 3h | 高频需求+演示亮点 |
| 🔴 P0-16 | 架构 | Checkpointing 持久化 | 2h | 对话可恢复 |
| 🔴 P0-17 | 架构 | Migration 正规化 | 2h | 可维护性 |
| 🟡 P1-3 | 性能 | Embedding 缓存 | 1h | -20-30% API 调用 |
| 🟡 P1-4 | 性能 | Streaming session cache | 0.25h | streaming 多轮对话 |
| 🟡 P1-10 | 产品 | 深度解读模式 | 4h | 简历亮点 |
| 🟡 P1-11 | 产品 | 隐式指代增强 | 2h | E8 修复 |
| 🟡 P1-18 | 架构 | Dead code 清理 | 1h | -850 行 |
| 🟡 P1-19 | 架构 | per-depth TOOL_GUIDANCE | 2h | deep 工具链质量 |
| 🟡 P1-20 | 架构 | Session cache 抽象 | 3h | 多 worker + 重启 |
| 🟢 P2-5 | 性能 | `init_db()` 加速 | 0.5h | 启动 -50% |
| 🟢 P2-6 | 性能 | 增量 token 统计 | 1h | CPU -40% |
| 🟢 P2-12 | 产品 | "随便看看"模式 | 2h | 冷启动 |
| 🟢 P2-13 | 产品 | Token 级 SSE | 3h | 前端体验 |
| 🟢 P2-14 | 产品 | 每日 digest | 3h | 留存 |
| 🟢 P2-15 | 产品 | 极简前端 | 3h | 可演示 |
| 🟢 P2-21 | 架构 | Render early exit | 0.5h | -1 次 LLM 调用 |
| 🟢 P2-22 | 架构 | Rate limiting | 1h | 生产安全 |
| 🟢 P2-23 | 架构 | 结构化日志 | 2h | 可观测性 |

---

## 建议执行顺序

**第一波（今天，2-3h）**:
1. P0-2 `_memory_context` bug — 15 分钟
2. P0-1 `create_llm()` 缓存 — 30 分钟
3. P0-18 Dead code 清理 — 1 小时

**第二波（本周，6-8h）**:
4. P0-8 `GET /discover` — 4 小时
5. P0-16 Checkpointing — 2 小时

**第三波（有时间就做）**:
6. P0-9 `POST /compare` — 3 小时
7. P1-3 Embedding 缓存 — 1 小时
8. P1-19 per-depth TOOL_GUIDANCE — 2 小时
