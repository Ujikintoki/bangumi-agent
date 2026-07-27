# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

**Bangumi 的 AI 看板娘** — 一个住在 Bangumi 站内的、有性格的二次元损友。她可以查站内数据，但她存在的理由不是查数据——是陪你聊动画。

技术栈：FastAPI + LangGraph ReAct agent + PostgreSQL + pgvector RAG + Zhipu embedding-3 + DeepSeek function-calling。

**产品定位**：Companion Agent（知识型损友），不是 Tool Agent（搜索引擎），不是 Research Agent（Perplexity 式深度分析）。在 Agent 光谱上卡在 "ChatGPT 通用助手" 和 "Character.AI 角色扮演" 之间——有真实数据支撑的聊天角色。

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

## Architecture — 四层架构

系统分为四个独立演化的层次。每一层可以单独修改，不影响其他层。

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

入口参数：`depth`（`"auto"` / `"quick"` / `"deep"`）和 `output_style`（`"bangumi"` / `"neutral"`）。
`agent_type` 参数已 deprecated。

### 四层概览

| 层 | 管什么 | 核心文件 | 稳定性 | 修改频率 |
|---|--------|---------|--------|---------|
| **编排层** | 怎么思考、查多深、用什么工具 | `orchestrate/nodes.py`, `state.py`, `orchestrate/strategies.py`, `orchestrate/classifier.py`, `orchestrate/guardrails.py` | 🟡 刚稳定 | 偶尔 |
| **人格层** | 怎么说话、什么风格 | `persona/profiles.py`, `persona/render.py` | 🟡 活跃调参 | **经常** |
| **记忆层** | 能记住什么 | `memory/short_term.py`, `memory/long_term.py`, `memory/cache.py` | ✅ 稳 | 很少 |
| **数据层** | 能查什么、查到的是什么 | `clients/`, `tools/`, `rag/`, `database/`, `schemas/` | ✅ 稳 | 几乎不 |

**核心原则**：上层依赖下层，下层完全不感知上层。数据层不知道谁在用它；编排层知道要调用哪些工具但不知道返回的 dict 长什么样。

---

### 编排层 — 怎么思考、查多深

编排层是 Agent 的"大脑"——决定调不调工具、调哪个、调几轮后停。

#### 拓扑

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

五级路由（`route_after_reasoning`）：
1. AIMessage 含 tool_calls → tool_node
2. intent = chitchat → END（快速通道）
3. depth == "deep" → critic_node
4. 当前轮有工具调用 → render_node → END
5. 其他 → END

#### Depth 模式

| 模式 | `_MAX_ITERATIONS` | Critic | Render | 工具策略 |
|------|-------------------|--------|--------|---------|
| quick | 3 | 无 | 有工具时触发 | 1 轮够用就停 |
| auto（默认） | 5 | 无 | 有工具时触发 | 1-2 轮 |
| deep（Research Skill） | 12 | 有（LLM 四维度评估） | 有工具时触发 | 深度链式 search→detail→characters |

#### 目录结构

```
agent/
├── state.py                 # 统一 AgentState（含 depth 字段）
├── graph.py                 # 统一 StateGraph（含 render_node 路由）
├── llm.py                   # LLM 工厂（多 Provider）
│
├── orchestrate/             # 编排层
│   ├── nodes.py             # reasoning_node + critic_node
│   ├── strategies.py        # Companion 浅层 intent 策略
│   ├── deep_strategies.py   # Deep 策略 + Critic prompt
│   ├── prompt_builder.py    # 统一 prompt 组装（8 层，无 data_guide）
│   ├── classifier.py        # 意图分类 + 深度信号检测
│   ├── guardrails.py        # 终端检测 / XML 泄漏 / 重复调用
│   └── helpers.py           # 共享辅助函数
│
├── persona/                 # 人格层
│   ├── profiles.py          # CharacterProfile + AgentProfile
│   └── render.py            # Render 节点——工具回复风格转换
│
├── memory/                  # 记忆层
│   ├── short_term.py        # L1 滑动窗口截断
│   ├── long_term.py         # L2 跨会话语义记忆
│   └── cache.py             # 跨 HTTP 请求 session 缓存
```

#### 调参杠杆

- `agent/state.py` → `_MAX_ITERATIONS_QUICK/DEFAULT/DEEP` —— 三种模式的迭代上限
- `agent/orchestrate/strategies.py` → `COMPANION_INTENT_PROMPTS` —— 每种意图的浅层策略
- `agent/orchestrate/deep_strategies.py` → `INTENT_PROMPTS` —— deep 模式策略（更长更详细）
- `agent/orchestrate/deep_strategies.py` → `TOOL_DEPENDENCY_CONSTRAINT` —— deep 模式工具链顺序
- `agent/orchestrate/prompt_builder.py` → `_TOOL_CALLING_RULES`, `_CONTINUITY_RULES` —— 工具调用纪律和话题绑定

