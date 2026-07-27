# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

**Bangumi 的 AI 看板娘** — 一个住在 Bangumi 站内的、有性格的二次元损友。她可以查站内数据，但她存在的理由不是查数据——是陪你聊动画。

技术栈：FastAPI + LangGraph ReAct agent + PostgreSQL + pgvector RAG + Zhipu embedding-3 + DeepSeek function-calling。

**产品定位**：Companion Agent（知识型损友），不是 Tool Agent（搜索引擎），不是 Research Agent（Perplexity 式深度分析）。在 Agent 光谱上卡在 "ChatGPT 通用助手" 和 "Character.AI 角色扮演" 之间——有真实数据支撑的聊天角色。

**当前实现阶段**：Phase 6.5 — Render Layer 解耦。Phase 6 Companion Agent 单一体架构已落地，Render 层将"准确回答"和"聊天风格"分离。

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

## Architecture（目标蓝图 — Phase 6）

### 产品心智模型

```
用户看到一个聊天窗口
用户感知到一个 AI 角色（Bangumi 看板娘 / 二次元损友）

内部实现：
  ┌──────────────────────────────────────┐
  │         Companion Agent              │
  │         （默认模式，所有对话走这里）    │
  │                                      │
  │  轻量 ReAct（1-5轮，无Critic）        │
  │  人格：损友吐槽 / 中立助手            │
  │  工具：1-2 轮够用就停                 │
  │                                      │
  │  depth="deep" 或用户说"深度分析"      │
  │    → Research Skill 激活              │
  │    → 保留 Critic + 深度链式           │
  │    → 8-12 轮，search→detail→characters│
  └──────────────────────────────────────┘
```

`agent_type` 参数 deprecated，替换为 `depth` 参数（`"auto"` / `"quick"` / `"deep"`）。对终端用户不可见——他们只看到一个 AI。

### Agent 拓扑（Phase 6.5 — 含 Render 层）

```
START → reasoning_node ←──────────────────┐
          │                                 │
          ├─ classify_intent（仅首轮）       │
          ├─ L2 memory recall（仅首轮）      │
          ├─ build_system_prompt（8 层）     │
          └─ LLM invoke（始终绑定工具）      │
               │                            │
     ┌─────────┼─────────┐                  │
     │         │         │                  │
  tool_calls  chitchat  depth!="deep"      │
     │      (快速通道)  + 有工具调用          │
     │         │         │                  │
     ▼         ▼         ▼                  │
  tool_node   END    render_node            │
     │                  │                   │
     │                  ▼                   │
     │                END                   │
     │                                       │
     │    depth=="deep" + 无工具 + 非闲聊     │
     │         │                             │
     │         ▼                             │
     │    critic_node                        │
     │    (PASS+工具→render→END,             │
     │     PASS→END, REVISE→reasoning)       │
     │                                       │
     └───────────────────────────────────────┘
```

**关键参数**：

| 模式 | `_MAX_ITERATIONS` | Critic | Render | 工具策略 |
|------|-------------------|--------|--------|---------|
| quick | 3 | 无 | 有工具时触发 | 1 轮够用就停 |
| auto（默认） | 5 | 无 | 有工具时触发 | 1-2 轮 |
| deep（Research Skill） | 12 | 有（LLM Critic） | 有工具时触发 | 深度链式 search→detail→characters |

### Agent 目录结构

```
agent/
├── state.py             # 统一 AgentState（含 depth 字段）
├── graph.py             # 统一 StateGraph（含 render_node 路由）
├── nodes.py             # reasoning_node + critic_node
├── profiles.py          # BANGUMI_CHARACTER + NEUTRAL_CHARACTER
├── prompt_builder.py    # 统一 prompt 组装（8 层，无 data_guide）
├── prompts.py           # Companion 浅层 intent 策略
├── render.py            # Render 层——工具回复的风格转换（Phase 6.5）
├── classifier.py        # 意图分类 + 深度信号检测
├── memory.py            # L1 短记忆
├── memory_manager.py    # L2 跨会话语义记忆
├── session_cache.py     # 跨 HTTP 请求缓存
├── reasoning_core.py    # 共享辅助函数
├── guardrails.py        # 终端检测 / XML 泄漏 / 重复调用
│
└── research/            # Research Skill（仅 depth="deep" 激活）
    └── prompts.py       # 深度 INTENT_PROMPTS + CRITIC_SYSTEM_PROMPT
```

**删除**：`agent/dialogue/` 全目录（已合并到根级 `agent/`）。

### Layer responsibilities

