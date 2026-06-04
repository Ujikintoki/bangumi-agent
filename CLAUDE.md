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

The system follows a **layered architecture** with strict separation of concerns:

```
FastAPI entry (main.py)
  └─ LangGraph ReAct agent (agent/)
       ├─ reasoning_node → tool_node → critic_node
       └─ tools (tools/bgm_tools.py)
            ├─ BangumiClient (clients/) → p1 API (next.bgm.tv/p1)
            └─ RAG retriever (rag/retriever.py) → pgvector
```

### Layer responsibilities

| Layer | Module | Role |
|-------|--------|------|
| Entry | `main.py` | FastAPI app, CORS, health check |
| Config | `core/config.py` | pydantic-settings from `.env`, `@lru_cache` singleton |
| Agent | `agent/` | LangGraph StateGraph: reasoning → (conditional) tool → critic → retry/END. Max 3 iterations, then forced termination |
| Tools | `tools/bgm_tools.py` | LangChain `@tool` functions with Pydantic `args_schema`. Returns natural-language strings to the LLM |
| Client | `clients/` | `BaseClient` (httpx, retry, auth) → `BangumiClient` (business methods) → `sanitizers` (field whitelisting, type coercion) |
| RAG | `rag/` | `text_processor.py` (tiktoken sliding-window chunking) → `ingestion.py` (batch embedding + DB write) → `retriever.py` (hybrid vector + JSONB filter search) |
| Database | `database/` | SQLModel + pgvector. `engine.py` manages connection pool and DDL (HNSW + GIN trigram indexes) |
| Schemas | `schemas/tools_input.py` | Pydantic v2 input contracts for every tool — the "type contract" between LLM and tool functions |

### Data flow for a search query

```
User query → reasoning_node (detects tool intent) → tool_node (calls tool function)
  → BangumiClient.search() → BaseClient._post() → p1 API
  → sanitizers.sanitize_search_subjects() → field-whitelisted dict
  → tool returns natural-language string → critic_node (PASS/REVISE)
  → response to user (or retry)
```

### RAG architecture — single-table polymorphism

All three entity types (Subject, Character, Person) share one `rag_entities` table with a prefixed primary key (`subject_10`, `character_5`, `person_3`). The `entity_type` column + JSONB `meta_info` distinguish entity-specific fields. Retrieval pipeline: scalar pre-filter (`entity_type = ?`) → vector cosine distance → distance threshold cutoff (0.65) → semantic bucket sort with entity-type-specific heat signals (rating_total for subjects, collects for characters/persons).

## Key conventions

- **Async-first**: all network I/O uses `async/await`. HTTP client is `httpx.AsyncClient`.
- **Error handling**: API failures return `{"_error": "..."}` dicts — never throw. Callers check `"_error" in result` and propagate gracefully. BaseClient retries on 429/502/503/TimeoutException with exponential backoff (max 3 attempts).
- **Sanitizer pattern**: pure functions that whitelist fields, coerce magic numbers to human-readable labels, hard-truncate text, and filter noise (<4 char comments, pure numbers/dates). No self, no side effects.
- **Agent state** (`agent/state.py`): TypedDict with `messages` (Annotated[list, operator.add] for append semantics), `iterations`, `critic_status` (PENDING/PASS/REVISE), `needs_tool`, `error_flag`.
- **Token input schemas** (`schemas/tools_input.py`): every tool's parameters are defined as Pydantic BaseModel subclasses with Field descriptions written for LLM consumption.
- **`.env`** is at project root, loaded by `core/config.py`. Key variables: `DATABASE_URL`, `BANGUMI_APP_ID`, `BANGUMI_APP_SECRET`, `ZHIPU_API_KEY`, `EMBEDDING_DIMENSION` (default 2048).

## Current state and known issues

- **Agent nodes are stubs**: `reasoning_node` uses hardcoded keyword matching instead of real LLM calls. `critic_node` uses `iterations < 2` as a dummy quality gate. The graph topology and conditional routing are correct; only the node internals need real LLM integration.
- **Client layer duplication**: `clients/` contains both the original `bgm_client.py` (tracked) and newer `client.py` + `base.py` + `sanitizers.py` (untracked). `tools/bgm_tools.py` imports from `docs/tmp/bgm_client.py` (a third copy) and also has inline HTTP logic that bypasses the client layer entirely.
- **RAG v0/v1 coexistence**: `BangumiChunk` / `BangumiRetriever` / `BangumiIngestor` (old, `bangumi_chunks` table) coexist with `RagEntity` / `RagEntityRetriever` / `RagEntityIngestor` (new, `rag_entities` table). The old code is marked `[DEPRECATED]` but still referenced in tests (`conftest.py` cleans up `bangumi_chunks`).
