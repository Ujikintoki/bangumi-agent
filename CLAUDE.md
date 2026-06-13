# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

A **Stateful AI Agent** for the [Bangumi](https://bgm.tv) ecosystem — natural-language understanding, multi-tool orchestration, and long-term memory for anime/manga/music/game discovery. Built as a FastAPI microservice with LangGraph ReAct agent, PostgreSQL + pgvector RAG, and Zhipu embedding-3.

Phase 5 三层记忆系统已落地：L1 同 session 滑动窗口 + L2 跨 session 语义召回（双通道：语义 + 时效回退，时间衰减 + 最小语义锚定）+ L3 用户画像增量更新。

## Commands

```bash
# Run the app
uvicorn main:app --reload --port 8000

# Run all tests (require PostgreSQL with pgvector running locally)
pytest test/ -v

# Run a single test file
pytest test/test_rag.py -v

# Run a single test function
pytest test/test_rag.py::TestPreciseFiltering::test_exclude_nsfw_blocks_adult_content -v

# Start PostgreSQL + pgvector (Docker, required for tests and RAG)
docker run -d --name bangumi-pg \
  -e POSTGRES_USER=myuser -e POSTGRES_PASSWORD=mypassword \
  -e POSTGRES_DB=bangumidb -p 5432:5432 pgvector/pgvector:pg16
```

## Architecture

The system follows a **layered architecture** with strict separation of concerns.

### Agent topology (Phase 4 — current)

```
                        START
                          │
                          ▼
                   reasoning_node
                   ├─ manage_memory (两层截断: 单条 ToolMessage + 列表滑动窗口)
                   ├─ classify_intent (规则优先 + LLM fallback, 6 类)
                   ├─ build_system_prompt (BASE + intent 变体 + critic_feedback)
                   └─ LLM invoke
                       ├─ chitchat/factual → 不绑工具
                       └─ 其余 intent → bind_tools(12 工具，消化态不摘工具)
                       └─ 其余 intent → bind_tools(12 工具)
                          │
                          ▼ (条件边: route_after_reasoning — 原生消息路由)
               ┌──────────┼──────────┐
               │          │          │
        AIMessage.      chitchat    其他无工具
        tool_calls       无工具
        非空               │          │
               │          ▼          ▼
               │        END       critic_node
               │      (快速通道)   (rule/llm 双模式)
               │                     │
               ▼                     ▼ (条件边: route_after_critic)
          ToolNode            PASS/超限 → END
         (LangGraph           REVISE  → reasoning_node (重试)
          内置)                    │
               │                   │
               └──────────┬────────┘
                          ▼
                   reasoning_node（固定边: 消化工具结果，如需继续调工具则 model 自主判定）
```

**关键设计点：**
- **原生消息路由**：`route_after_reasoning` 直接读 `state["messages"][-1]` 的 `tool_calls` 属性，不依赖冗余状态字段
- **固定边** `tool_node → reasoning_node`：工具执行后必须回到 reasoning 消化结果，不直接进 critic
- **消化态引导**：`reasoning_node` 检测到入口最后一条为 ToolMessage 时，注入引导指令让模型优先综合数据输出文本，但不强制解绑工具——允许模型在确实需要更多数据时（如 search → get_detail 串行依赖）继续调用工具。循环保护由 Critic 重复调用检测 + `_MAX_ITERATIONS` 熔断负责。不再强制解绑是 XML 泄漏（DeepSeek 在无工具通道时将 `<function_calls>` 喷到 `.content`）的最终修复
- chitchat 快速通道：跳过工具和 critic，直达 END
- `critic_feedback` 定向注入下一轮 System Prompt（`"<缺陷> | <建议> | <缺失>"` 格式）
- 最大 10 轮迭代熔断（`_MAX_ITERATIONS = 10`，graph 和 critic 双重检查）
- `error_flag` 优雅降级：置 True 时 reasoning_node 返回兜底消息

### Agent 目录结构

```
agent/
├── classifier.py    # 两阶段意图分类 (优先级规则 + LLM fallback)
├── llm.py           # create_llm() 多 Provider 工厂 (Azure/OpenAI/DeepSeek)
├── memory.py        # 两层截断: 单条 ToolMessage 内容截断 + 列表滑动窗口 (tiktoken cl100k_base)
│
├── research/        # 研究助手 agent（当前主力）
│   ├── state.py     # AgentState TypedDict (8 字段，无 last_tool_calls)
│   ├── graph.py     # build_graph() + 2 条件边 + 1 固定边 + 快速通道
│   ├── nodes.py     # reasoning_node (工具始终可用, 消化态仅引导), critic_node (rule/llm 双模式)
│   └── prompts.py   # BASE + 5 个 intent 变体 + CRITIC_SYSTEM_PROMPT
│
└── dialogue/        # 对话式 agent（Phase 4 — 快 > 准，30-150 字，<2s）
    ├── state.py     # DialogueState（5 字段，无 critic，_MAX_ITERATIONS=3）
    ├── graph.py     # 2 节点拓扑: reasoning → (条件) tool/END, tool → reasoning(固定边)
    ├── nodes.py     # dialogue_reasoning_node（极简推理，无消化态引导/Critic；含 XML 泄漏防护）
    └── prompts.py   # Bangumi娘人格 prompt（腹黑萝莉，黑色幽默）
```

共用层：`tools/`, `rag/`, `clients/`, `core/config.py`, `agent/llm.py`, `agent/memory.py`, `agent/classifier.py`

### Layer responsibilities

| Layer | Module | Role |
|-------|--------|------|
| Entry | `main.py` | FastAPI app, CORS, health check, POST `/chat` + `/chat/dialogue` + `/chat/stream` |
| Config | `core/config.py` | pydantic-settings from `.env`, `@lru_cache` singleton |
| Agent | `agent/research/` | LangGraph StateGraph: reasoning → (条件) tool/critic/END, tool → reasoning (固定边), 消化态解绑工具. 最大 10 轮强制终止 |
| Tools | `tools/bgm_tools.py` | LangChain `@tool` functions with Pydantic `args_schema`. Returns natural-language strings to the LLM |
| Client | `clients/` | `BaseClient` (httpx, retry, auth) → `BangumiClient` (business methods) → `sanitizers` (field whitelisting, type coercion) |
| RAG | `rag/` | `text_processor.py` (tiktoken sliding-window chunking) → `ingestion.py` (batch embedding + DB write) → `retriever.py` (hybrid vector + JSONB filter search) |
| Database | `database/` | SQLModel + pgvector. `engine.py` manages connection pool and DDL (HNSW + GIN trigram indexes) |
| Schemas | `schemas/tools_input.py` | Pydantic v2 input contracts for every tool — the "type contract" between LLM and tool functions |

### Data flow for a search query

```
POST /chat → agent_app.invoke(initial_state)
  → reasoning_node:
      manage_memory (L1 两层截断)
      → classify_intent (规则/LLM, 仅首轮)
      → memory_manager.recall_for_prompt (L2 语义召回 + recency fallback + 时间衰减, 仅首轮)
      → build_system_prompt (BASE + memory_context + intent 变体 + critic_feedback)
      → LLM invoke (绑定工具: lookup/discovery/realtime, 解绑: chitchat/factual/消化态)
      → AIMessage(tool_calls=[...])
  → route_after_reasoning: 原生路由 —
      AIMessage.tool_calls 非空 → tool_node
      chitchat → END (快速通道)
      其他 → critic_node
  → ToolNode: 并发执行工具调用 (RAG 检索 + Bangumi API)
      → ToolMessage(content=格式化文本)
  → reasoning_node (固定边): 消化态 — 解绑工具，强制 LLM 输出文本回复
      → AIMessage(content=文本回复)
  → critic_node: rule/llm 评估 → PASS/REVISE
  → PASS → END, REVISE → reasoning_node (重新绑定工具, 注入 critic_feedback)
```

### RAG architecture — single-table polymorphism

All three entity types (Subject, Character, Person) share one `rag_entities` table with a prefixed primary key (`subject_10`, `character_5`, `person_3`). The `entity_type` column + JSONB `meta_info` distinguish entity-specific fields. Retrieval pipeline: scalar pre-filter (`entity_type = ?`) → vector cosine distance → distance threshold cutoff (0.65) → semantic bucket sort with entity-type-specific heat signals (rating_total for subjects, collects for characters/persons).

## Key conventions

- **Async-first**: all network I/O uses `async/await`. HTTP client is `httpx.AsyncClient`.
- **Error handling**: API failures return `{"_error": "..."}` dicts — never throw. Callers check `"_error" in result` and propagate gracefully. BaseClient retries on 429/502/503/TimeoutException with exponential backoff (max 3 attempts).
- **Sanitizer pattern**: pure functions that whitelist fields, coerce magic numbers to human-readable labels, hard-truncate text, and filter noise (<4 char comments, pure numbers/dates). No self, no side effects.
- **Agent state** (`agent/research/state.py`): TypedDict with 8 fields — `messages` (Annotated[list[BaseMessage], operator.add]), `iterations`, `critic_status` (PENDING/PASS/REVISE), `critic_feedback`, `query_intent` (chitchat/factual/lookup/discovery/realtime/unknown), `session_id`, `user_id`, `error_flag`. 路由由原生消息属性 (`messages[-1].tool_calls`) 驱动，不依赖冗余状态字段。`_MAX_ITERATIONS = 10`。
- **Token input schemas** (`schemas/tools_input.py`): every tool's parameters are defined as Pydantic BaseModel subclasses with Field descriptions written for LLM consumption.
- **`.env`** is at project root, loaded by `core/config.py`. Key variables: `DATABASE_URL`, `BANGUMI_APP_ID`, `BANGUMI_APP_SECRET`, `ZHIPU_API_KEY`, `EMBEDDING_DIMENSION` (default 2048).

## Current state and known issues

### Phase 3 — 已完成 (2026-06-06)

全部 9 个 Step 完成，438 tests 通过。LLM + 工具 + Critic + 记忆 + 端点全线贯通。

### 2026-06-09 架构重构（已完成）

以下 2026-06-07 review 发现的问题已修复：

- ✅ **critic_feedback 异常时丢弃** — `reasoning_node` 异常 handler 现在保留 `state.get("critic_feedback", "")`
- ✅ **超大 ToolMessage 无截断** — 两层截断：单条 >2000 tokens 内容截断 + 列表滑动窗口（`agent/memory.py`）
- ✅ **`_MAX_ITERATIONS` 重复定义** — 统一定义在 `agent/research/state.py:70` (=10)
- ✅ **prompt 注入风险** — `classifier.py` 对用户输入做 `{`→`{{` `}`→`}}` 转义
- ✅ **消化步仍绑定工具** — 消化态检测 `is_digesting` 后解绑全部工具，强制 LLM 输出文本
- ✅ **`last_tool_calls` 冗余字段** — 已从 State 删除，路由改为原生 `messages[-1].tool_calls`
- ✅ **`search_bangumi_subject` / `get_bangumi_subject_detail` 裸返 JSON** — 改为结构化文本输出

### Phase 5 — 已完成 (2026-06-13)

三层记忆系统全线贯通：

| 层级 | 实现 | 存储 |
|------|------|------|
| L1 短记忆 | `agent/memory.py` — 滑动窗口 + 两层截断 | 内存 |
| L2 长记忆 | `agent/memory_manager.py` — 跨 session 语义召回 | PostgreSQL + pgvector |
| L3 用户画像 | `agent/memory_manager.py` — 增量更新偏好/亲和度 | PostgreSQL JSONB |

**召回策略（双通道）：**
1. **语义通道**：query embedding → pgvector cosine_distance ≤ 0.5 → 组合分数排序（`similarity × 0.5^(days/14)`）
2. **时效回退**：语义不足 TOP_K 时补齐最近 session，cosine_distance ≤ 0.70 锚定过滤
3. 合并 → combined_score 降序 → 取 top-K → 格式化注入 System Prompt

**写入策略**：fire-and-forget（`asyncio.create_task`），LLM 摘要 → embedding → INSERT session_memories + UPSERT user_profiles。异常静默降级，不阻塞主流程。

**配置**：`MEMORY_ENABLED`、`MEMORY_RECALL_TOP_K=5`、`MEMORY_RECALL_THRESHOLD=0.5`、`MEMORY_TIME_DECAY_HALF_LIFE_DAYS=14`、`MEMORY_RECENCY_FALLBACK_THRESHOLD=0.70`、`MEMORY_MIN_SESSIONS_FOR_PROFILE=5`

**测试**：`test/test_memory.py` (L1, 21 tests) + `test/test_memory_manager.py` (L2/L3, 15 tests) + `test/test_memory.py::TestComputeCombinedScore` (10 tests)

**详细文档**：`docs/memory/` — 架构、实现、配置、测试、调试综合手册

### 当前已知问题（2026-06-13）

**🟡 中等：**

1. **流式端点仅节点级**：`/chat/stream` 推送节点完成事件，非逐 token 流
2. **Critic 仍含 `< 20 字` 硬阈值**：尽管有逃逸舱，仍可能误伤合法短回复
3. **ToolNode 无数据降噪**：直接使用 LangGraph 内置 ToolNode，无 JSON 清洗/投影层

**ℹ️ 轻微：**

4. **`_extract_final_reply` 兜底无区分度**：异常/超限/工具失败统一返回相同兜底消息
5. **记忆写入的摘要 LLM 无独立超时配置**：复用 `create_llm(request_timeout=10)`，极端慢模型可能拖长 fire-and-forget 任务

### 技术债

- **Client 层重复**: `clients/` 含 `bgm_client.py`(原始) + `client.py`/`base.py`/`sanitizers.py`(新版) + `docs/tmp/bgm_client.py`(第三个副本)。`tools/bgm_tools.py` 从 `docs/tmp/` 导入绕过了 client 层。
- **RAG v0/v1 共存**: 旧 `BangumiChunk` 系列 (deprecated) 与新 `RagEntity` 系列并存，旧表仍在测试中被引用。

### Phase 4 — 已完成 (2026-06-09)

双 Agent 架构落地：

| | Research Agent | Dialogue Agent |
|---|---|---|
| 端点 | `POST /chat` | `POST /chat/dialogue` |
| 定位 | 深度研究助手 | 快速对话（Bangumi娘人格） |
| 节点数 | 3 (reasoning, tool, critic) | 2 (reasoning, tool) |
| Max 迭代 | 10 | 3 |
| 回复长度 | 不限 | 30-80 字（闲聊）/ ≤150 字（工具查询） |
| 工具链 | search→detail→characters→comments | search → (可选 detail) |
| LLM 调用 | 2-5 次 | 1-2 次 |
| Critic | rule/llm 双模式 | 无 |
| 人格 | 中性助手 | Bangumi娘（腹黑萝莉，黑色幽默） |
| 文件位置 | `agent/research/` | `agent/dialogue/` |

### Dialogue Agent 数据流

```
POST /chat/dialogue → dialogue_app.ainvoke(initial_state)
  → dialogue_reasoning_node:
      manage_memory (两层截断)
      → classify_intent (规则/LLM, 仅首轮)
      → build_dialogue_prompt (Bangumi娘人格)
      → LLM invoke (chitchat/factual 不绑工具，其余绑工具)
      → AIMessage(tool_calls=[...])
  → route_after_dialogue_reasoning: 原生路由 —
      iterations >= 3 → END (熔断)
      AIMessage.tool_calls 非空 → tool_node → dialogue_reasoning_node
      其他 → END
```

共用层：`tools/`、`rag/`、`clients/`、`agent/llm.py`、`agent/memory.py`、`agent/classifier.py`。
