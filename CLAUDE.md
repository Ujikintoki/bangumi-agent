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
          ├─ classify_intent (LLM-only, shared via reasoning_core)
          ├─ L2 memory recall (_memory_context cached)
          ├─ build_system_prompt (BASE + intent variant + critic_feedback + style appendix)
          └─ LLM invoke
               │
     ┌─────────┼─────────┐
     │         │         │
  tool_calls  chitchat  其他无工具
     │      (快速通道)     │
     ▼         ▼         ▼
  tool_node   END     critic_node
     │              (llm/rule)
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

Key differences: Dialogue skips Critic, has tighter L2 threshold, and a last-iteration bailout that force-unbinds tools + injects a "respond NOW" instruction. Both agents always bind tools — intent classification affects only System Prompt strategy, not tool availability.

### Agent directory structure

```
agent/
├── classifier.py        # Single-stage LLM intent classification (keyword table removed 2026-07-22)
├── llm.py               # create_llm() multi-provider factory (Azure/OpenAI/DeepSeek)
├── memory.py            # L1 short memory — sliding window + tiktoken truncation + orphan cleanup
├── memory_manager.py    # L2 cross-session semantic recall + L3 deprecated methods
├── session_cache.py     # Cross-HTTP-request message cache (TTL 1h, max 1000 sessions)
├── profiles.py          # CharacterProfile + AgentProfile dataclasses (replaces old styles.py)
├── prompt_builder.py    # Unified prompt assembly — role-first, shared by both agents
├── reasoning_core.py    # Shared reasoning helpers (classify, recall, build_list, xml_guard)
├── guardrails.py        # Shared: terminal response detection, XML leak stripping, duplicate tool detection
│
├── research/            # Research Agent (deep search, Critic quality control)
│   ├── state.py         # AgentState — 10 fields (_MAX_ITERATIONS=12)
│   ├── graph.py         # 3-node topology with conditional edges
│   ├── nodes.py         # reasoning_node + critic_node (thin wrappers, delegates to reasoning_core)
│   └── prompts.py       # INTENT_PROMPTS + CRITIC_SYSTEM_PROMPT + build_system_prompt()
│
└── dialogue/            # Dialogue Agent (fast chat, no Critic, Bangumi娘 persona)
    ├── state.py         # DialogueState — 7 fields (_MAX_ITERATIONS=4)
    ├── graph.py         # 2-node topology
    ├── nodes.py         # dialogue_reasoning_node (thin wrapper, delegates to reasoning_core)
    └── prompts.py       # build_dialogue_prompt() thin wrapper
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
- **`output_style`** control: `agent/profiles.py` holds `CharacterProfile` and `AgentProfile` dataclasses. Style is prompt-only — role-first assembly, zero extra LLM call, no separate rendering.
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
| Critic | llm (default) / rule | None |
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

## Phase 5.5: Output Style Control ✅ (2026-06-17, 重构 2026-07-22)

Structured character profiles + unified prompt builder — role-first architecture with intent extension and tone profiling.

**Design decision**: The original ROADMAP planned a post-processing `render()` architecture (hexagonal ports & adapters, separate `agent/personality/` module, second LLM call for style rewriting). This was tested and rejected — the extra LLM call added latency and risked data fabrication.

The Lite approach (prompt appendix injection, `f644a56`) was the first implementation. The **2026-07-22 refactoring** (`078a803`) completed the architecture: structured dataclasses, role-first assembly order, intent taxonomy extension, and memory tone profiling.

### Architecture: Role-First Prompt Assembly

```
build_system_prompt() / build_dialogue_prompt()
  → CharacterProfile (who you are — first layer)
  → AgentProfile.capabilities (what you can do — subordinate to character)
  → tool behavior + strategy (how you use tools)
  → tool constraints + data model rules
  → continuity rules
  → intent strategy variant (debate/emotional/lookup/...)
  → memory context + tone hints
  → critic feedback (Research only)
  → expression guide + output format
  → guardrails (word limits, emoji ban, etc.)
  → last-chance instruction (Dialogue only)