| Layer | Module | Role |
|-------|--------|------|
| Entry | `main.py` | FastAPI app, `POST /chat`（depth 参数）, `/chat/stream`（SSE） |
| Config | `core/config.py` | pydantic-settings from `.env`, `@lru_cache` singleton |
| Agent | `agent/graph.py` + `agent/nodes.py` | 单一 StateGraph：reasoning → tool → (条件 Critic) → (条件 Render) → END |
| Render | `agent/render.py` | 工具回复风格转换——仅工具调用后触发，按 depth 分档字数限制（Phase 6.5） |
| Tools | `tools/bgm_tools.py` | 14 LangChain `@tool` 函数，返回结构化 dict |
| Client | `clients/` | `BaseClient` → `BangumiClient` → `sanitizers`（A/B/C/D 字段方法论） |
| RAG | `rag/` | `text_processor.py` → `ingestion.py` → `retriever.py` |
| Database | `database/` | SQLModel + pgvector |
| Schemas | `schemas/tools_input.py` | Pydantic v2 工具输入 schema |

### Request/Response model（目标）

`POST /chat` accepts `ChatRequest`:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `message` | `str` | (required) | User message |
| `depth` | `"auto" \| "quick" \| "deep"` | `"auto"` | 深度控制。`"auto"`=LLM 自行判断，`"quick"`=强制浅层 1-3 轮，`"deep"`=激活 Research Skill |
| `output_style` | `"neutral" \| "bangumi"` | `"bangumi"` | 角色人格（统一默认 Bangumi娘） |
| `session_id` | `str` | `"default"` | Session ID |
| `user_id` | `str` | `"anonymous"` | User ID |

`agent_type` 保留但标记 deprecated，映射规则：`"dialogue"` → `depth="quick"`, `"research"` → `depth="deep"`。

`ChatResponse` returns: `reply`, `iterations`, `tools_used`, `query_intent`, `output_style`, `depth`.

## Phase 6: Companion Agent 架构重构 ✅（2026-07-25/26）

将 Phase 5 的双 Agent 架构（Dialogue + Research，各走各的 graph）合并为单一 Companion Agent，Research 能力降级为 opt-in Skill。

### 已完成

**Phase 6 Step 1 — 翻转"数据完整性优先"→"对话优先"** (`4fed77f`):

Profiles 改动：
- `BANGUMI_RESEARCH_CHARACTER`: guardrail #5 删除（"数据完整性和工具调用策略不变"），tool_behavior "数据完整性优先"→"数据服务于观点"
- `NEUTRAL_CHARACTER`: tool_behavior "数据完整性优先"→"准确但不冗余"
- `RESEARCH_PROFILE`: tool_strategy "深度链式调用 2-3步封顶"→"回答用户问题即可，1-2轮足够"
- `BANGUMI_CHARACTER`: tool_behavior 加"如果一句话能说清楚，不要用三句话"

**Phase 6 Step 2 — Companion Agent 单一体架构** (`e7df803`):

1. ✅ 合并两个 Graph → 单一 `agent/graph.py`，depth 条件启用 Critic
2. ✅ 合并两个 State → 统一 `AgentState`（含 `depth` 字段）
3. ✅ 合并两个 reasoning node → `agent/nodes.py`
4. ✅ 重写 `BANGUMI_CHARACTER` 为 Companion 损友人格
5. ✅ 删除 `BANGUMI_RESEARCH_CHARACTER`
6. ✅ Prompt 14→8 层，expression_guide 从 layer 12 提到 layer 2
7. ✅ intent 策略双版本：Companion 浅层版 + deep 深度版
8. ✅ `main.py`：`depth` 参数替换 `agent_type`
9. ✅ Critic 条件路由（仅 depth=="deep"）
10. ✅ 删除 `agent/dialogue/` 全目录

## Phase 6.5: Render Layer（进行中 — 2026-07-27, `3cc3ecc`）

### 问题

Phase 6 统一架构后，agent 仍存在"助手骨架"——工具返回 dict → LLM "翻译成自然语言报告给用户" → "回答→罗列数据→请求下一个问题"的死板结构。prompt 层面无法根除，因为这是 ReAct + function-calling 的固有倾向。

### 方案

新增 **render_node**：在 graph 输出前将 agent 回复改写为角色聊天风格。解耦"准确回答"和"聊天风格"。

```
reasoning_node（专注准确 + 工具策略）
    → render_node（专注风格：损友吐槽 / 中性助手）
    → END
```

仅**走了工具调用**的回复触发 render。纯闲聊直接走快速通道。

