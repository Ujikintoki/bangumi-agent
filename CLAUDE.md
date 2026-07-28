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
  │  纯 ReAct（1-12 轮，无 Critic）       │
  │  人格：损友 / 高冷腹黑 / 可爱 / 助手  │
  │  工具：1-2 轮够用就停                 │
  │                                      │
  │  depth 控制预算和人格参数:             │
  │    quick → 6000 tok, 3 轮, 低深度低主动│
  │    auto  → 10000 tok, 5 轮, 角色默认   │
  │    deep  → 16000 tok, 12 轮, 高深度高主动│
  └──────────────────────────────────────┘
```

入口参数：`depth`（`"auto"` / `"quick"` / `"deep"`）和 `output_style`（`"bangumi"` / `"bangumi_cold"` / `"bangumi_cute"` / `"neutral"`）。
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

#### 拓扑（纯 ReAct，Critic 已屏蔽）

```
                   START
                     │
                     ▼
              reasoning_node ◄──────────┐
                     │                   │
                     ▼                   │
              ┌──────┼──────┐            │
              │      │      │            │
         tool_node  render  END          │
              │      │                   │
              └──────┘                   │
                 │                       │
                 └─── reasoning_node ────┘
```

两级路由（`route_after_reasoning`）：
1. AIMessage 含 tool_calls → tool_node
2. 其他 → render_node（风格渲染后输出）

critic_node 保留注册在 graph 中，但当前不被路由到。如需恢复，在 `route_after_reasoning` 中加回一条路由规则即可。

#### 推理节点 (`agent/orchestrate/nodes.py`)

三种 depth 共享同一推理逻辑，差异仅在参数：

```
1. 意图分类（仅首轮）→ classify_intent_step()
2. L2 记忆召回（仅首轮）→ recall_memory_step()，按 depth 选阈值
3. 构建 System Prompt → build_system_prompt()，按 depth 传 personality tone_kwargs
4. LLM 调用（始终绑定工具，最后一轮解绑）
5. XML 泄漏防护 → guard_xml_leak()
```

消化态引导：deep 模式注入"如果已拿到完整信息直接基于数据回答，不需要逐个搜索每部作品"。

#### Depth 模式

| 模式 | `_MAX_ITERATIONS` | Critic | Token 预算 | 人格参数覆盖 | 工具策略 |
|------|-------------------|--------|-----------|-------------|---------|
| quick | 3 | 无 | 6000 | depth_taste=0.35, initiative=0.15 | 1 轮够用就停，最后一轮强制回复 |
| auto（默认） | 5 | 无 | 10000 | 角色默认 (snark=0.65, depth_taste=0.70, initiative=0.60) | 1-2 轮，最后一轮强制回复 |
| deep | 12 | 无（屏蔽） | 16000 | depth_taste=0.90, initiative=0.85 | 高预算高迭代，无最后一轮强制 |

depth 的本质区别：**不是行为逻辑不同，是预算和人格参数不同**。三种模式跑同一段 ReAct 代码，差异在 `max_tokens`（窗口大小）和 tone_kwargs（回复深度/主动性）的数值。

#### 目录结构

```
agent/
├── state.py                 # 统一 AgentState（含 depth 字段）
├── graph.py                 # 统一 StateGraph（纯 ReAct，Critic 屏蔽但保留注册）
├── llm.py                   # LLM 工厂（多 Provider）
│
├── orchestrate/             # 编排层
│   ├── nodes.py             # reasoning_node + critic_node（保留但未路由）
│   ├── strategies.py        # Companion 浅层 intent 策略
│   ├── deep_strategies.py   # Deep Scene Hints + Critic prompt（保留）
│   ├── prompt_builder.py    # 统一 prompt 组装（TOOL_GUIDANCE 五合一）
│   ├── classifier.py        # 意图分类 + 深度信号检测
│   ├── guardrails.py        # 终端检测 / XML 泄漏 / 重复调用
│   └── helpers.py           # 共享辅助函数
│
├── persona/                 # 人格层
│   ├── profiles.py          # CharacterProfile + Character Cards（4 个）+ 5 档离散人格参数
│   └── render.py            # Render 节点——per-personality voice hints + 参数感知风格微调
│
├── memory/                  # 记忆层
│   ├── short_term.py        # L1 滑动窗口 + 工具结果压缩 + SystemMessage 免疫
│   ├── long_term.py         # L2 跨会话语义记忆
│   └── cache.py             # 跨 HTTP 请求 session 缓存
```

#### 调参杠杆

- `agent/state.py` → `_MAX_ITERATIONS_QUICK/DEFAULT/DEEP` —— 三种模式的迭代上限
- `agent/orchestrate/nodes.py` → tone_kwargs —— depth 对应的人格参数覆盖
- `agent/orchestrate/strategies.py` → `COMPANION_INTENT_PROMPTS` —— 每种意图的浅层策略
- `agent/orchestrate/deep_strategies.py` → `DEEP_SCENE_HINTS` —— deep 模式场景提示
- `agent/orchestrate/prompt_builder.py` → `TOOL_GUIDANCE`, `_CONTINUITY_RULES` —— 工具调用纪律和话题绑定

---

### 人格层 — 怎么说话、什么风格

人格层是 Agent 的"性格"——决定回复听起来像损友还是助手。采用**两层表达管线**：

```
System Prompt                    Render Node
(Character Card + 今天的语气)      (per-personality voice hint + 参数微调)
        │                                    │
        ▼                                    ▼
  决定 agent 怎么思考                  决定输出怎么表达
  "WHAT to think"                    "HOW to say it"