```

The model handles "being a character + reasoning + styling" in a single inference pass. No separate render LLM, no diff validation, no added latency.

### File: `agent/profiles.py` (270 lines) — Canonical Source

Replaces the old `agent/styles.py`. Structured dataclasses:

| Class | Purpose | Instances |
|-------|---------|-----------|
| `CharacterProfile` | Identity, motivation, expression guide, guardrails, tool behavior | `BANGUMI_CHARACTER`, `NEUTRAL_CHARACTER`, `BANGUMI_RESEARCH_CHARACTER` |
| `AgentProfile` | Capabilities, tool strategy, output format, default character | `DIALOGUE_PROFILE`, `RESEARCH_PROFILE` |

**Agent-level character variants**: `get_character("bangumi", agent_type="research")` returns `BANGUMI_RESEARCH_CHARACTER` — same persona but **no word limits** + explicit "数据完整性和工具调用策略不变" guardrail. `get_character("bangumi", agent_type="dialogue")` returns `BANGUMI_CHARACTER` — full persona with 30-80 char limit.

### File: `agent/prompt_builder.py` (160 lines) — Unified Builder

Both agents use the same `build_system_prompt()` function with different parameters. Assembly order enforces role-first hierarchy. Shared rules (continuity, tool-calling-after-tool) live here. Agent-specific rules (data model constraint, tool dependency constraint) are passed in via parameters from `research/prompts.py`.

### Core/Style Separation (v3)

**Dialogue** (`agent/dialogue/prompts.py` → thin wrapper):
- `build_dialogue_prompt(memory_context, output_style)` delegates to `prompt_builder.build_system_prompt()`
- `DIALOGUE_CORE_PROMPT` deleted — content moved to `DIALOGUE_PROFILE` + `BANGUMI_CHARACTER`

**Research** (`agent/research/prompts.py` → thin wrapper + strategy variants):
- `build_system_prompt(intent, critic_feedback, memory_context, output_style)` delegates to `prompt_builder.build_system_prompt()`
- `BASE_SYSTEM_PROMPT` deleted — content moved to `RESEARCH_PROFILE` + `NEUTRAL_CHARACTER`
- `INTENT_PROMPTS` retained and extended with `debate` and `emotional` strategies
- `TOOL_DEPENDENCY_CONSTRAINT` and data model constraint passed as parameters to builder

### Intent Taxonomy Extension

Two new intents added to `agent/classifier.py`:

| Intent | Behavior | Strategy |
|--------|----------|----------|
| `debate` | Tools available — data backs opinion | Express opinion backed by search/discussion data; don't just rant |
| `emotional` | Tools available — empathy + data | Acknowledge emotion first, then recommend with real data if needed |

All intents have tools available (2026-07-22 refactor). Intent only affects System Prompt strategy, not tool availability.

### Memory Tone Profiling

`agent/memory_manager.py` — lightweight interaction style tracking:

- `SUMMARIZE_PROMPT_V2`: LLM extracts `tone` (casual/debate/emotional/informational) alongside summary and entities
- Stored in `intent_distribution` JSONB field (schema-free, no DB migration)
- `_format_memory_context()`: when ≥2 recent sessions share same tone, injects hint like "这位用户喜欢争论和被挑衅"

### Data Flow

```
POST /chat { output_style: "bangumi" | "neutral" | null }
  → _resolve_output_style(): explicit > AGENT_DEFAULT_STYLES (dialogue→bangumi, research→neutral)
  → state["output_style"] = resolved_style
  → reasoning_node → build_*_prompt(output_style=...)
  → get_character(output_style, agent_type=...) → CharacterProfile
  → get_agent_profile(agent_type) → AgentProfile
  → prompt_builder.build_system_prompt(profile, character, ...)
  → LLM generates styled output
  → ChatResponse { output_style: resolved_style }