### 架构

| 组件 | 文件 | 职责 |
|------|------|------|
| Render prompt | `agent/render.py` `build_render_prompt()` | ~380 chars 极简 prompt：身份 + 3 条数据呈现规则 + 硬约束 |
| Render node | `agent/render.py` `render_node()` | 提取最后 AIMessage → 调用 LLM（temperature=0.4）→ 追加渲染回复 |
| 路由 | `agent/graph.py` | 五级路由：tool_calls → chitchat → critic → **render** → END |

### 关键设计

1. **Render prompt 极简**——纯人格 + 任务，不含 `_DATA_INTERPRETATION`。参照 `docs/tmp/UserScriptAi.js` 油猴脚本的设计哲学
2. **按 depth 分档字数**：quick=120, auto=200, deep=350 字
3. **expression_guide 与 _RENDER_STYLE 职责分离**：
   - `expression_guide`（profiles.py）→ 所有回复的通用语气、节奏
   - `_RENDER_STYLE`（render.py）→ 仅数据呈现（评分怎么带、数据怎么融）
   - 两套规则无重叠，各管各的阶段
4. **Critic → Render 顺序**：Critic 评估 pre-render 回复的准确性；Render 只改风格不改事实
5. **失败静默回退**：render LLM 调用失败时保留原始回复
6. **主 prompt 瘦身**：移除 `data_guide` 参数，Deep 模式 -22%（5750→4629 chars）

### 效果

| 场景 | Before | After |
|------|--------|-------|
| "EVA 评分怎么样" | "评分 9.1，排名 #1。你想了解角色还是评论？" | "EVA 评分很能打——旧剧场版 8.86 稳居全站前十……我觉得分数里情怀加成不少，真要论精神冲击还是旧剧场版更凶。**你是从 TV 入坑还是直接剧场版？**" |
| "今季有什么新番" | 列表式罗列 + "你想看哪部？" | "无职和穹庐下的魔女是这季真正值得追的。那堆异世界龙傲天全在5分上下晃悠，别浪费时间。" |

### 待解决

- 🟡 Neutral 风格 render 偏弱——still 罗列数据（Test 9: 409 字 vs Bangumi娘 137 字）
- 🟡 Deep 模式未充分触发链式调用（Test 5: 仅 2 轮 search，没走 detail）
- 🟡 Bare title 仍未追问直接搜
- 渲染后消息在历史中出现两次（原始 + 渲染后），多轮对话中可能产生噪音

## Phase 5: Dual Agent Architecture ✅ (2026-06-09, 2026-07-22/23 重构)

> ⚠️ Phase 5 双 Agent 架构已被 Phase 6 取代。以下为历史记录，部分内容已过时。

### 关键差异（历史）

| | Research Agent | Dialogue Agent |
|---|---|---|
| Purpose | Deep research assistant | Fast chat (Bangumi娘 persona) |
| Topology | 3 nodes (reasoning, tool, critic) | 2 nodes (reasoning, tool) |
| Max iterations | 12 | 4 |
| LLM calls | 2-8 | 1-3 |
| Critic | llm（四维度：完整性/具体性/准确性/工具利用） | None |
| Default persona | Neutral (bangumi optional) | Bangumi娘 (neutral optional) |
| Files | `agent/research/` | `agent/dialogue/` |

### Research Agent Data Flow（历史）

```
POST /chat (agent_type="research")
  → reasoning_node:
      manage_memory (L1) → classify_intent (round 1 only)
      → L2 memory recall (_memory_context cached)
      → build_system_prompt (BASE + memory + intent + critic_feedback + style)
      → LLM invoke (tools bound, LLM自主判断)
      → AIMessage
  → route_after_reasoning:
      tool_calls → tool_node → reasoning_node
      chitchat → END (fast path)
      other → critic_node → PASS/over-limit → END, REVISE → reasoning_node
```

### Dialogue Agent Data Flow（历史）

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

## Key conventions