```

**Character Card 和 Render 是不同的东西，缺一不可**：
- Card 决定 agent 的内在判断——它的审美体系、对数据的态度、自我认知
- Render 决定输出的语言风格——吐槽尺度、专业深度、结尾方式

实验证明：去掉 Render → XML 泄漏、个性丢失、数据 dump；去掉 Card → agent 变成无性格的搜索引擎。

#### 四种人格模式

| key | 人格 | snark | depth_taste | initiative | Character Card 主题 |
|-----|------|-------|-------------|------------|-------------------|
| `bangumi` | 二次元损友 | 0.65 (L4) | 0.70 (L4) | 0.60 (L3) | 有审美体系的 ACGN 爱好者，有褒有贬 |
| `bangumi_cold` | 高冷腹黑评论家 | 0.95 (L5) | 0.90 (L5) | 0.25 (L2) | 高标准、话少、精准、不迎合 |
| `bangumi_cute` | 可爱安利爱好者 | 0.15 (L1) | 0.50 (L3) | 0.65 (L4) | 温暖真诚、乐于分享、发现优点 |
| `neutral` | 中性助手 | 0.20 (L1) | 0.40 (L2) | 0.50 (L3) | 客观、简洁、信息优先 |

#### CharacterProfile — 性格定义 (`agent/persona/profiles.py`)

```python
BANGUMI_CHARACTER = CharacterProfile(
    key="bangumi",
    identity="你是谁"              # 轻量身份描述（向后兼容，Character Card 优先）
    motivation="为什么存在"         # 行为动机
    expression_guide="怎么说话"     # 通用语气——所有回复生效
    guardrails="硬约束"            # 字数上限（{word_limit} 占位符）、禁止项
    tool_behavior="对数据的态度"    # 查数据是为了什么
    snark=0.65,                   # 毒舌度 0.0-1.0（5 档离散）
    depth_taste=0.70,             # 分析深度 0.0-1.0（5 档离散）
    initiative=0.60,              # 主动性 0.0-1.0（5 档离散）
)
```

**5 档离散人格参数**（`_render_tone()`）：每个参数（snark/depth_taste/initiative）有 5 档 lookup table。
`_pick_level(value, levels)` 按阈值选中一档，每次只注入 1 个片段——档位增加不改变 System Prompt 长度。

**Character Card**（`_CHARACTER_CARDS` dict）：每个角色有独立的人格素描（~300-400 字），描述"你是谁、你的审美体系、你对数据的态度、你对自己的认知"。Card 是 `build_system_prompt()` 的第一段，优先于碎片化的 identity + motivation + expression_guide。

**expression_guide 的职责**：管通用聊天语气（"吐槽语气"、"语言简洁"、"有自己的判断"）。
它**不管**数据怎么呈现——那是 Render 层的职责。

#### Render 层 — 风格转换 (`agent/persona/render.py`)

独立的 LLM 调用。把 Agent 的"数据回答"改写为角色聊天风格。

设计哲学：per-personality voice hints（~50 chars 每种人格）+ 参数感知的风格微调（0-3 条规则）。
参照 `docs/tmp/UserScriptAi.js` 的油猴脚本：数据由上游整理好，LLM 只做风格转换。

```python
_VOICE["bangumi"]         # "你是 Bangumi 看板娘，一个二次元损友。语气：有态度、有判断…"
_VOICE["bangumi_cold"]    # "你是 Bangumi 看板娘，一个高冷腹黑的评论家。语气：话少、精准、冷…"
_VOICE["bangumi_cute"]    # "你是 Bangumi 看板娘，一个乐于分享的 ACGN 爱好者。语气：温暖、真诚…"
_VOICE["neutral"]         # "你是 Bangumi 助手。语气：客观、简洁、信息优先。"

