# CLAUDE.md

> BGM Agent — Claude Code 操作手册。所有架构声明以代码为准，本文档最后更新: 2026-08-16。

## 1. 项目是什么

BGM Agent 是部署在 [bangumi.tv](https://bgm.tv) 站内的 AI 聊天角色（Companion Agent），通过 FastAPI 对外提供 `/chat` 与 `/chat/stream` 接口。

**产品定位（必读）**：它不是"帮你查数据的 AI"，而是一个住在 Bangumi 里的动画损友——有自己的品位和脾气、会明确说"这部过誉了"、可以被反驳也会承认错误、记得你聊过什么。完整愿景见 [`docs/design/claude-on-bangumi-vision.md`](docs/design/claude-on-bangumi-vision.md)，**修改任何功能、人格或 prompt 之前必须先读**。核心原则：AI 不必永远"正确"（否则只是搜索引擎）；存在感要低、存在要有意义；隐私第一（用户评分与观看历史是对话的一部分，不是公共数据）；记住该记住的、忘记该忘记的。愿景相对现状的增量（被动触发、页面语境、社区参与、可调人格）是 Phase 8+ 方向，参考 [`docs/design/evolution-roadmap-phase7-9.md`](docs/design/evolution-roadmap-phase7-9.md)。

**架构必读**：[`docs/architecture-blueprint.md`](docs/architecture-blueprint.md) — 宏观架构蓝图（9 板块清单 / 四层接口契约 / 编排模式库 / 演化地图）。涉及跨层改动、新增板块或编排形态前先读；新功能按其中 §7"开工前四问"定位，定位不进地图的需求先评审地图而非硬塞代码。

技术栈：**FastAPI + LangGraph 异质拓扑（Pipeline + ReAct）+ DeepSeek/OpenAI 兼容 function-calling + PostgreSQL/pgvector + 智谱 embedding-2 (1024d)**。

两个入口参数控制一切：

| 参数 | 值 | 说明 |
|------|-----|------|
| `depth` | `"fast"` / `"deep"` | 推理深度：迭代上限 + Token 预算 + 记忆阈值 |
| `output_style` | `"bangumi"` / `"bangumi_kawaii"` / `"neutral"` | 人格（默认 `"bangumi"`） |

## 2. 常用命令

```bash
# 启动开发服务器
uvicorn main:app --reload --port 8000

# 全部测试（539 个，需要 PostgreSQL + pgvector）
pytest test/ -v

# 跳过数据库依赖的测试
pytest test/ --ignore=test/test_rag.py -v

# 仅记忆系统测试
pytest test/test_memory.py test/test_memory_manager.py test/test_phase5_l1.py -v

# 代码格式化 / 检查
ruff format .
ruff check .

# 启动 PostgreSQL + pgvector
docker run -d --name bangumi-pg \
  -e POSTGRES_USER=myuser -e POSTGRES_PASSWORD=mypassword \
  -e POSTGRES_DB=bangumidb -p 5432:5432 pgvector/pgvector:pg16

# RAG 语料管理（Phase 1: ID 发现 → Phase 2: 富化 + 灌入）
python -m rag.cli.discover
python -m rag.cli.ingest --clear

# RAG 离线语料准备 + 灌库自检（不产出指标，故不属于 eval/）
python -m rag.cli.collect          # 抓语料 → rag/corpus/{raw,processed}/
python -m rag.cli.verify --dry-run # 校验语料格式

# RAG 评测管线（--build 生成 GT + 标注模板，--evaluate 检索 + 计算指标）
python -m eval.rag_eval --build
python -m eval.rag_eval --evaluate

# RAG 数据库迁移
python scripts/migrate_subject_type.py

# 冒烟请求
curl -s -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "你好，最近有什么好看的番？", "depth": "fast", "output_style": "bangumi"}'
```

## 3. 架构

### 异质拓扑

```
START → classify_node ─┬─ chat ───────────────────────────────→ END
                        ├─ fetch → fetch_pipeline ─────────────────→ END
                        │           (search → tool → detail → tool → synthesize)
                        ├─ realtime → realtime_pipeline ──────────→ END
                        │              (search → tool → synthesize)
                        ├─ profile → profile_pipeline ────────────→ END
                        │             (search → tool → synthesize)
                        └─ explore/discuss/fallback/低置信度
                             → reasoning_node ⇄ tool_node → END
```

- **Pipeline intents**（fetch/realtime/profile）：编译时确定性步骤，每个 pipeline 封装为独立 subgraph，内部节点 + 专属 prompt + 专属工具集
- **ReAct intents**（explore/discuss/fallback）：运行时 LLM 自主探索，**隐式终止**——输出文本（无 tool_calls）= END，无显式终止工具
- **Chat**：直通 END，main.py 直接 render
- **Render 在 graph 之外**：main.py 后处理阶段调用独立 LLM（render node）

### 路由规则

| 路由函数 | 规则 |
|---------|------|
| `route_after_classify` | chat → END；置信度 < 0.7 → reasoning_node（ReAct 兜底）；pipeline intent → 对应子图；explore/discuss/fallback → reasoning_node |
| `route_after_tool` | pipeline → 按阶段步进；硬熔断（iterations ≥ max）→ END；连续 2 次空搜索 → END；重复调用 → END；ReAct → reasoning_node |
| `route_after_reasoning` | AIMessage 含 tool_calls → tool_node；其他 → END（隐式终止） |

### 迭代上限与工具子集

定义位置：`agent/config.py`（`_INTENT_MAX_ITERATIONS` + `_INTENT_DEEP_OVERRIDES`）。

| intent | fast | deep | 工具子集 |
|--------|------|------|---------|
| chat | 0 | 0 | — |
| fetch | 3 | 3 | search, detail, person, character |
| explore | 3 | 5 | fetch + opinions, characters, episodes, trending, local_search |
| discuss | 4 | 6 | explore + entity_comments, episode_comments |
| realtime | 2 | 2 | calendar, trending, hot_topics |
| profile | 2 | 2 | user_profile, user_timeline |
| fallback | 2 | 2 | 同 fetch |

置信度阈值：`_PIPELINE_CONFIDENCE_THRESHOLD = 0.7`。

### 四层架构

| 层 | 职责 | 核心文件 |
|---|------|---------|
| **编排层** | StateGraph 拓扑、路由、意图分类、prompt 装配、护栏 | `agent/graph.py`, `agent/config.py`, `agent/state.py`, `agent/nodes/`, `agent/routing/`, `agent/prompts/`, `agent/guardrails.py`, `agent/helpers.py` |
| **人格层** | CharacterProfile 定义 + Render 风格转换 | `agent/persona/` |
| **记忆层** | L1 滑动窗口 + 压缩 + L2 语义召回 + session 缓存 | `agent/memory/` |
| **数据层** | 工具函数 + HTTP Client + RAG + pgvector | `tools/`, `clients/`, `rag/`, `database/`, `schemas/` |

**核心契约：上层依赖下层，下层完全不感知上层。**

### 人格两层管线

```
Character Card (System Prompt) → 决定 agent 怎么思考（WHAT to think）
Render Node (独立 LLM 调用)    → 决定输出怎么表达（HOW to say it）
```

- 3 个活跃人格注册于 `CHARACTER_REGISTRY`：`bangumi`（腹黑吐槽损友，默认）/ `bangumi_kawaii`（可爱分享者，fast 字数覆盖 250）/ `neutral`（中性）
- AgentProfile 只有 `companion`（`dialogue`/`research` 是兼容别名）
- 可调参数：`snark`（5 档）、`depth_taste`（注入 Aggregator 搜索深度指令）、`initiative`
- 字数：`_WORD_LIMIT` fast 200 / deep 350 字 + 硬截断 280/480；`RENDER_TEMPERATURE = 0.4`

**修改人格行为时必须同时检查两层**——改一个不改另一个会导致人格表达断裂。

### 记忆系统

- **L1**（`memory/short_term.py`）：按 depth 两级 Token 预算（fast 10000 / deep 16000 tok，tiktoken 精确计数）。SystemMessage 永不截断。`manage_memory()` 流程：压缩历史工具结果 → 截断超大消息（单条上限 2000 tok）→ 滑动窗口 → 清理孤儿 ToolMessage
- **L2**（`memory/long_term.py`）：pgvector 语义召回（cosine_distance）+ 时间衰减（半衰期 14 天）。阈值：deep 0.5 / 非 deep 0.35；注入预算：deep 500 / 非 deep 300 tok
- **Cache**（`memory/cache.py`）：跨 HTTP 请求 session 缓存（fast 20 / deep 30 条消息）

### 工具与 RAG

- **16 个工具**：13 个无条件注册 + 3 个需 `BANGUMI_ACCESS_TOKEN`（get_user_profile / get_user_timeline / get_blog）。装配逻辑在 `tools/bgm_tools.py:get_agent_tools()`
- **RAG**：只使用 `RagEntity`（单表多态）。检索三通道：`hybrid_search`（向量语义）/ `name_search`（名称精确）/ `keyword_search`（标签/年份硬过滤 + 约束计数软补齐），生产入口是 `search_local_bangumi` 工具

## 4. 编码规则

### Critical — 绝对遵守

1. **不抛异常。** 所有 API/工具失败返回 `{"_error": "..."}` dict。Client 层通过 `BaseClient` 统一重试（429/502/503/Timeout，指数退避，429 优先取 Retry-After 头）。

2. **SystemMessage 不截断、不压缩、不参与滑动窗口预算竞争。** `manage_memory()` 中 SystemMessage 直接跳过；预算先扣除 system_tokens，剩余给对话消息。

3. **人格两层管线缺一不可。** 修改 `profiles.py`（Character Card）必须同步检查 `render.py`（Render Node），反之亦然。

4. **层间隔离。** 上层依赖下层，下层完全不感知上层。禁止在数据层引用 `AgentState`、在记忆层直接调用 Bangumi API、在人格层直接访问数据库。

### Convention — 默认遵守，除非有明确理由

5. **Pydantic v2**：`model_dump()` 而非 `dict()`；`json_schema_extra` 而非 `schema_extra`。

6. **`AgentState`** 使用 `TypedDict + Annotated[list, operator.add]`——消息在节点间追加而非覆盖。

7. **工具返回格式**：全部工具返回结构化 `dict`（A/B/C/D 字段方法论）。`search_local_bangumi` 返回 `{"results": [...], "total": N}`（无结果/出错时 `{"_error": ...}`）。新增工具必须遵循 dict 返回约定。

### Deprecated — 禁止使用或新增引用

8. **Critic 节点**已删除（Phase 4 移出 graph，2026-08-16 连文件一并删除）。如需恢复，从 git 历史找回 `agent/nodes/critic.py`，在 `graph.py` 重新注册节点并添加路由规则。

9. **L3 记忆**已废弃。`MEMORY_MIN_SESSIONS_FOR_PROFILE` 为零消费者配置项，不要引用。

10. **RAG**：`BangumiChunk` 已废弃，不要引入新引用。

11. **向量索引**：不要创建 2000d 以上的向量索引（pgvector 上限）。当前 embedding-2 (1024d) 已规避。

## 5. 文件地图

```
agent/
├── state.py                       # AgentState TypedDict（纯 schema）
├── config.py                      # 迭代上限、置信度阈值等运行时配置
├── graph.py                       # 异质拓扑 StateGraph + Pipeline Subgraph 封装
├── llm.py                         # LLM 工厂（ChatOpenAI / AzureChatOpenAI）
├── devtools.py                    # Token 统计 + 节点计时（DEV_MODE）
├── guardrails.py                  # XML 泄漏 / 重复调用检测 / 错误格式化
├── helpers.py                     # 共享辅助（extract_user_input, recall_memory, build_message_list）
├── nodes/                         # LangGraph 节点实现
│   ├── classify.py                # classify_node + 7 intent 分类器 + 置信度路由
│   ├── pipeline.py                # 5 个 pipeline 节点（fetch×2/realtime/profile/synthesize）
│   └── reasoning.py               # reasoning_node（ReAct）+ 消化态检测
├── routing/routes.py              # route_after_classify / _tool / _reasoning + 空搜索检测
├── prompts/                       # Prompt 模板与工具配置
│   ├── aggregator.py              # build_aggregator_prompt + 身份/终止规则/深度指令
│   ├── pipeline.py                # 各 pipeline 节点的专属 prompt
│   ├── scene_hints.py             # 浅层意图场景提示（COMPANION_SCENE_HINTS）
│   ├── scene_hints_deep.py        # 深度意图场景提示
│   └── tool_config.py             # TOOLS_BY_INTENT + TOOL_GUIDANCE
├── persona/
│   ├── profiles.py                # CharacterProfile + AgentProfile + CHARACTER_REGISTRY
│   └── render.py                  # Render Node — 字数限制 + 风格微调
└── memory/
    ├── short_term.py              # L1 滑动窗口 + 工具压缩 + SystemMessage 免疫
    ├── long_term.py               # L2 语义召回 + 时间衰减
    └── cache.py                   # Session 缓存（跨 HTTP 请求）
tools/bgm_tools.py                 # 16 个 LangChain @tool 函数 + get_agent_tools
clients/                           # HTTP 客户端（BaseClient 重试 + sanitizers + zhipu）
rag/                               # RAG 检索管线
│   ├── __init__.py                  # Public API re-exports
│   ├── enricher.py                  # API 数据富化（SubjectCollector/Character/Person）
│   ├── ingestion.py                 # 向量化 + pgvector 灌入（RagEntityIngestor）
│   ├── retriever.py                 # 三通道检索（hybrid / name / keyword）
│   ├── cli/                         # python -m rag.cli.{discover,ingest,collect,verify}
│   └── corpus/                      # 离线语料（collect 产出 / verify 消费），非评测资产
database/                           # SQLModel ORM + pgvector
schemas/tools_input.py              # Pydantic v2 工具输入 schema
core/config.py                      # pydantic-settings 全局配置（.env）
main.py                             # FastAPI 入口（/health, /chat, /chat/stream）
eval/                               # 全部离线评测（只放"测量"；现状见 eval/README.md）
scripts/                            # migrate_subject_type 等数据库迁移
test/                               # 539 测试 / 20 文件
docs/                               # design/（设计决策与愿景）、eval/、memory/、Rag/、Tools/
```

## 6. 调参速查

| 效果 | 文件 | 改什么 |
|------|------|--------|
| 回复太长/太短 | `persona/render.py` | `_WORD_LIMIT` / `_HARD_CUTOFF_MAX_CHARS` |
| 吐槽太狠/太温和 | `persona/profiles.py` | 角色 `snark` 值或 `_SNARK_LEVELS` 文本 |
| 分析太深/太浅 | `persona/profiles.py` | 角色 `depth_taste` 值（注入 Aggregator 深度指令） |
| AI 调了太多轮工具 | `agent/config.py` | `_INTENT_MAX_ITERATIONS` / `_INTENT_DEEP_OVERRIDES` |
| 多轮对话丢上下文 | `memory/short_term.py` | `DEPTH_TOKEN_BUDGETS` |
| 忘了之前聊过什么 | `core/config.py` | `MEMORY_*` 阈值与预算 |
| Render 太保守/太放飞 | `persona/render.py` | `RENDER_TEMPERATURE` |
| Deep 模式不调工具 | `prompts/tool_config.py` | `TOOL_GUIDANCE` + deep 场景提示 |
| 常识问题误调工具 | `nodes/classify.py` | 分类 prompt 或置信度阈值 |
| 切换人格 | 请求参数 | `output_style="bangumi_kawaii"` |

## 7. 已知问题

当前问题的完整追踪见 [`ROADMAP.md`](ROADMAP.md)（根目录）待解决章节（P0 字数控制、deep 0 工具调用、常识误分类等）。注意：ROADMAP 部分条目滞后于代码（如人格数量、`orchestrate/` 旧路径、版本号），遇到冲突以本手册和代码为准。
