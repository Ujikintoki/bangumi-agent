# CLAUDE.md

> BGM Agent 操作手册 —— Claude Code 修改此代码库时的参考文档。

## 1. Project Identity

BGM Agent 是 Bangumi 站内的 **Companion Agent（知识型损友）**——有真实数据支撑的聊天角色，卡在"ChatGPT 通用助手"和"Character.AI 角色扮演"之间。

**她不是**搜索引擎、数据看板、维基百科。查数据是为了聊天，不是为了交报告。

两个入口参数控制一切：`depth`（fast/deep）控制思考深度，`output_style`（bangumi/bangumi_cold/bangumi_cute/neutral）控制人格。

技术栈：FastAPI + LangGraph 异质拓扑（Pipeline + ReAct）+ PostgreSQL/pgvector + DeepSeek function-calling + Zhipu embedding-2。

## 2. Guardrails & Anti-Patterns

修改此代码库时的**绝对红线**。违反这些规则会导致运行时错误、人格表达异常、或架构退化。

### 错误处理
- **绝不抛出异常**。所有 API/工具失败返回 `{"_error": "..."}` dict。Client 层通过 `BaseClient` 统一处理重试（429/502/503/TimeoutException，指数退避，最多 3 次）和降级。

### Critic 节点
- **Critic 节点已从 graph 中移除（Phase 4）**。如需恢复，在 `graph.py` 中重新注册节点并添加路由规则。

### 层间隔离
- **上层依赖下层，下层完全不感知上层**。这是四层架构的核心契约。
- 数据层不知道谁在用它；编排层知道要调用哪些工具但不知道返回的 dict 长什么样。
- 不要在数据层引用 `AgentState`；不要在记忆层直接调用 Bangumi API；不要在人格层直接访问数据库。

### SystemMessage 不可侵犯
- **SystemMessage 永不截断、不压缩、不参与滑动窗口预算竞争**。
- `manage_memory()` 中 SystemMessage 直接跳过；`trim_messages()` 中 system_msgs 不参与预算竞争（预算先扣除 system_tokens，剩余给对话消息）。

### 工具返回格式
- 绝大多数工具返回结构化 `dict`，仅 `search_local_bangumi` 返回 `str`。
- 新增工具必须遵循 dict 返回约定 + A/B/C/D 字段方法论。反例只有 RAG 检索这一种场景。

### 人格两层管线
- **Character Card（System Prompt，`profiles.py`）和 Render Node（独立 LLM 调用，`render.py`）是两层独立的管线。**
- Card 决定 agent 的内在判断（WHAT to think），Render 决定输出风格（HOW to say it）。
- 修改人格行为时**必须同时检查两层**——改一个不改另一个会导致人格表达断裂。

### 数据模型
- 使用 **Pydantic v2**（`model_dump()` 而非 `dict()`，`json_schema_extra` 而非 `schema_extra`）。
- `AgentState` 使用 **TypedDict + `Annotated[list, operator.add]`**——消息在节点间追加而非覆盖。

## 3. Architecture Boundaries

### 四层总览

```
编排层 (Orchestration)   ← 决定怎么思考、查多深
人格层 (Persona)         ← 决定怎么说话、什么风格
记忆层 (Memory)          ← 管理上下文窗口和跨会话记忆
数据层 (Data)            ← 提供工具函数和外部数据访问
```

| 层 | 职责 | 核心文件 | 契约 |
|---|------|---------|------|
| **编排层** | StateGraph 拓扑、路由、意图分类、策略、护栏 | `graph.py`, `state.py`, `orchestrate/` | 只通过工具函数访问数据，不直接调 API |
| **人格层** | CharacterProfile 定义 + Render 风格转换 | `persona/profiles.py`, `persona/render.py` | 只做风格转换，不调工具、不访问数据库 |
| **记忆层** | L1 滑动窗口 + 压缩 + L2 语义召回 + session 缓存 | `memory/short_term.py`, `memory/long_term.py`, `memory/cache.py` | 只做截断/压缩/召回，不修改消息语义 |
| **数据层** | 工具函数 + HTTP Client + RAG + pgvector | `tools/`, `clients/`, `rag/`, `database/`, `schemas/` | 不感知上层 state，不知道何时被调用 |

### 编排层 — 拓扑 & 深度模式

v5 异质拓扑（Phase 4）：

```
START → classify_node ─┬── [chat] ──────────→ END
                         ├── [fetch] ─────────→ fetch_search → tool → fetch_detail → tool → synthesize → END
                         ├── [realtime] ──────→ realtime_search → tool → synthesize → END
                         ├── [profile] ───────→ profile_search → tool → synthesize → END
                         └── [explore|discuss|fallback] → reasoning_node ⇄ tool_node → END
```