---

### 人格层 — 怎么说话、什么风格

人格层是 Agent 的"性格"——决定回复听起来像损友还是助手。分为两部分：**性格定义**（`profiles.py`，所有回复生效）和**风格渲染**（`render.py`，仅工具回复时叠加上去）。

#### CharacterProfile — 性格定义 (`agent/persona/profiles.py`)

```python
BANGUMI_CHARACTER = CharacterProfile(
    identity="你是谁"              # "二次元损友"——改这一行，AI 就换人设
    motivation="为什么存在"         # "让对话有趣"——改动机，影响回复倾向
    expression_guide="怎么说话"     # 通用语气、节奏、态度——所有回复生效
    guardrails="硬约束"            # 字数上限、禁止项
    tool_behavior="对数据的态度"    # 查数据是为了什么
)

NEUTRAL_CHARACTER  # 中性助手——更正式，无吐槽人格

COMPANION_PROFILE  # AgentProfile: 能力描述 + 工具策略 + 输出格式
```

**expression_guide 的职责**：管通用聊天语气（"吐槽语气"、"语言简洁"、"有自己的判断"）。
它**不管**数据怎么呈现——那是 Render 层的职责。

#### Render 层 — 风格转换 (`agent/persona/render.py`)

仅工具调用后触发。把 Agent 的"数据回答"改写为角色聊天风格。

设计哲学：极简 prompt（~380 chars），纯人格 + 任务——不含数据解读教科书。
参照 `docs/tmp/UserScriptAi.js` 的油猴脚本：数据由上游整理好，LLM 只做吐槽。

```python
_RENDER_STYLE["bangumi"]     # 3 条数据呈现规则（评分怎么带、数据怎么融、结尾怎么收）
_RENDER_WORD_LIMIT           # {"quick": "120 字", "auto": "200 字", "deep": "350 字"}
RENDER_TEMPERATURE = 0.4     # 风格改写的大胆程度（高=更骚，低=更保守）
```

**expression_guide 与 _RENDER_STYLE 职责分离**：

| | expression_guide | _RENDER_STYLE |
|---|---|---|
| 位置 | `profiles.py` | `render.py` |
| 生效范围 | 所有回复（闲聊+数据） | 仅工具调用后 |
| 管什么 | 通用语气、节奏、态度 | 数据怎么呈现 |
| 内容示例 | "用自然的吐槽语气说话" | "评分随口带过，不要每条标⭐" |
| 重叠 | — | 无 ✅ |

#### 调参杠杆

- `profiles.py` → `BANGUMI_CHARACTER.expression_guide` —— 改通用说话语气
- `profiles.py` → `BANGUMI_CHARACTER.guardrails` —— 改字数限制和禁止项
- `render.py` → `_RENDER_STYLE["bangumi"]` —— 改数据呈现风格（3 条规则）
- `render.py` → `_RENDER_WORD_LIMIT` —— 改三种模式的字数上限
- `render.py` → `RENDER_TEMPERATURE` —— 改风格改写的大胆程度

---

### 记忆层 — 能记住什么

#### L1：短记忆 — 滑动窗口 (`agent/memory/short_term.py`)

每轮 reasoning_node 入口调用。tiktoken `cl100k_base` 精确编码，SystemMessage 永久保留，旧消息从头部丢弃。

Token 预算：deep 模式 8000, 非 deep 模式 4000。

#### L2：跨会话语义记忆 (`agent/memory/long_term.py` + `cache.py`)

**写入**（fire-and-forget，15s 超时）：对话 → DeepSeek 生成 ~200 char JSON 摘要 → Zhipu embedding-3 向量化（2048d）→ UPSERT `session_memories`。

**召回**（双通道 + 时间衰减）：用户 query → embedding → pgvector cosine_distance ≤ threshold → `combined_score = (1 - distance) × 0.5^(days/14)` → top-5 → 注入 System Prompt。

| Config key | Value | Notes |
|---|---|---|
| `MEMORY_ENABLED` | `True` | Master kill switch |
| `MEMORY_RECALL_TOP_K` | `5` | Max sessions to recall |
| `MEMORY_RECALL_THRESHOLD` | `0.5` | Deep 语义阈值 |
| `MEMORY_DIALOGUE_RECALL_THRESHOLD` | `0.35` | 非 deep 语义阈值 |
| `MEMORY_TIME_DECAY_HALF_LIFE_DAYS` | `14` | 时间衰减半衰期 |
| `MEMORY_MAX_INJECT_TOKENS` | `700` | Deep L2 注入预算 |
| `MEMORY_DIALOGUE_MAX_INJECT_TOKENS` | `300` | 非 deep L2 注入预算 |

