# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

A **Stateful AI Agent** for the [Bangumi](https://bgm.tv) ecosystem — natural-language understanding, multi-tool orchestration, and long-term memory for anime/manga/music/game discovery. Built as a FastAPI microservice with LangGraph ReAct agent, PostgreSQL + pgvector RAG, and Zhipu embedding-3.

**Current phase**: Phase 5 memory system (L1 + L2 active, L3 deprecated). Phase 5.5 Lite output_style four-quadrant control live. Two agents (Research + Dialogue) sharing a unified `POST /chat` endpoint.

## Commands

```bash
# Run the app
uvicorn main:app --reload --port 8000

# Run all tests (require PostgreSQL with pgvector running locally)
pytest test/ -v

# Run a single test file
pytest test/test_memory_manager.py -v

# Start PostgreSQL + pgvector (Docker, required for tests, RAG, and memory)
docker run -d --name bangumi-pg \
  -e POSTGRES_USER=myuser -e POSTGRES_PASSWORD=mypassword \
  -e POSTGRES_DB=bangumidb -p 5432:5432 pgvector/pgvector:pg16
```

## Architecture

### Agent topology

Two agents share a unified `POST /chat` endpoint, routed by `agent_type` in the request body (default: `"dialogue"`).

**Research Agent** (3 nodes, `_MAX_ITERATIONS=12`):
```
START → reasoning_node ←──────────────────┐
          │                                 │
          ├─ manage_memory (L1 sliding window)
          ├─ classify_intent (rule-first + LLM fallback)
          ├─ L2 memory recall (_memory_context cached)
          ├─ build_system_prompt (BASE + intent variant + critic_feedback + style appendix)
          └─ LLM invoke
               │
     ┌─────────┼─────────┐
     │         │         │
  tool_calls  chitchat  其他无工具
     │         │         │
     ▼         ▼         ▼
  tool_node   END     critic_node
     │      (fast)    (rule/llm)
     │         │         │
     │         │    PASS/over-limit → END
     │         │    REVISE → reasoning_node
     └─────────┘
```

**Dialogue Agent** (2 nodes, `_MAX_ITERATIONS=4`):
```
START → dialogue_reasoning_node ←─────────┐
          │                                 │
          ├─ manage_memory (L1)
          ├─ classify_intent
          ├─ L2 memory recall (tighter threshold 0.35)
          ├─ build_dialogue_prompt (CORE + style appendix)
          ├─ last-chance bailout (iter≥3 → unbind tools + inject emergency instruction)
          └─ LLM invoke
               │
         ┌─────┴─────┐
         │           │
    tool_calls    无工具/熔断
         │           │
         ▼           ▼
     tool_node      END
         │
         └──────────┘
```

Key differences: Dialogue skips Critic, has tighter L2 threshold, and a last-iteration bailout that force-unbinds tools + injects a "respond NOW" instruction to prevent silent circuit-breaker failures.

### Agent directory structure

```
agent/
├── classifier.py      # Two-stage intent classification (rule priority + LLM fallback)
├── llm.py             # create_llm() multi-provider factory (Azure/OpenAI/DeepSeek)
├── memory.py          # L1 short memory — sliding window + tiktoken truncation + orphan cleanup
├── memory_manager.py  # L2 cross-session semantic recall + L3 deprecated methods
├── session_cache.py   # Cross-HTTP-request message cache (TTL 1h, max 1000 sessions)
├── styles.py          # Output style registry — neutral/bangumi appendices for both agents
├── guardrails.py      # Shared: terminal response detection, XML leak stripping, duplicate tool detection
│
├── research/          # Research Agent (deep search, Critic quality control)
│   ├── state.py       # AgentState — 10 fields (_MAX_ITERATIONS=12)
│   ├── graph.py       # 3-node topology with conditional edges
│   ├── nodes.py       # reasoning_node + critic_node
│   └── prompts.py     # BASE + 5 intent variants + CRITIC_SYSTEM_PROMPT
│
└── dialogue/          # Dialogue Agent (fast chat, no Critic, Bangumi娘 persona)
    ├── state.py       # DialogueState — 7 fields (_MAX_ITERATIONS=4)
    ├── graph.py       # 2-node topology
    ├── nodes.py       # dialogue_reasoning_node + last-chance bailout
    └── prompts.py     # DIALOGUE_CORE_PROMPT (persona-free) + build_dialogue_prompt()
```

Shared layers: `tools/`, `rag/`, `clients/`, `core/config.py`, `database/`

### Layer responsibilities

| Layer | Module | Role |
|-------|--------|------|
| Entry | `main.py` | FastAPI app, CORS, `POST /chat` (unified, `agent_type` routing), `/chat/stream` (SSE), `_resolve_output_style()` |
| Config | `core/config.py` | pydantic-settings from `.env`, `@lru_cache` singleton |
| Agent | `agent/research/` + `agent/dialogue/` | LangGraph StateGraph: reasoning → tool → (critic for research) → END. 12/4 max iterations |
| Tools | `tools/bgm_tools.py` | 14 LangChain `@tool` functions with Pydantic `args_schema`. Returns natural-language strings |
| Client | `clients/` | `BaseClient` (httpx, retry, auth) → `BangumiClient` (business methods) → `sanitizers` (field whitelisting, type coercion) |
| RAG | `rag/` | `text_processor.py` → `ingestion.py` → `retriever.py` (hybrid vector + JSONB filter). DB currently empty |
| Database | `database/` | SQLModel + pgvector. `engine.py` (connection pool, DDL), `models.py` (RagEntity), `memory_tables.py` (SessionMemory, UserProfile, PublicMemory) |
| Schemas | `schemas/tools_input.py` | Pydantic v2 input contracts — the type contract between LLM and tool functions |

### Request/Response model

`POST /chat` accepts `ChatRequest`:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `message` | `str` | (required) | User message |
| `agent_type` | `"dialogue" \| "research"` | `"dialogue"` | Which agent to use |
| `output_style` | `"neutral" \| "bangumi" \| None` | `None` | `None` = agent default (dialogue→bangumi, research→neutral) |
| `session_id` | `str` | `"default"` | Session ID for L1 session cache + L2 memory |
| `user_id` | `str` | `"anonymous"` | User ID for L2 cross-session recall |

`ChatResponse` returns: `reply`, `iterations`, `tools_used`, `query_intent`, `output_style`.

## Key conventions

- **Async-first**: all network I/O uses `async/await`. HTTP client is `httpx.AsyncClient`.
- **Error handling**: API failures return `{"_error": "..."}` dicts — never throw. BaseClient retries on 429/502/503/TimeoutException with exponential backoff (max 3 attempts).
- **Sanitizer pattern**: pure functions that whitelist fields, coerce magic numbers to human-readable labels, hard-truncate text, and filter noise. No self, no side effects.
- **AgentState** (`agent/research/state.py`): TypedDict with 10 fields — `messages`, `iterations`, `critic_status`, `critic_feedback`, `query_intent`, `session_id`, `user_id`, `error_flag`, `_memory_context`, `output_style`. `_MAX_ITERATIONS = 12`.
- **DialogueState** (`agent/dialogue/state.py`): TypedDict with 7 fields — same minus `critic_status`, `critic_feedback`, `error_flag`. `_MAX_ITERATIONS = 4`.
- **Routing** is driven by native message properties (`messages[-1].tool_calls`), not redundant state fields.
- **`output_style`** control: `agent/styles.py` holds two dicts (`STYLE_APPENDICES` for dialogue, `STYLE_APPENDICES_RESEARCH` for research). `"neutral"` maps to `""` (zero token overhead). Style appendix is appended to System Prompt — no separate rendering LLM call.
- **`.env`** at project root, loaded by `core/config.py`. Key variables: `DATABASE_URL`, `BANGUMI_APP_ID`, `BANGUMI_APP_SECRET`, `ZHIPU_API_KEY`, `DEEPSEEK_API_KEY`, `EMBEDDING_DIMENSION` (default 2048).

## Phase 4: Dual Agent Architecture ✅ (2026-06-09)

| | Research Agent | Dialogue Agent |
|---|---|---|
| Endpoint | `POST /chat` (`agent_type="research"`) | `POST /chat` (`agent_type="dialogue"`, default) |
| Purpose | Deep research assistant | Fast chat (Bangumi娘 persona) |
| Topology | 3 nodes (reasoning, tool, critic) | 2 nodes (reasoning, tool) |
| Max iterations | 12 | 4 |
| Response length | Unlimited | 30-80 chars (chat) / ≤150 chars (with tools) |
| Tool chain depth | search → detail → characters → comments | search → (optional detail) |
| LLM calls | 2-8 | 1-3 |
| Critic | rule/llm dual-mode | None |
| Default persona | Neutral (bangumi optional) | Bangumi娘 (neutral optional) |
| Last-iteration protection | Critic REVISE + circuit breaker | Force-unbind tools + emergency instruction |
| Files | `agent/research/` | `agent/dialogue/` |

### Research Agent Data Flow

```
POST /chat (agent_type="research")
  → reasoning_node:
      manage_memory (L1) → classify_intent (round 1 only)
      → L2 memory recall (_memory_context cached)
      → build_system_prompt (BASE + memory + intent + critic_feedback + style)
      → LLM invoke (chitchat/factual: no tools; others: 14 tools bound)
      → AIMessage
  → route_after_reasoning:
      tool_calls → tool_node → reasoning_node (fixed edge)
      chitchat → END (fast path)
      other → critic_node → PASS/over-limit → END, REVISE → reasoning_node
```

### Dialogue Agent Data Flow

```
POST /chat (agent_type="dialogue")
  → dialogue_reasoning_node:
      manage_memory (L1) → classify_intent (round 1 only)
      → L2 memory recall (threshold 0.35, _memory_context cached)
      → build_dialogue_prompt (CORE + style appendix)
      → last-chance check (iter≥3 → unbind tools + inject emergency instruction)
      → LLM invoke
      → AIMessage
  → route_after_dialogue_reasoning:
      iter ≥ 4 → END (circuit breaker)
      tool_calls → tool_node → dialogue_reasoning_node
      other → END
```

## Phase 5: Memory System ✅ (2026-07-21)

L1 + L2 active. L3 user profile deprecated (2026-07-20) — code preserved, call sites commented out, DB tables intact.

### L1: Short Memory — Sliding Window

`agent/memory.py` (380 lines). Called at the start of every reasoning_node entry.

1. **Single-message truncation**: ToolMessage > 1500 tokens → truncate content (don't drop entirely)
2. **Token budget check**: tiktoken `cl100k_base` exact encoding (NOT `len//4` estimation — Chinese + JSON underestimates by 30-50%)
3. **Sliding window**: SystemMessage always preserved. Old messages dropped from head. Tail-first traversal.
4. **Orphan cleanup**: `_remove_orphaned_tool_messages()` — discards ToolMessages whose paired AIMessage was trimmed (prevents DeepSeek 400 BadRequest)

Token budgets: Research 8000, Dialogue 4000.

### L2: Cross-Session Semantic Memory

`agent/memory_manager.py` (1015 lines) + `agent/session_cache.py` (194 lines) + `database/memory_tables.py`.

**Write path** (fire-and-forget, `asyncio.create_task`, 15s hard timeout):

```
Conversation (Human + AI, skip Tool/System)
  → truncate to 3000 tokens
  → DeepSeek generates ~200-char JSON summary {"summary": "...", "entities": [...]}
  → Zhipu embedding-3 vectorize (2048d)
  → UPSERT session_memories (one row per user+session, prevents vector space pollution)
  → ~~_update_user_profile()~~ [L3 deprecated, skipped]
```

**Recall path** (dual-channel + time decay):

```
User query → Zhipu embedding-3 → 2048d vector
  │
  ├─ Channel 1 (semantic): pgvector cosine_distance ≤ threshold
  │    Score: combined_score = (1 - distance) × 0.5^(days/14)
  │
  └─ Channel 2 (recency fallback): triggered when semantic hits < TOP_K
       Fetch recent sessions by created_at DESC
       → Anchor filter: distance ≤ 0.60 (minimum semantic anchor)
       → Same time-decay scoring

→ Sort by combined_score DESC → top-5 → format → inject into System Prompt
  "## 用户历史\n- [3 days ago] User asked about EVA ratings…"
```

**Agent differentiation**:

| | Research | Dialogue |
|---|---|---|
| Injection budget | 700 tokens | 300 tokens |
| Semantic threshold | cos ≤ 0.50 | cos ≤ 0.35 |
| Skipped intents | chitchat, factual | chitchat, factual |
| Last-iteration protection | Critic REVISE | Force-unbind tools + emergency instruction |

**Graceful degradation** (7 failure points, all handled):
embedding API timeout → recency-only ranking; DB error → scored=[]; summary LLM failure → `final_reply[:200]`; INSERT failure → skip silently; `MEMORY_ENABLED=False` → no-op; `user_id="anonymous"` → no-op.

### L3: User Profile — Deprecated (2026-07-20)

Profile inference (`_update_genres` / `_update_affinities` / `_format_profile_summary`) had negligible value for an entertainment-focused agent — anime discussion preferences don't guide future answers, and L2 semantic recall already provides cross-session continuity. All L3 methods preserved with `[L3 deprecated]` docstrings. DB tables not dropped. Can be reactivated by uncommenting call sites.

### Session Cache

`session_cache.py` — bridges HTTP statelessness for L1. Same `session_id` across multiple `POST /chat` calls: prior messages injected into `state["messages"]` from in-memory cache so L1 sliding window has data to manage. In-memory only, TTL 1h, max 1000 sessions. SystemMessages excluded (rebuilt every round with fresh L2 memory + critic feedback).

### Memory Configuration

| Config key | Value | Notes |
|---|---|---|
| `MEMORY_ENABLED` | `True` | Master kill switch |
| `MEMORY_RECALL_TOP_K` | `5` | Max sessions to recall |
| `MEMORY_RECALL_THRESHOLD` | `0.5` | Research semantic threshold |
| `MEMORY_DIALOGUE_RECALL_THRESHOLD` | `0.35` | Dialogue semantic threshold (tighter) |
| `MEMORY_TIME_DECAY_HALF_LIFE_DAYS` | `14` | Time decay half-life |
| `MEMORY_RECENCY_FALLBACK_THRESHOLD` | `0.60` | Recency fallback anchor |
| `MEMORY_MAX_INJECT_TOKENS` | `700` | Research L2 injection budget |
| `MEMORY_DIALOGUE_MAX_INJECT_TOKENS` | `300` | Dialogue L2 injection budget |
| `MEMORY_MIN_SESSIONS_FOR_PROFILE` | `5` | [L3 deprecated] |

### Memory Tests

`test/test_memory.py` (L1) + `test/test_memory_manager.py` (L2, 23 L3 tests skipped) + `test/test_phase5_l1.py` (token budgets). Total: ~62 passed, 23 skipped.

## Phase 5.5: Output Style Control ✅ (2026-06-17)

Prompt appendix injection — zero extra LLM calls, four-quadrant `output_style` control.

**Design decision**: The original ROADMAP planned a post-processing `render()` architecture (hexagonal ports & adapters, separate `agent/personality/` module, second LLM call for style rewriting). This was tested and rejected — the extra LLM call added latency and risked data fabrication. The Lite approach was adopted as the **canonical implementation**.

### Architecture: Prompt Appendix Injection

```
build_system_prompt() / build_dialogue_prompt()
  → BASE/CORE prompt (capabilities + strategy, no persona)
  → intent variant
  → L2 memory context
  → style appendix (from agent/styles.py, "" when neutral)
  → LLM generates styled output directly — one call, no post-processing
```

The model handles "reasoning + styling" in a single inference pass. No separate render LLM, no diff validation needed, no added latency.

### File: `agent/styles.py` (99 lines)

Two independent dicts — Dialogue and Research use different appendix strings for the same `"bangumi"` semantic:

| Dict | Used by | `"neutral"` | `"bangumi"` |
|------|---------|-------------|-------------|
| `STYLE_APPENDICES` | Dialogue | `""` (zero tokens) | Full persona: 腹黑萝莉, 30-80 char limit, 150 char max with tools |
| `STYLE_APPENDICES_RESEARCH` | Research | `""` (zero tokens) | Soft version: same persona, **no word limits**, emphasizes data completeness |

Dialogue Bangumi appendix has hard constraints (30-80 chars chat, ≤150 chars with tools). Research Bangumi appendix is a soft version — same 腹黑 tone but explicitly instructs "数据完整性和工具调用策略不变，不要因为风格要求而缩减数据或跳过工具调用."

### Core/Style Separation

**Dialogue** (`agent/dialogue/prompts.py`):
- `DIALOGUE_CORE_PROMPT` — capabilities, tool strategy, output format, continuity rules. **Persona-free** (no "Bangumi娘", no "腹黑萝莉").
- `BANGUMI_STYLE_APPENDIX` in `agent/styles.py` — persona, tone, word limits.
- `build_dialogue_prompt(output_style=)` assembles: CORE → style appendix → memory.

**Research** (`agent/research/prompts.py`):
- `BASE_SYSTEM_PROMPT` — capabilities, data model constraints, continuity rules, output format. Contains "回答风格：简洁、具体、可操作" (not fully stripped — see known issues).
- `BANGUMI_STYLE_RESEARCH_APPENDIX` in `agent/styles.py` — soft persona, no word limits.
- `build_system_prompt(output_style=)` assembles: BASE → memory → intent variant → critic feedback → style appendix.

### Data Flow

```
POST /chat { output_style: "bangumi" | "neutral" | null }
  → _resolve_output_style(): explicit > AGENT_DEFAULT_STYLES (dialogue→bangumi, research→neutral)
  → state["output_style"] = resolved_style
  → reasoning_node → build_*_prompt(output_style=...) → style appendix injected
  → LLM generates styled output
  → ChatResponse { output_style: resolved_style }
```

### State Fields

Both `AgentState` (Research) and `DialogueState` have `output_style: str` — set by `main.py` at graph entry, read by prompt builders, returned in response.

### Key Design Properties

1. **Zero extra latency**: Style is prompt-only; no second LLM call. Compare: original `render()` plan would add ~500ms per response.
2. **No data fabrication risk**: The model sees raw data AND style instructions simultaneously — it styles while reasoning, rather than rewriting after the fact. No need for diff validation.
3. **"neutral" = zero token overhead**: Empty string in both dicts; `build_*_prompt()` skips empty appendices.
4. **Agent-appropriate differentiation**: Dialogue gets tight word limits; Research gets "soft" persona that preserves data completeness.
5. **Extensible**: Adding a new style requires: (a) write appendix string, (b) register in the appropriate dict. Two keys, not a new module.

### Test Coverage

`test/test_prompts.py` — 7 tests covering all four quadrants:
- `test_research_neutral_excludes_style` — no "腹黑"/"吐槽" in output
- `test_research_bangumi_includes_style` — has persona, no word limits, has data integrity note
- `test_dialogue_neutral_excludes_persona` — no persona, has core capabilities
- `test_dialogue_bangumi_includes_persona` — has persona + word limits
- `test_dialogue_core_prompt_has_no_persona` — CORE is persona-free
- `test_style_registry_keys` — both dicts have "neutral"→"" and "bangumi"→non-empty

### Known Deviations from Ideal

1. **`BASE_SYSTEM_PROMPT` still has "回答风格：简洁、具体、可操作"** — a style instruction in the neutral base prompt. Minor; doesn't break functionality but means Research neutral isn't perfectly style-free.
2. **`DIALOGUE_CORE_PROMPT` has "你是吐槽役，不是论文写手"** — a minor persona leak in the otherwise-persona-free CORE prompt.
3. **`/chat/stream` doesn't report `output_style`** in SSE events. The style IS resolved and passed to the graph, but streaming clients can't see which style is active.
4. **Original design doc** (`docs/design/personality-rendering-layer.md`, 347 lines) describes the rejected hexagonal architecture. Kept for historical reference; ROADMAP.md is the authoritative source.

## Phase 6: More Tools & Community Data — Reserved

> Planned: `get_group_topics`, `web_search`, `polish_text`, community sentiment analysis. Memory system benefits from public memories (`public_memories` table already created). Not started.

## Current Known Issues (2026-07-21)

**🟡 Medium:**

1. **Streaming endpoint is node-level only**: `/chat/stream` pushes node-completion events, not token-by-token streaming
2. **Critic `< 20 chars` hard threshold**: Despite escape hatch, may still reject legitimate short responses
3. **`_memory_context` empty-string caching**: When `recall_for_prompt()` returns `""`, subsequent reasoning node entries re-trigger embedding calls because `""` is falsy. Low impact (~2-3 extra calls/request for new users). Fix: use a sentinel value (~3 lines)

**ℹ️ Minor:**

4. **Summary LLM has no independent timeout**: Reuses `create_llm(request_timeout=10)`. Very slow models could extend fire-and-forget task duration
5. **`session_memories` doesn't track `agent_type`**: Recall can't distinguish dialogue vs research sessions

## Technical Debt

- **RAG v0/v1 coexistence**: Deprecated `BangumiChunk` series coexists with new `RagEntity` series. Old tables still referenced in tests. DB is empty — clean removal is low-risk
- **`create_llm()` no caching**: Creates a new `ChatOpenAI` instance on every call. Minor overhead but listed as P3-2 in ROADMAP
- **HNSW index creation fails** on 2048d vectors (pgvector limit is 2000d). Logged as WARNING, falls through gracefully. Consider reducing embedding dimension or using IVFFlat

## Documentation Index

| Document | Content |
|----------|---------|
| `CLAUDE.md` | This file — architecture, conventions, current state |
| `docs/design/ROADMAP.md` | Development roadmap, phase details, fix status |
| `docs/design/phase5-memory-system-design.md` | Phase 5 full design spec (1194 lines) |
| `docs/design/personality-rendering-layer.md` | Phase 5.5 original design (render-based approach, not the Lite implementation) |
| `docs/memory/` | Memory system manuals (6 files: architecture, implementation, config, testing, debugging) |
| `docs/ARCHITECTURE.md` | Legacy architecture doc (2026-05-29, partially outdated) |
| `README.md` | Project README (badges, quick start — partially outdated) |
