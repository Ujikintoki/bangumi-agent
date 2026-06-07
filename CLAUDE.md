# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

A **Stateful AI Agent** for the [Bangumi](https://bgm.tv) ecosystem — natural-language understanding, multi-tool orchestration, and long-term memory for anime/manga/music/game discovery. Built as a FastAPI microservice with LangGraph ReAct agent, PostgreSQL + pgvector RAG, and Zhipu embedding-3.

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

### Agent topology (Phase 3 — current)

```
                        START
                          │
                          ▼
                   reasoning_node
                   ├─ manage_memory (tiktoken 滑动窗口截断)
                   ├─ classify_intent (规则优先 + LLM fallback, 6 类)
                   ├─ build_system_prompt (BASE + intent 变体 + critic_feedback)
                   └─ LLM invoke (chitchat/factual 不绑定工具)
                          │
                          ▼ (条件边: route_after_reasoning)
               ┌──────────┼──────────┐
               │          │          │
          last_tool_calls  chitchat  其他无工具
           非空            无工具
               │          │          │
               ▼          ▼          ▼
          ToolNode      END       critic_node
         (LangGraph    (快速通道)   (rule/llm 双模式)
          内置)                      │
               │                    ▼
               │            (条件边: route_after_critic)
               │             PASS/超限 → END
               │             REVISE  → reasoning_node (重试)
               │                    │
               └──────────┬─────────┘
                          ▼
                         END
```

**关键设计点：**
- `last_tool_calls` 单写者驱动路由（仅 `reasoning_node` 写入）
- chitchat 快速通道：跳过工具和 critic，直达 END
- `critic_feedback` 定向注入下一轮 System Prompt（`"<缺陷> | <建议> | <缺失>"` 格式）
- 最大 5 轮迭代熔断（graph 和 critic 双重检查）
- `error_flag` 优雅降级：置 True 时 reasoning_node 返回兜底消息

### Agent 目录结构

```
agent/
├── state.py         # AgentState TypedDict (9 字段)
├── graph.py         # build_graph() + 2 条条件边 + 快速通道
├── nodes.py         # reasoning_node, critic_node (rule/llm 双模式)
├── classifier.py    # 两阶段意图分类 (优先级规则 + LLM fallback)
├── prompts.py       # BASE + 5 个 intent 变体 + CRITIC_SYSTEM_PROMPT
├── llm.py           # create_llm() 多 Provider 工厂 (Azure/OpenAI/DeepSeek)
├── memory.py        # tiktoken 滑动窗口截断 (cl100k_base 精确计数)
│
├── research/        # 研究助手 agent（当前，计划迁移）
│   ├── state.py     # 从 agent/ 移入
│   ├── graph.py     # 从 agent/ 移入
│   ├── nodes.py     # 从 agent/ 移入
│   └── prompts.py   # 从 agent/ 移入
│
└── dialogue/        # 对话式 agent（计划新建 — 快 > 准，回复 ~100 字节）
    ├── state.py     # DialogueState（5 字段，无 critic）
    ├── graph.py     # reasoning → tool → reasoning(直接回复) → END
    ├── nodes.py     # dialogue_reasoning_node
    └── prompts.py   # 极简 prompt
```

共用层：`tools/`, `rag/`, `clients/`, `core/config.py`, `agent/llm.py`, `agent/memory.py`, `agent/classifier.py`

### Layer responsibilities

| Layer | Module | Role |
|-------|--------|------|
| Entry | `main.py` | FastAPI app, CORS, health check, POST `/chat` + `/chat/stream` |
| Config | `core/config.py` | pydantic-settings from `.env`, `@lru_cache` singleton |
| Agent | `agent/` | LangGraph StateGraph: reasoning → (条件) tool/critic/END → (条件) END/retry. 最大 5 轮强制终止 |
| Tools | `tools/bgm_tools.py` | LangChain `@tool` functions with Pydantic `args_schema`. Returns natural-language strings to the LLM |
| Client | `clients/` | `BaseClient` (httpx, retry, auth) → `BangumiClient` (business methods) → `sanitizers` (field whitelisting, type coercion) |
| RAG | `rag/` | `text_processor.py` (tiktoken sliding-window chunking) → `ingestion.py` (batch embedding + DB write) → `retriever.py` (hybrid vector + JSONB filter search) |
| Database | `database/` | SQLModel + pgvector. `engine.py` manages connection pool and DDL (HNSW + GIN trigram indexes) |
| Schemas | `schemas/tools_input.py` | Pydantic v2 input contracts for every tool — the "type contract" between LLM and tool functions |

### Data flow for a search query

```
POST /chat → agent_app.invoke(initial_state)
  → reasoning_node:
      manage_memory (token 截断) → classify_intent (规则/LLM)
      → build_system_prompt (BASE + intent 变体)
      → LLM invoke (绑定工具 if lookup/discovery/realtime)
      → AIMessage(tool_calls=[...])
  → route_after_reasoning: last_tool_calls 非空 → tool_node
  → ToolNode: 并发执行工具调用 (RAG 检索 + Bangumi API)
      → ToolMessage(content=结果)
  → critic_node: rule/llm 评估 → PASS/REVISE
  → PASS → END, REVISE → reasoning_node (注入 critic_feedback)
```

### RAG architecture — single-table polymorphism

All three entity types (Subject, Character, Person) share one `rag_entities` table with a prefixed primary key (`subject_10`, `character_5`, `person_3`). The `entity_type` column + JSONB `meta_info` distinguish entity-specific fields. Retrieval pipeline: scalar pre-filter (`entity_type = ?`) → vector cosine distance → distance threshold cutoff (0.65) → semantic bucket sort with entity-type-specific heat signals (rating_total for subjects, collects for characters/persons).

## Key conventions

- **Async-first**: all network I/O uses `async/await`. HTTP client is `httpx.AsyncClient`.
- **Error handling**: API failures return `{"_error": "..."}` dicts — never throw. Callers check `"_error" in result` and propagate gracefully. BaseClient retries on 429/502/503/TimeoutException with exponential backoff (max 3 attempts).
- **Sanitizer pattern**: pure functions that whitelist fields, coerce magic numbers to human-readable labels, hard-truncate text, and filter noise (<4 char comments, pure numbers/dates). No self, no side effects.
- **Agent state** (`agent/state.py`): TypedDict with 9 fields — `messages` (Annotated[list[BaseMessage], operator.add]), `iterations`, `critic_status` (PENDING/PASS/REVISE), `critic_feedback`, `last_tool_calls` (仅 reasoning_node 写入，驱动路由), `query_intent` (chitchat/factual/lookup/discovery/realtime/unknown), `session_id`, `user_id`, `error_flag`.
- **Token input schemas** (`schemas/tools_input.py`): every tool's parameters are defined as Pydantic BaseModel subclasses with Field descriptions written for LLM consumption.
- **`.env`** is at project root, loaded by `core/config.py`. Key variables: `DATABASE_URL`, `BANGUMI_APP_ID`, `BANGUMI_APP_SECRET`, `ZHIPU_API_KEY`, `EMBEDDING_DIMENSION` (default 2048).

## Current state and known issues

### Phase 3 — 已完成 (2026-06-06)

全部 9 个 Step 完成，375 tests 通过。LLM + 工具 + Critic + 记忆 + 端点全线贯通。

### 当前已知问题（2026-06-07 review）

**🔴 严重：**

1. **critic_feedback 在 LLM 异常时被静默丢弃** (`agent/nodes.py:121-129`)：异常 handler 返回的 dict 不含 `critic_feedback`，上一轮的反馈丢失，浪费 REVISE 轮次
2. **超大 ToolMessage 无截断保护** (`agent/memory.py`)：`manage_memory` 只截断消息列表，不截断单条内容。工具返回 50KB JSON 时单条 ToolMessage 即可爆预算

**🟡 中等：**

3. **`_MAX_ITERATIONS` 重复定义**：`graph.py:30` 和 `nodes.py:214` 各定义一次 (=5)，改一处漏一处会行为不一致
4. **prompt 注入风险** (`agent/classifier.py:231`)：`INTENT_CLASSIFIER_PROMPT.format(user_message=...)` — 用户含 `{}` 时抛 `KeyError`
5. **流式端点仅节点级**：`/chat/stream` 推送节点完成事件，非逐 token 流。LLM 慢时用户感知无改善
6. **session_id / user_id 存而未用**：标注 "Layer 2/3 预留" 但无持久化，每次 /chat 无状态

**ℹ️ 轻微：**

7. **`_extract_final_reply` 兜底无区分度**：异常/超限/工具失败统一返回相同兜底消息

### 技术债

- **Client 层重复**: `clients/` 含 `bgm_client.py`(原始) + `client.py`/`base.py`/`sanitizers.py`(新版) + `docs/tmp/bgm_client.py`(第三个副本)。`tools/bgm_tools.py` 从 `docs/tmp/` 导入绕过了 client 层。
- **RAG v0/v1 共存**: 旧 `BangumiChunk` 系列 (deprecated) 与新 `RagEntity` 系列并存，旧表仍在测试中被引用。

### 未来方向：双 Agent 架构

当前"研究助手" agent 将迁移到 `agent/research/`，另写 `agent/dialogue/` 对话式 agent：

- **Research**: 深度搜索、Critic 质量自省、多轮修正 — 准确 > 速度
- **Dialogue**: reasoning → tool → 直接回复，无 Critic — 速度 > 准确，回复 ~100 字节

共用 `tools/`、`rag/`、`clients/`、`agent/llm.py`、`agent/memory.py`、`agent/classifier.py`。