> ⚠️ 双套阈值（Deep/非 Deep）继承自 Phase 4 双 Agent 时代。后续可合并为 depth 分支的单一配置。

---

### 数据层 — 能查什么、查到的是什么

最稳定的层。自 dict 结构化重构（A/B/C/D 字段方法论）后基本没动过。

#### 工具 (`tools/bgm_tools.py`)

16 个 LangChain `@tool` 函数（13 个无条件可用 + 3 个需 `BANGUMI_ACCESS_TOKEN`），15 个返回结构化 dict，`search_local_bangumi` 是唯一返回 `str` 的工具（RAG 结果）。

#### Client (`clients/`)

`BaseClient` → `BangumiClient` → sanitizer 纯函数。Async-first，API 失败返回 `{"_error": "..."}` dict 绝不抛异常。429/502/503/TimeoutException 指数退避重试（最多 3 次）。

#### RAG (`rag/`)

`text_processor.py` → `ingestion.py` → `retriever.py`。支持模糊描述搜索（"80年代黑暗机战番"）。

#### Database (`database/`)

SQLModel + pgvector。记忆表和 RAG 实体的持久层。

---

## 调参速查

| 想改的效果 | 文件 | 改什么 |
|-----------|------|--------|
| "AI 说话太像助手，不够损" | `persona/profiles.py` | `BANGUMI_CHARACTER.expression_guide` |
| "回复太长/太短" | `persona/render.py` | `_RENDER_WORD_LIMIT` dict |
| "数据回复风格不对" | `persona/render.py` | `_RENDER_STYLE["bangumi"]` 3 条规则 |
| "AI 调了太多轮工具" | `state.py` | `_MAX_ITERATIONS_*` |
| "总是反问句结尾" | `persona/render.py` | `_RENDER_STYLE["bangumi"]` 结尾规则 |
| "Deep 模式查得太深/太浅" | `orchestrate/deep_strategies.py` | `INTENT_PROMPTS` + `TOOL_DEPENDENCY_CONSTRAINT` |
| "闲聊风格不对" | `persona/profiles.py` | `BANGUMI_CHARACTER.expression_guide`（闲聊只用这个） |
| "忘了之前聊过什么" | `config.py` | `MEMORY_*` 阈值 |
| "话题绑定太松/太紧" | `orchestrate/prompt_builder.py` | `_CONTINUITY_RULES` |
| "Render 太保守/太放飞" | `persona/render.py` | `RENDER_TEMPERATURE` |

## Request/Response model

`POST /chat` accepts `ChatRequest`:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `message` | `str` | (required) | User message |
| `depth` | `"auto" \| "quick" \| "deep"` | `"auto"` | `"auto"`=LLM 自行判断, `"quick"`=1-3 轮, `"deep"`=Research Skill |
| `output_style` | `"neutral" \| "bangumi"` | `"bangumi"` | 角色人格 |
| `session_id` | `str` | `"default"` | Session ID |
| `user_id` | `str` | `"anonymous"` | User ID |

`ChatResponse` returns: `reply`, `iterations`, `tools_used`, `query_intent`, `output_style`, `depth`.

## Key conventions

- **Async-first**: all network I/O uses `async/await`. HTTP client is `httpx.AsyncClient`.
- **Error handling**: API failures return `{"_error": "..."}` dicts — never throw. BaseClient retries on 429/502/503/TimeoutException with exponential backoff (max 3 attempts).
- **Sanitizer pattern**: pure functions that whitelist fields, coerce magic numbers to human-readable labels, hard-truncate text, and filter noise. 按 A/B/C/D 方法论逐字段决策。
- **Tool return format**: 15/16 tools return structured `dict`. `search_local_bangumi` 是唯一返回 `str` 的工具。
- **`output_style`** control: `agent/persona/profiles.py` holds `CharacterProfile` and `AgentProfile` dataclasses. Style is prompt-only — role-first assembly, zero extra LLM call.
- **`.env`** at project root, loaded by `core/config.py`. Key variables: `DATABASE_URL`, `BANGUMI_APP_ID`, `BANGUMI_APP_SECRET`, `ZHIPU_API_KEY`, `DEEPSEEK_API_KEY`, `EMBEDDING_DIMENSION` (default 2048).

## Test Coverage