```

### Key Design Properties

1. **Role-first**: Character identity is the FIRST thing the LLM sees — capabilities are subordinate
2. **Zero extra latency**: Style is prompt-only; no second LLM call
3. **No data fabrication risk**: Model sees data AND style simultaneously
4. **Agent-appropriate variants**: `BANGUMI_RESEARCH_CHARACTER` preserves data completeness; `BANGUMI_CHARACTER` enforces word limits
5. **Extensible**: New style = new CharacterProfile + register in CHARACTER_REGISTRY; new intent = keywords in classifier + strategy in INTENT_PROMPTS
6. **No DB migration**: Tone stored in existing `intent_distribution` JSONB

### Test Coverage

`test/test_prompts.py` — 46 tests (up from 7):
- `TestProfiles` (9 tests): profile integrity, field validation, registry keys, fallback behavior
- `TestPromptBuilder` (14 tests): assembly order, role-first verification, all four quadrants, memory/critic/last-chance injection
- `TestBuildDialoguePrompt` (4 tests): thin wrapper correctness
- `TestBuildResearchPrompt` (7 tests): thin wrapper + debate/emotional strategy verification
- `TestIntentPrompts` (9 tests): all intents registered, tool constraint inclusion/exclusion, data model rules
- `TestResearchContinuityRules` (3 tests): continuity rules present in assembled prompt

Full suite: 527 passed, 23 skipped (L3), 0 failed.

## Phase 6: More Tools & Community Data — Reserved

> Planned: `get_group_topics`, `web_search`, `polish_text`, community sentiment analysis. Memory system benefits from public memories (`public_memories` table already created). Not started.

## Current Known Issues (2026-07-23)

**🟡 Medium:**

1. **Streaming endpoint is node-level only**: `/chat/stream` pushes node-completion events, not token-by-token streaming
2. **`_memory_context` empty-string caching**: When `recall_for_prompt()` returns `""`, subsequent reasoning node entries re-trigger embedding calls because `""` is falsy. Low impact. Fix: use a sentinel value (~3 lines)
3. **bare title → data dump**: Dialogue agent sometimes dumps full data on bare title instead of asking what user wants. Mitigated by tool_strategy rule, but LLM compliance is probabilistic

**ℹ️ Minor:**

4. **Summary LLM has no independent timeout**
5. **`session_memories` doesn't track `agent_type`**

**✅ Resolved (2026-07-22/23):**

- Critic `< 20 chars` hard threshold → CRITIC_MODE defaults to `"llm"`
- `_NO_TOOL_INTENTS` keyword table blocking tool access → deleted; LLM always has tools
- `INTENT_RULES` 213-line keyword/regex table → deleted; LLM-only classification
- Two agent nodes duplicate ~75 lines → extracted to `reasoning_core.py`

## Technical Debt

- **RAG v0/v1 coexistence**: Deprecated `BangumiChunk` series coexists with new `RagEntity` series. DB is empty — clean removal is low-risk
- **`create_llm()` no caching**: Creates a new `ChatOpenAI` instance on every call
- **HNSW index creation fails** on 2048d vectors (pgvector limit is 2000d)

## Documentation Index

| Document | Content |
|----------|---------|
| `CLAUDE.md` | This file — architecture, conventions, current state |
| `docs/design/ROADMAP.md` | Development roadmap, phase details, fix status |
| `docs/design/phase5-memory-system-design.md` | Phase 5 full design spec |
| `docs/design/personality-rendering-layer.md` | Phase 5.5 original design (render-based, superseded by v3) |
| `docs/design/architecture-review-2026-07-22.md` | Macro architecture review — agent layer directional issues |
| `docs/memory/` | Memory system manuals (6 files) |
| `docs/ARCHITECTURE.md` | Legacy architecture doc (2026-05-29, partially outdated) |
| `README.md` | Project README (badges, quick start — partially outdated) |
| `.env.example` | Environment variable template — new developers copy to `.env` |