- **Pipeline intents**（fetch/realtime/profile）：编译时确定性步骤，每步独立节点 + 独立 prompt + 独立工具绑定
- **ReAct intents**（explore/discuss/fallback）：运行时 LLM 自主探索，隐式终止（输出文本 = END）
- **Chat**：直通 END，main.py 直接 render

路由规则：
- `route_after_classify`：按 intent + 置信度（<0.7 → ReAct fallback）分发到 pipeline 入口或 reasoning_node
- `route_after_tool`：pipeline 步骤路由（iterations→下一步）+ 硬熔断 + ReAct 路由
- `route_after_reasoning`：AIMessage 含 tool_calls → tool_node；其他 → END（隐式终止）

两种 depth 的行为差异：
- fast：pipeline 或 ReAct，正常迭代上限和预算
- deep：pipeline 步骤不变，ReAct 有更高迭代上限（explore 5/discuss 6）+ 更大 Token 预算（16000 tok）

迭代上限见 `state.py` `_INTENT_MAX_ITERATIONS` + `_INTENT_DEEP_OVERRIDES`，Token 预算见 `memory/short_term.py` `DEPTH_TOKEN_BUDGETS`。

classify_node 流程（首轮）：
1. 意图分类 → `classify_intent_step()`（LLM function calling，7 intent）
2. L2 记忆召回 → `recall_memory_step()`

reasoning_node 流程：
1. 构建 System Prompt → `build_aggregator_prompt()`
2. Dynamic Tool Binding → 按 intent 过滤 TOOLS_BY_INTENT + tool_choice
3. L1 记忆管理 → `manage_memory()`
4. LLM 调用（首轮 required，后续 auto）
5. 消化态引导（接近最后一轮注入 `_LAST_CHANCE_DIGEST_HINT`）
6. XML 泄漏防护 → `guard_xml_leak()`

### 人格层 — 两层管线

```
Character Card (System Prompt) → 决定 agent 怎么思考（WHAT to think）
Render Node (独立 LLM 调用)   → 决定输出怎么表达（HOW to say it）
```

**两者缺一不可。** 实验证明：去掉 Render → XML 泄漏、个性丢失、数据 dump；去掉 Card → agent 变成无性格的搜索引擎。

四种人格模式（`persona/profiles.py` `_CHARACTER_CARDS` + `CharacterProfile`）：

| key | 人格 | 人物设定 |
|-----|------|---------|
| `bangumi` | 二次元损友 | 有审美体系的 ACGN 爱好者，有褒有贬 |
| `bangumi_cold` | 高冷腹黑评论家 | 高标准、话少、精准、不迎合 |
| `bangumi_cute` | 可爱安利爱好者 | 温暖真诚、乐于分享、发现优点 |
| `neutral` | 中性助手 | 客观、简洁、信息优先 |

具体参数值（snark/depth_taste/initiative）见各 `CharacterProfile` 实例——这些值在调参时频繁变动，以代码为准。

5 档离散参数（`_render_tone()` in `profiles.py`）：每维 5 段 prompt 文本，按阈值查找注入——档位增加不改变 System Prompt 长度。阈值和文本见 `_SNARK_LEVELS` / `_DEPTH_LEVELS` / `_INITIATIVE_LEVELS`。

Render 层（`persona/render.py`）：
- per-personality voice hints（`_VOICE` dict）
- `_style_modifiers(snark, depth_taste, initiative)` 按参数选 0-3 条微调规则（neutral 跳过）
- `_WORD_LIMIT` 按 depth 控制字数上限——具体值见代码
- `RENDER_TEMPERATURE` 控制风格改写的大胆程度——见代码
- 短闲聊跳过规则：无工具调用 + 回复较短 → 跳过 render（阈值见代码）

### 记忆层 — L1 + L2

**L1 短记忆**（`memory/short_term.py`）：
- 按 depth 三级 Token 预算——见 `DEPTH_TOKEN_BUDGETS` dict
- SystemMessage 永不截断
- 工具结果压缩：上一轮 ToolMessage 提取关键字段（`_compress_tool_result()`，不同工具不同策略）
- 孤儿 ToolMessage 清理（`_remove_orphaned_tool_messages()`，防止 API 400）
- 管理入口 `manage_memory()`：压缩历史工具 → 截断超大消息 → 滑动窗口 → 清理孤儿