- `test/test_prompts.py` — Profile + prompt builder + render prompt 测试
- `test/test_state.py` — State 结构 + 路由测试
- `test/test_critic.py` — Critic 节点测试（仅 deep 模式）
- `test/test_classifier.py` — 意图分类器测试
- `test/test_memory.py` + `test/test_memory_manager.py` — 记忆系统测试
- `test/test_bgm_tools.py` + `test/test_tools.py` — 工具层测试
- `test/test_sanitizers.py` — Sanitizer 测试
- `test/test_client.py` — Client 层测试

## Current Known Issues (2026-07-27)

| 层 | 问题 | 严重度 |
|----|------|--------|
| 编排 | Deep 模式链式调用不充分——有时仅 1-2 轮 search，未触发 search→detail 链 | 🟡 |
| 编排 | Bare title 仍直接搜——只给作品名时未追问确认 | 🟡 |
| 编排 | Render 后消息在历史中出现两次（原始 + 渲染后），多轮对话可能产生噪音 | 🟡 |
| 编排 | Streaming endpoint 仅节点级，非逐 token | 🟡 |
| 人格 | Neutral 风格 render 偏弱——仍可能罗列数据（`_RENDER_STYLE` 仅 2 条规则） | 🟡 |
| 记忆 | `_memory_context` 空字符串缓存：`""` 是 falsy → 重复触发 embedding 调用 | 🟡 |

## Technical Debt

| 层 | 问题 |
|----|------|
| 数据 | RAG v0/v1 共存：deprecated `BangumiChunk` 与 `RagEntity` 并行 |
| 数据 | HNSW index 创建失败（2048d 向量，pgvector 上限 2000d） |
| 编排 | `create_llm()` 无缓存——每次调用新建 `ChatOpenAI` 实例 |
| 编排 | 双套记忆阈值（Research/Dialogue）继承自 Phase 4 — 应合并为 depth 分支 |
| 人格 | Render 追加新 AIMessage 而非替换——历史中出现两条连续 AIMessage |

---

## 附录：演化历史

> 理解我们从哪来、走过什么弯路、为什么现在是这个结构。

**2026-05 ~ 06 初（Phase 1-3，地基）**：FastAPI + Bangumi API Client + pgvector + 第一个 ReAct Agent。
假设是 Tool Agent——"用户问、Agent 查、报答案"。

**2026-06-09（Phase 4，双 Agent）**：拆成 Research Agent（深度研究 + Critic）和 Dialogue Agent（快速聊天）。
**这个决策埋下了根因**：架构假设是 Tool Agent（"数据完整性优先"、"深度链式调用"），
但产品定位是 Companion Agent（"陪你聊动画"）。此后 Phase 6 + 6.5 都是在纠正这个错配。

**2026-07-21（Phase 5，记忆）**：L1 滑动窗口 + L2 语义召回（双通道 + 时间衰减）。L3 用户画像废弃。

**2026-06-17 ~ 07-22（Phase 5.5，人格化）**：CharacterProfile/AgentProfile dataclass + 角色优先 prompt 组装。
人格层独立出来的起点。

**2026-07-25/26（Phase 6，纠正错配）**：合并双 Agent 为单一 Companion Agent。
`depth` 替代 `agent_type`，Critic 仅 deep 时路由，`agent/dialogue/` 删除。

**2026-07-27（Phase 6.5，解耦风格）**：新增 render_node。Agent 负责准确，Render 负责风格。
主 prompt 移除 `_DATA_INTERPRETATION`（-22%）。expression_guide 与 _RENDER_STYLE 职责分离。

**核心教训**：架构假设和产品定位必须一致。Tool Agent 的架构 + Companion Agent 的定位 = 三个阶段的修正（Phase 6 纠正拓扑 + Phase 6.5 纠正输出风格）。现在四层架构中，编排层不再预设"查数据是为了交报告"，人格层可以独立调优而不用改推理逻辑。

## Documentation Index

| Document | Content |
|----------|---------|
| `CLAUDE.md` | This file — 四层架构、调参速查、编码规范 |
| `docs/design/ROADMAP.md` | 架构状态 & 路线图 |
| `docs/design/architecture-review-2026-07-22.md` | 宏观架构 review |
| `docs/design/phase5-memory-system-design.md` | Phase 5 记忆系统完整设计 |
| `docs/design/data-layer-redesign-discussion.md` | 工具层 str→dict 迁移决策过程 |
| `docs/design/bangumi-api-schema-methodology.md` | A/B/C/D 字段方法论 |
| `docs/memory/` | 记忆系统手册（6 文件） |
| `docs/tmp/real_data_test.md` | Phase 5 测试数据基线 |
| `README.md` | 项目 README |
| `.env.example` | Environment variable template |