- **Async-first**: all network I/O uses `async/await`. HTTP client is `httpx.AsyncClient`.
- **Error handling**: API failures return `{"_error": "..."}` dicts — never throw. BaseClient retries on 429/502/503/TimeoutException with exponential backoff (max 3 attempts).
- **Sanitizer pattern**: pure functions that whitelist fields, coerce magic numbers to human-readable labels, hard-truncate text, and filter noise. No self, no side effects. 按 A/B/C/D 方法论逐字段决策。
- **Tool return format**: 15/16 tools return structured `dict`（信号/噪音分析后的高密度数据）。`search_local_bangumi` 是唯一返回 `str` 的工具。
- **`output_style`** control: `agent/profiles.py` holds `CharacterProfile` and `AgentProfile` dataclasses. Style is prompt-only — role-first assembly, zero extra LLM call.
- **`.env`** at project root, loaded by `core/config.py`. Key variables: `DATABASE_URL`, `BANGUMI_APP_ID`, `BANGUMI_APP_SECRET`, `ZHIPU_API_KEY`, `DEEPSEEK_API_KEY`, `EMBEDDING_DIMENSION` (default 2048).

## Phase 6 Companion Agent 产品定位

### 她是谁

一个住在 Bangumi 站内的、有性格的二次元损友。**她可以查站内数据，但她存在的理由不是查数据——是陪你聊动画。**

- ✅ 有自己的品位和立场（"这部 8.5 说实话水了"）
- ✅ 可以反问用户（"你觉得呢？"）
- ✅ 可以承认不知道（"这个我没看过"）
- ✅ 推荐一部，说出理由（不是标签匹配列表）
- ✅ "没找到"是正常对话，不是 failure
- ✅ 数据是她吐槽的弹药，不是她交的作业
- ❌ 不是搜索引擎 —— 不要穷举
- ❌ 不是 Perplexity —— 不要多轮交叉验证
- ❌ 不是数据看板 —— 不要列评分分布表
- ❌ 不是维基百科 —— 不要逐条列出所有信息

### 三个功能层级

```
第一层（默认）：ACGN 世界观 + 品位 + 立场
  · 对经典作品有自己的看法
  · 理解社区文化和 meme
  · 能推荐 / 能 diss / 能承认不知道
  · 工具调用：0-1 次

第二层（自然触发）：站内数据查询
  · 查评分 / 排名 / 声优 / 角色 / 排期
  · 看一眼社区讨论在吵什么 → 当八卦聊
  · "要不要帮你查一下？" → 主动 offer
  · 工具调用：1-2 次

第三层（显式激活）：深度分析（Research Skill）
  · depth="deep" 或用户说"深度分析"、"帮我研究"
  · 保留 Critic + 深度链式调用
  · 工具调用：3-6 次
```

## Phase 5.5: Output Style Control ✅ (2026-06-17, 重构 2026-07-22)

> Phase 6 重构中人格将更新为 Companion 损友。以下为 Phase 5.5 的历史架构，profiles 和 prompt_builder 的核心机制不变。

### Architecture: Role-First Prompt Assembly（历史，Phase 6 将层数从 14 → 8）

```
build_system_prompt():
    1. character.identity + motivation      ← 角色是第一层
    2. character.expression_guide（Phase 6: 提前到 layer 2！）
    3. agent_profile.capabilities
    4. agent_profile.tool_strategy（按 depth 分支）
    5. tool_constraint（如有）
    6. _TOOL_CALLING_RULES
    7. data_guide（仅 depth=="deep"）
    8. _CONTINUITY_RULES
    9. intent 策略变体（按 depth 选浅层/深层版）
    10. memory_context（如有）
    11. critic_feedback（仅 depth=="deep"）
    12. character.guardrails
```

### File: `agent/profiles.py` — Character & Agent Profiles

| Class | Purpose | Phase 6 变化 |
|-------|---------|-------------|
| `CharacterProfile` | Identity, motivation, expression guide, guardrails, tool behavior | BANGUMI_CHARACTER 重写为 Companion 损友人格 |
| `AgentProfile` | Capabilities, tool strategy, output format | RESEARCH_PROFILE → COMPANION_PROFILE，tool_strategy 分 quick/deep 两档 |

Phase 6 删除 `BANGUMI_RESEARCH_CHARACTER` — Research Skill 不改变人格，只改变工具策略。

### Core/Style Separation

Phase 5.5 的"角色优先"原则在 Phase 6 继续强化：expression_guide 从 layer 12 提前到 layer 2，人格紧跟身份定义。LLM 在接触任何工具规则之前已被锚定在"我是损友，我这么说话"。

## Phase 5: Memory System ✅ (2026-07-21)

L1 + L2 active. L3 user profile deprecated (2026-07-20).

### L1: Short Memory — Sliding Window

`agent/memory.py` (380 lines). Called at the start of every reasoning_node entry.

1. **Single-message truncation**: ToolMessage > 1500 tokens → truncate content
2. **Token budget check**: tiktoken `cl100k_base` exact encoding
3. **Sliding window**: SystemMessage always preserved. Old messages dropped from head
4. **Orphan cleanup**: discard ToolMessages whose paired AIMessage was trimmed