**L2 跨会话记忆**（`memory/long_term.py`）：
- 写入（fire-and-forget，超时见 `asyncio.wait_for` timeout 参数）：对话摘要 → embedding 向量化 → UPSERT `session_memories`
- 召回（双通道 + 时间衰减）：语义召回（cosine_distance）+ 时效回退。衰减公式和半衰期见 `core/config.py` `MEMORY_TIME_DECAY_HALF_LIFE_DAYS`
- 阈值和注入预算见 `core/config.py` `MEMORY_*` 配置项。注意 deep 和非 deep 使用不同的阈值——非 deep 的阈值命名过时（`MEMORY_DIALOGUE_*`，历史遗留，实际含义是"非 deep 模式"）

### 数据层 — 工具 & Client

工具函数（`tools/bgm_tools.py`）：LangChain `@tool` 装饰，大部分无条件可用，少数需 `BANGUMI_ACCESS_TOKEN`（详见 `get_agent_tools()` 中的条件分支）。全部返回结构化 dict（A/B/C/D 字段方法论），`search_local_bangumi` 是唯一返回 `str` 的工具。

Client（`clients/`）：`BaseClient` → `BangumiClient` → sanitizer 纯函数。async-first（httpx），API 失败绝不抛异常。

RAG（`rag/`）：`text_processor.py` → `ingestion.py` → `retriever.py`。5 阶段检索管线——标量预过滤 → 语义分桶 → 对数归一化 → MMR 同名去重。

## 4. Where to Find What

### 调参速查

| 想改的效果 | 文件 | 改什么 |
|-----------|------|--------|
| 切换人格 | 请求参数 | `output_style="bangumi_cold"` / `"bangumi_cute"` |
| 回复太长/太短 | `persona/render.py` | `_WORD_LIMIT` dict |
| 吐槽太狠/太温和 | `persona/profiles.py` | 角色 `snark` 默认值，或 `_SNARK_LEVELS` 文本 |
| 分析太深/太浅 | `persona/profiles.py` | 角色 `depth_taste` 默认值 |
| AI 调了太多轮工具 | `state.py` | `_MAX_ITERATIONS_*` |
| 多轮对话丢上下文 | `memory/short_term.py` | `DEPTH_TOKEN_BUDGETS` |
| 忘了之前聊过什么 | `core/config.py` | `MEMORY_*` 阈值 |
| Render 太保守/太放飞 | `persona/render.py` | `RENDER_TEMPERATURE` |
| Deep 模式不调工具 | `orchestrate/prompt_builder.py` | `TOOL_GUIDANCE` + deep 场景提示 |
| 常识问题误调工具 | `orchestrate/classifier.py` | intent 分类规则 |
| 搜索空结果耗时过长 | `orchestrate/strategies.py` | 空结果处理策略 |
| 话题绑定太松/太紧 | `orchestrate/prompt_builder.py` | `_CONTINUITY_RULES` |

### 关键文件

```
agent/
├── state.py                       # AgentState + 迭代上限
├── graph.py                       # v5 异质拓扑 StateGraph（Pipeline + ReAct）
├── llm.py                         # LLM 工厂（多 Provider）
├── devtools.py                    # Token 统计 + 节点计时（DEV_MODE）
├── orchestrate/
│   ├── nodes.py                   # classify_node + reasoning_node + 5 pipeline 节点
│   ├── strategies.py              # 浅层 intent 策略（COMPANION_INTENT_PROMPTS）
│   ├── deep_strategies.py         # Deep 模式 Scene Hints + intent 策略
│   ├── prompt_builder.py          # System Prompt 组装 + tool_choice + TOOLS_BY_INTENT
│   ├── classifier.py              # 7 intent LLM 分类器 + 置信度路由
│   ├── guardrails.py              # 终端检测 / XML 泄漏 / 重复调用检测
│   └── helpers.py                 # 共享辅助函数
├── persona/
│   ├── profiles.py                # CharacterProfile + Character Cards + 5 档离散参数
│   └── render.py                  # Render Node — per-personality voice hints + 风格微调
├── memory/
│   ├── short_term.py              # L1 滑动窗口 + 工具压缩 + SystemMessage 免疫
│   ├── long_term.py               # L2 语义召回 + 时间衰减
│   └── cache.py                   # Session 缓存（跨 HTTP 请求）
tools/bgm_tools.py                  # LangChain @tool 函数
clients/                            # HTTP 客户端（httpx 异步 + 指数退避重试）+ sanitizers
rag/                                # RAG 检索管线
database/                           # SQLModel ORM + pgvector
schemas/tools_input.py              # Pydantic v2 工具输入 schema
core/config.py                      # pydantic-settings 全局配置
main.py                             # FastAPI 入口（/chat, /chat/stream）
test/                               # ~570 个测试（20 个文件）
```