_style_modifiers()        # 按 snark/depth_taste/initiative 选 0-3 条微调规则（neutral 跳过）

_WORD_LIMIT               # {"quick": "120 字", "auto": "200 字", "deep": "350 字"}
RENDER_TEMPERATURE = 0.4  # 风格改写的大胆程度（高=更骚，低=更保守）
```

**短闲聊跳过**：无工具调用 + 回复 <60 字 → 跳过 render，节省一次 LLM 调用。

**expression_guide 与 Render 职责分离**：

| | expression_guide | Render (_VOICE + _style_modifiers) |
|---|---|---|
| 位置 | `profiles.py` | `render.py` |
| 生效范围 | 所有回复（闲聊+数据） | 仅工具调用后（+ 短闲聊跳过） |
| 管什么 | 通用语气、节奏、态度 | 数据怎么呈现、吐槽尺度、结尾方式 |
| 内容示例 | "用自然的吐槽语气说话" | "评分随口带过，不要每条标⭐" |
| 重叠 | — | 无 ✅ |

#### 调参杠杆

- `profiles.py` → `_SNARK_LEVELS` / `_DEPTH_LEVELS` / `_INITIATIVE_LEVELS` —— 改 5 档离散文本
- `profiles.py` → `_CHARACTER_CARDS["bangumi"]` —— 改损友人设
- `profiles.py` → `CHARACTER_REGISTRY` —— 注册新人设
- `profiles.py` → `CharacterProfile(snark=..., depth_taste=..., initiative=...)` —— 改角色默认参数
- `render.py` → `_VOICE["bangumi"]` —— 改数据呈现的 voice hint
- `render.py` → `_style_modifiers()` —— 改参数→微调规则的映射
- `render.py` → `_WORD_LIMIT` —— 改三种模式的字数上限
- `render.py` → `RENDER_TEMPERATURE` —— 改风格改写的大胆程度

---

### 记忆层 — 能记住什么

#### L1：短记忆 — 滑动窗口 + 压缩 (`agent/memory/short_term.py`)

每轮 reasoning_node 入口调用。tiktoken `cl100k_base` 精确编码。Phase 8 重构：

1. **按 depth 的 Token 预算**（替代旧的 DIALOGUE_MAX_TOKENS / DEFAULT_MAX_TOKENS 双常量）：

| depth | 预算 |
|-------|------|
| quick | 6,000 |
| auto | 10,000 |
| deep | 16,000 |

2. **SystemMessage 永不截断**：`_truncate_oversized_messages()` 中 SystemMessage 直接跳过；`trim_messages()` 中 system_msgs 不参与预算竞争（预算先扣 system_tokens，剩余给对话）。

3. **工具结果压缩**（`_compress_tool_result()`）：上一轮的 ToolMessage 提取关键字段。
   - `search_bangumi_subject`：提取 name, name_cn, type, date, rating.score, rating.rank, id → 2000→80 tokens
   - `get_person_detail` / `get_character_detail`：保留前 400 tokens
   - 其他工具：保留前 300 tokens
   
   当前轮的 ToolMessage 保留完整。

4. **孤儿消息清理**：`_remove_orphaned_tool_messages()` 移除失去配对 AIMessage(tool_calls) 的 ToolMessage，防止 API 400 错误。

管理入口 `manage_memory()`：压缩历史工具 → 截断超大消息 → 滑动窗口 → 清理孤儿。

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
| `MEMORY_MAX_INJECT_TOKENS` | `500` | Deep L2 注入预算 |
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
| "AI 说话太像助手，不够损" | `persona/profiles.py` | `BANGUMI_CHARACTER.expression_guide` 或切换 `output_style` |
| "想要高冷腹黑风格" | 请求参数 | `output_style="bangumi_cold"` |
| "想要可爱安利风格" | 请求参数 | `output_style="bangumi_cute"` |
| "回复太长/太短" | `persona/render.py` | `_WORD_LIMIT` dict |
| "数据回复风格不对" | `persona/render.py` | `_VOICE[style_key]` voice hint |
| "吐槽太狠/太温和" | `persona/profiles.py` | 角色 `snark` 默认值，或 `_SNARK_LEVELS` 文本 |
| "分析太深/太浅" | `persona/profiles.py` | 角色 `depth_taste` 默认值 |
| "AI 调了太多轮工具" | `state.py` | `_MAX_ITERATIONS_*` |
| "总是反问句结尾" | `persona/render.py` | `_style_modifiers()` 中 initiative 对应的结尾规则 |
| "Deep 模式查得太深/太浅" | `orchestrate/deep_strategies.py` | `DEEP_SCENE_HINTS` |
| "闲聊风格不对" | `persona/profiles.py` | Character Card 文本 |
| "忘了之前聊过什么" | `config.py` | `MEMORY_*` 阈值 |
| "多轮对话丢上下文" | `memory/short_term.py` | `DEPTH_TOKEN_BUDGETS` |
| "话题绑定太松/太紧" | `orchestrate/prompt_builder.py` | `_CONTINUITY_RULES` |
| "Render 太保守/太放飞" | `persona/render.py` | `RENDER_TEMPERATURE` |
| "工具调用太多并行" | `orchestrate/prompt_builder.py` | `TOOL_GUIDANCE` 中并行规则 |

## Request/Response model

`POST /chat` accepts `ChatRequest`:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `message` | `str` | (required) | User message |
| `depth` | `"auto" \| "quick" \| "deep"` | `"auto"` | `"auto"`=默认 5 轮, `"quick"`=3 轮快速, `"deep"`=12 轮深度 |
| `output_style` | `"bangumi" \| "bangumi_cold" \| "bangumi_cute" \| "neutral"` | `"bangumi"` | 角色人格 |
| `session_id` | `str` | `"default"` | Session ID |
| `user_id` | `str` | `"anonymous"` | User ID |

`ChatResponse` returns: `reply`, `iterations`, `tools_used`, `query_intent`, `output_style`, `depth`.

## Key conventions

- **Async-first**: all network I/O uses `async/await`. HTTP client is `httpx.AsyncClient`.
- **Error handling**: API failures return `{"_error": "..."}` dicts — never throw. BaseClient retries on 429/502/503/TimeoutException with exponential backoff (max 3 attempts).
- **Sanitizer pattern**: pure functions that whitelist fields, coerce magic numbers to human-readable labels, hard-truncate text, and filter noise. 按 A/B/C/D 方法论逐字段决策。
- **Tool return format**: 15/16 tools return structured `dict`. `search_local_bangumi` 是唯一返回 `str` 的工具。
- **`output_style`** control: `agent/persona/profiles.py` holds `CharacterProfile` dataclasses and `_CHARACTER_CARDS`. Personality expressed through two-layer pipeline: Character Card (System Prompt, determines thinking) → Render Node (independent LLM call, determines expression).
- **`.env`** at project root, loaded by `core/config.py`. Key variables: `DATABASE_URL`, `BANGUMI_APP_ID`, `BANGUMI_APP_SECRET`, `ZHIPU_API_KEY`, `DEEPSEEK_API_KEY`, `EMBEDDING_DIMENSION` (default 2048).

## Test Coverage

- `test/test_prompts.py` — Profile + prompt builder + render prompt 测试
- `test/test_state.py` — State 结构 + 路由测试
- `test/test_graph.py` — 图谱集成测试（mock LLM + mock 工具）
- `test/test_phase5_l1.py` — L1 记忆管理 + 工具结果压缩测试
- `test/test_critic.py` — Critic 节点测试（critic_node 保留但当前不被路由到）
- `test/test_classifier.py` — 意图分类器测试
- `test/test_memory.py` + `test/test_memory_manager.py` — 记忆系统测试
- `test/test_bgm_tools.py` + `test/test_tools.py` — 工具层测试
- `test/test_sanitizers.py` — Sanitizer 测试
- `test/test_client.py` — Client 层测试

## Current Known Issues (2026-07-28)

| 层 | 问题 | 严重度 |
|----|------|--------|
| 编排 | Deep 模式偶发超出迭代上限（13-14 轮 vs max 12），无 Critic 兜底 | 🟡 |
| 编排 | Bare title 仍直接搜——只给作品名时未追问确认 | 🟡 |
| 编排 | Render 后消息在历史中出现两次（原始 + 渲染后），多轮对话可能产生噪音 | 🟡 |
| 编排 | Streaming endpoint 仅节点级，非逐 token | 🟡 |
| 人格 | bangumi_cold / bangumi_cute Character Card 措辞可进一步调优 | 🟢 |
| 记忆 | `_memory_context` 空字符串缓存：`""` 是 falsy → 重复触发 embedding 调用 | 🟡 |

## Technical Debt

| 层 | 问题 |
|----|------|
| 数据 | RAG v0/v1 共存：deprecated `BangumiChunk` 与 `RagEntity` 并行 |
| 数据 | HNSW index 创建失败（2048d 向量，pgvector 上限 2000d） |
| 编排 | `create_llm()` 无缓存——每次调用新建 `ChatOpenAI` 实例 |
| 编排 | 双套记忆阈值（Research/Dialogue）继承自 Phase 4 — 应合并为 depth 分支 |
| 编排 | critic_node 在 graph 中保留注册但未路由——可清理或恢复 |
| 人格 | Render 追加新 AIMessage 而非替换——历史中出现两条连续 AIMessage |

---

## 附录：演化历史

> 理解我们从哪来、走过什么弯路、为什么现在是这个结构。

**2026-05 ~ 06 初（Phase 1-3，地基）**：FastAPI + Bangumi API Client + pgvector + 第一个 ReAct Agent。
假设是 Tool Agent——"用户问、Agent 查、报答案"。

**2026-06-09（Phase 4，双 Agent）**：拆成 Research Agent（深度研究 + Critic）和 Dialogue Agent（快速聊天）。
**这个决策埋下了根因**：架构假设是 Tool Agent（"数据完整性优先"、"深度链式调用"），
但产品定位是 Companion Agent（"陪你聊动画"）。此后 Phase 6 + 6.5 + 8 + 9 都是在纠正这个错配。

**2026-07-21（Phase 5，记忆）**：L1 滑动窗口 + L2 语义召回（双通道 + 时间衰减）。L3 用户画像废弃。

**2026-06-17 ~ 07-22（Phase 5.5，人格化）**：CharacterProfile/AgentProfile dataclass + 角色优先 prompt 组装。
人格层独立出来的起点。

**2026-07-25/26（Phase 6，纠正错配）**：合并双 Agent 为单一 Companion Agent。
`depth` 替代 `agent_type`，Critic 仅 deep 时路由，`agent/dialogue/` 删除。

**2026-07-27（Phase 6.5，解耦风格）**：新增 render_node。Agent 负责准确，Render 负责风格。
主 prompt 移除 `_DATA_INTERPRETATION`（-22%）。expression_guide 与 _RENDER_STYLE 职责分离。

**2026-07-27（Phase 8，Context & Memory 重构）**：解决 deep 模式死亡螺旋和多轮隐式引用丢失。
- L1 按 depth 三级预算（6000/10000/16000）+ SystemMessage 永不截断
- TOOL_GUIDANCE 五合一（~400 tok 替代碎片化 ~1300 tok）
- 工具结果压缩（2000→80 tokens） + 孤儿 ToolMessage 清理
- L2 注入预算收紧 700→500

**2026-07-27（Phase 9，人格系统深化）**：Critic 屏蔽（纯 ReAct）+ 人格参数 5 档离散 + 四种人格模式。
- 3-axis pseudo-continuous → 5-level discrete lookup tables（每维 5 段 prompt 文本）
- 新增 bangumi_cold（高冷腹黑）和 bangumi_cute（可爱安利）两种人格
- Render 重设计：per-personality voice hints + 参数感知风格微调
- 确认 Character Card 和 Render 是两层管线：Card 决定思考、Render 决定表达

**核心教训**：架构假设和产品定位必须一致。Tool Agent 的架构 + Companion Agent 的定位 = 四个阶段的修正（Phase 6 纠正拓扑 + Phase 6.5 纠正输出风格 + Phase 8 纠正 Context 管理 + Phase 9 纠正人格表达）。现在四层架构中，编排层不再预设"查数据是为了交报告"，人格层用两层管线（Card + Render）独立表达。

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