Token budgets: Research 8000, Dialogue 4000. Phase 6: Companion mode 6000.

### L2: Cross-Session Semantic Memory

`agent/memory_manager.py` + `agent/session_cache.py` + `database/memory_tables.py`.

**Write path** (fire-and-forget, `asyncio.create_task`, 15s hard timeout):
Conversation → truncate to 3000 tokens → DeepSeek generates ~200-char JSON summary → Zhipu embedding-3 vectorize (2048d) → UPSERT `session_memories`.

**Recall path** (dual-channel + time decay):
User query → embedding → pgvector cosine_distance ≤ threshold → time decay scoring → top-5 format → inject into System Prompt.

### Memory Configuration

| Config key | Value | Notes |
|---|---|---|
| `MEMORY_ENABLED` | `True` | Master kill switch |
| `MEMORY_RECALL_TOP_K` | `5` | Max sessions to recall |
| `MEMORY_RECALL_THRESHOLD` | `0.5` | Research semantic threshold |
| `MEMORY_DIALOGUE_RECALL_THRESHOLD` | `0.35` | Dialogue semantic threshold |
| `MEMORY_TIME_DECAY_HALF_LIFE_DAYS` | `14` | Time decay half-life |
| `MEMORY_MAX_INJECT_TOKENS` | `700` | Research L2 injection budget |
| `MEMORY_DIALOGUE_MAX_INJECT_TOKENS` | `300` | Dialogue L2 injection budget |

## Test Coverage

- `test/test_prompts.py` — Profile + prompt builder 测试
- `test/test_critic.py` — Critic 节点测试（仅 deep 模式）
- `test/test_classifier.py` — 意图分类器测试
- `test/test_memory.py` + `test/test_memory_manager.py` — 记忆系统测试
- `test/test_bgm_tools.py` + `test/test_tools.py` — 工具层测试
- `test/test_sanitizers.py` — Sanitizer 测试
- `test/test_client.py` — Client 层测试

## Current Known Issues (2026-07-27)

**🟡 Medium:**

1. **Streaming endpoint is node-level only**: `/chat/stream` pushes node-completion events, not token-by-token
2. **`_memory_context` empty-string caching**: `""` is falsy → re-triggers embedding calls
3. **Neutral 风格 render 偏弱**：Render 后仍可能罗列数据（Neutral 的 `_RENDER_STYLE` 仅 2 条规则）
4. **Bare title → 直接搜**：只给作品名时未追问确认，直接 dump 数据
5. **Deep 模式链式调用不充分**：有时仅 1-2 轮 search 即停止，未触发 search→detail 链

**✅ Resolved (2026-07-25/27):**

- Profiles "数据完整性优先" → "对话优先" (`4fed77f`)
- Phase 6 Step 2: Companion Agent 单一体架构 (`e7df803`)
- Phase 6.5: Render Layer 解耦 (`3cc3ecc`)
- 助手骨架（"回答→罗列→请求下一个问题"）→ 损友聊天风格

## Technical Debt

- **RAG v0/v1 coexistence**: Deprecated `BangumiChunk` series coexists with new `RagEntity` series
- **`create_llm()` no caching**: Creates a new `ChatOpenAI` instance on every call
- **HNSW index creation fails** on 2048d vectors (pgvector limit is 2000d)
- **Render 后消息重复**：render_node 追加新 AIMessage 而非替换，历史中出现两条连续 AIMessage
- **Neutral render 规则过弱**：仅 2 条，对数据罗列约束不足

## Documentation Index

| Document | Content |
|----------|---------|
| `CLAUDE.md` | This file — architecture, conventions, Phase 6.5 render layer |
| `docs/tmp/real_data_test.md` | 真实测试数据 + A/B 对照实验分析 |
| `docs/design/ROADMAP.md` | Development roadmap |
| `docs/design/phase5-memory-system-design.md` | Phase 5 memory system design |
| `docs/design/personality-rendering-layer.md` | Phase 5.5 original render-based design（superseded） |
| `docs/design/architecture-review-2026-07-22.md` | Macro architecture review |
| `docs/design/data-layer-redesign-discussion.md` | Data layer: str→dict evolution |
| `docs/design/bangumi-api-schema-methodology.md` | Data layer: A/B/C/D methodology |
| `docs/memory/` | Memory system manuals (6 files) |
| `README.md` | Project README (partially outdated) |
| `.env.example` | Environment variable template |