## 5. Commands

```bash
# 启动开发服务器
uvicorn main:app --reload --port 8000

# 全部测试（需要 PostgreSQL + pgvector）
pytest test/ -v

# 单文件测试
pytest test/test_memory_manager.py -v

# 跳过数据库依赖的测试
pytest test/ --ignore=test/test_rag.py -v

# 仅记忆系统测试
pytest test/test_memory.py test/test_memory_manager.py test/test_phase5_l1.py -v

# 启动 PostgreSQL + pgvector（Docker）
docker run -d --name bangumi-pg \
  -e POSTGRES_USER=myuser -e POSTGRES_PASSWORD=mypassword \
  -e POSTGRES_DB=bangumidb -p 5432:5432 pgvector/pgvector:pg16

# 发测试请求
curl -s -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "你好，最近有什么好看的番？", "depth": "fast", "output_style": "bangumi"}'
```

## 6. Known Issues & Tech Debt

> ⚠️ **这是快照（2026-08-04），会过时。** 实时版本见 [`docs/design/ROADMAP.md`](docs/design/ROADMAP.md)。

Phase 4-4.1 已解决：submit_facts 反模式移除、隐式终止统一、pipeline 拓扑落地、多轮 session 修复、classifier fetch/explore 边界微调。

按层组织。

### 编排层

| 问题 | 严重度 | 定位 | 备注 |
|------|--------|------|------|
| 字数控制形同虚设——fast 模式近半数回复超过 200 字限制 | 🔴 P0 | `persona/render.py` `_WORD_LIMIT` | prompt 建议不够强，需硬截断或加大约束权重 |
| Deep 模式偶发 0 工具调用，闭卷答深度问题 | 🔴 P0 | `orchestrate/prompt_builder.py` | ReAct intent 仍需强化"deep 模式下即使是常识也应查数据佐证" |
| "今天星期几"等常识问题被误分类为 realtime | 🔴 P0 | `orchestrate/classifier.py` | classifier 需增加纯常识判断 |
| 长程多轮（8 轮+）话题跳转后 R8 完全失忆 | 🟡 P1 | `memory/short_term.py` 预算策略 | fast 10000 tok 对多话题对话不足 |
| Streaming 仅节点级（非逐 token） | 🟡 | `main.py` `/chat/stream` | 用户看到节点间等待，体验差 |
| Bare title 不追问确认直接搜索 | 🟢 | `orchestrate/strategies.py` | 仅作品名时追问体验更好，当前 classify→fetch pipeline |
| Deep 模式偶发超出迭代上限 | 🟢 | `orchestrate/strategies.py` | explore 5/discuss 6 deep，硬熔断兜底 |
| `create_llm()` 无缓存——每次调用新建 `ChatOpenAI` 实例 | 🟢 | `agent/llm.py` | 同一请求内多次调 reasoning/render，每次重建 |

### 人格层

| 问题 | 严重度 | 定位 | 备注 |
|------|--------|------|------|
| bangumi_cold / bangumi_cute Character Card 措辞可进一步调优 | 🟢 | `persona/profiles.py` | 当前已可用，但差异化可以更鲜明 |

### 记忆层

| 问题 | 严重度 | 定位 | 备注 |
|------|--------|------|------|
| `_memory_context` 空字符串缓存：`""` 是 falsy → 重复触发 embedding 调用 | 🟡 | `cache.py` | 改用 `is not None` 判断 |
| 双套记忆阈值命名过时（`MEMORY_DIALOGUE_*`） | 🟢 | `core/config.py` | 历史遗留，实际用于非 deep 模式 |

### 数据层

| 问题 | 严重度 | 定位 | 备注 |
|------|--------|------|------|
| RAG v0/v1 共存：deprecated `BangumiChunk` 与 `RagEntity` 并行 | 🟡 | `rag/` | 新代码只使用 `RagEntity`，不要引用 `BangumiChunk` |
| HNSW index 创建失败（2048d 超 pgvector 上限 2000d） | 🟢 | `database/` | 当前用 embedding-2 (1024d) 已规避。不要创建 2000d 以上的向量索引 |

---

## 文档索引

- [`README.md`](README.md) — 产品主页、API 文档、快速开始
- [`docs/design/ROADMAP.md`](docs/design/ROADMAP.md) — 架构状态、演化历史、未来计划
- [`docs/design/`](docs/design/) — 设计决策记录（架构 review、记忆系统设计、A/B/C/D 方法论）
- [`docs/eval/`](docs/eval/) — 评测体系设计
