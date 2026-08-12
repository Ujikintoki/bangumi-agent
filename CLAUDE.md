# CLAUDE.md

> BGM Agent 操作手册 — Claude Code 修改此代码库时的参考。
> 最后更新: 2026-08-05

## 1. Project Context

你正在开发 **BGM Agent**，一个部署在 [bangumi.tv](https://bgm.tv) 站内的 AI 聊天角色。两个入口参数控制一切：

| 参数 | 值 | 说明 |
|------|-----|------|
| `depth` | `"fast"` / `"deep"` | 控制推理深度和 Token 预算 |
| `output_style` | `"bangumi"` / `"bangumi_kawaii"` / `"neutral"` | 控制人格 |

技术栈：**FastAPI + LangGraph 异质拓扑（Pipeline + ReAct）+ DeepSeek function-calling + PostgreSQL/pgvector + Zhipu embedding-2**。

## 2. Architecture

### 异质拓扑

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

### 路由规则

| 路由函数 | 规则 |
|---------|------|
| `route_after_classify` | chat → END；pipeline intent → 对应入口节点；ReAct intent / 低置信度(<0.7) → reasoning_node |
| `route_after_tool` | pipeline → 按阶段步进；硬熔断(iterations>=max) → END；连续空搜索 → END；重复调用 → END；ReAct → reasoning_node |
| `route_after_reasoning` | AIMessage 含 tool_calls → tool_node；其他 → END（隐式终止） |

### 迭代上限

| intent | fast | deep | 工具子集 |
|--------|------|------|---------|
| chat | 0 | 0 | — |
| fetch | 3 | 3 | search, detail, person, character |
| explore | 3 | 5 | fetch + opinions, characters, episodes, trending, local_search |
| discuss | 4 | 6 | explore + entity_comments, episode_comments |
| realtime | 2 | 2 | calendar, trending, hot_topics |
| profile | 2 | 2 | user_profile, user_timeline |
| fallback | 2 | 2 | 同 fetch |

定义位置：`agent/state.py` `_INTENT_MAX_ITERATIONS` + `_INTENT_DEEP_OVERRIDES`。

### 四层架构

| 层 | 职责 | 核心文件 |
|---|------|---------|
| **编排层** | StateGraph 拓扑、路由、意图分类、策略、护栏 | `agent/graph.py`, `agent/config.py`, `agent/state.py`, `agent/nodes/`, `agent/routing/`, `agent/prompts/`, `agent/guardrails.py`, `agent/helpers.py` |
| **人格层** | CharacterProfile 定义 + Render 风格转换 | `agent/persona/` |
| **记忆层** | L1 滑动窗口 + 压缩 + L2 语义召回 + session 缓存 | `agent/memory/` |
| **数据层** | 工具函数 + HTTP Client + RAG + pgvector | `tools/`, `clients/`, `rag/`, `database/`, `schemas/` |

**核心契约：上层依赖下层，下层完全不感知上层。**

### 人格两层管线

```
Character Card (System Prompt) → 决定 agent 怎么思考（WHAT to think）
Render Node (独立 LLM 调用)   → 决定输出怎么表达（HOW to say it）
```

**修改人格行为时必须同时检查两层**——改一个不改另一个会导致人格表达断裂。

### 记忆系统

- **L1**（`memory/short_term.py`）：按 depth 两级 Token 预算（fast 10000 / deep 16000 tok）。SystemMessage 永不截断。`manage_memory()` 流程：压缩历史工具结果 → 截断超大消息 → 滑动窗口 → 清理孤儿 ToolMessage
- **L2**（`memory/long_term.py`）：双通道语义召回（cosine_distance）+ 时效回退。时间衰减半衰期 14 天。阈值：deep 模式 0.5，非 deep 模式 0.35
- **Cache**（`memory/cache.py`）：跨 HTTP 请求 session 缓存

## 3. Coding Rules

### Critical — 绝对遵守

1. **不抛异常。** 所有 API/工具失败返回 `{"_error": "..."}` dict。Client 层通过 `BaseClient` 统一处理重试（429/502/503/TimeoutException，指数退避，最多 3 次）。

2. **SystemMessage 不截断、不压缩、不参与滑动窗口预算竞争。** `manage_memory()` 中 SystemMessage 直接跳过；`trim_messages()` 中 system_msgs 不参与预算竞争——预算先扣除 system_tokens，剩余给对话消息。

3. **人格两层管线缺一不可。** 修改 `profiles.py`（Character Card）必须同步检查 `render.py`（Render Node）。反之亦然。单侧修改会导致人格表达断裂。

4. **层间隔离。** 上层依赖下层，下层完全不感知上层。NOT: 在数据层引用 `AgentState`、在记忆层直接调用 Bangumi API、在人格层直接访问数据库。

### Convention — 默认遵守，除非有明确理由

5. **Pydantic v2**：`model_dump()` NOT `dict()`；`json_schema_extra` NOT `schema_extra`。

6. **`AgentState`** 使用 `TypedDict + Annotated[list, operator.add]`——消息在节点间追加而非覆盖。

7. **工具返回格式**：绝大多数工具返回结构化 `dict`（A/B/C/D 字段方法论）。唯一例外：`search_local_bangumi` 返回 `str`。新增工具必须遵循 dict 返回约定。

### Deprecated — 禁止使用或新增引用

8. **Critic 节点** 已从 graph 中移除（Phase 4）。如需恢复，在 `graph.py` 中重新注册节点并添加路由规则。

9. **L3 记忆** 已废弃。`MEMORY_MIN_SESSIONS_FOR_PROFILE` 为零消费者配置项，不要引用。

10. **RAG**：只使用 `RagEntity`。`BangumiChunk` 已废弃，不要引入新引用。

11. **向量索引**：不要创建 2000d 以上的向量索引（pgvector 上限）。当前 embedding-2 (1024d) 已规避。

## 4. File Map

```
agent/
├── state.py                       # AgentState TypedDict（纯 schema）
├── config.py                      # 迭代上限、置信度阈值等运行时配置
├── graph.py                       # 异质拓扑 StateGraph + Pipeline Subgraph 封装
├── llm.py                         # LLM 工厂（多 Provider）
├── devtools.py                    # Token 统计 + 节点计时（DEV_MODE）
├── guardrails.py                  # 终端检测 / XML 泄漏 / 重复调用检测 / 错误格式化
├── helpers.py                     # 共享辅助函数（extract_user_input, recall_memory, build_message_list）
├── nodes/                         # LangGraph 节点实现
│   ├── classify.py                # classify_node + 7 intent LLM 分类器 + 置信度路由
│   ├── pipeline.py                # _pipeline_step + 5 个 pipeline 节点（fetch/realtime/profile/synthesize）
│   ├── reasoning.py               # reasoning_node（ReAct）+ 消化态检测
│   └── critic.py                  # [DEPRECATED] critic_node 保留以备恢复
├── routing/                       # 条件边路由函数
│   └── routes.py                  # route_after_classify / _tool / _reasoning + 空搜索检测
├── prompts/                       # Prompt 模板与工具配置
│   ├── aggregator.py              # build_aggregator_prompt + 身份/终止规则/few-shot
│   ├── pipeline.py                # 各 pipeline 节点的专属 prompt
│   ├── scene_hints.py             # 浅层意图场景提示（COMPANION_SCENE_HINTS）
│   ├── scene_hints_deep.py        # 深度意图场景提示 + CRITIC_SYSTEM_PROMPT
│   └── tool_config.py             # TOOLS_BY_INTENT + get_tool_choice + TOOL_GUIDANCE
├── persona/
│   ├── profiles.py                # CharacterProfile + Character Cards + 5 档离散参数
│   └── render.py                  # Render Node — per-personality voice hints + 风格微调
├── memory/
│   ├── short_term.py              # L1 滑动窗口 + 工具压缩 + SystemMessage 免疫
│   ├── long_term.py               # L2 语义召回 + 时间衰减
│   └── cache.py                   # Session 缓存（跨 HTTP 请求）
tools/bgm_tools.py                  # 16 个 LangChain @tool 函数
clients/                            # HTTP 客户端（httpx 异步 + 指数退避重试）+ sanitizers
rag/                                # RAG 检索管线
│   ├── __init__.py                  # Public API re-exports
│   ├── _utils.py                    # 内部共享工具（_clean_text, _first_sentence）
│   ├── enricher.py                  # API 数据富化（SubjectCollector, CharacterEnricher, PersonEnricher）
│   ├── ingestion.py                 # 向量化 + pgvector 灌入（RagEntityIngestor）
│   ├── retriever.py                 # 混合检索（RagEntityRetriever, RagSearchResult）
│   ├── cli/                         # RAG CLI 入口
│   │   ├── discover.py              # Phase 1: ID 发现（python -m rag.cli.discover）
│   │   └── ingest.py                # Phase 2: 富化 + 灌入（python -m rag.cli.ingest）
│   └── eval/                        # RAG 评测管线
│       ├── evaluate.py              # 评测主逻辑（--build / --evaluate）
│       ├── queries.py               # 查询定义（AUTO_QUERY_DEFS + POOLED_QUERIES）
│       └── artifacts/               # 评测产物（ground_truth_auto.json, pooled_annotate.json）
database/                           # SQLModel ORM + pgvector
schemas/tools_input.py              # Pydantic v2 工具输入 schema
core/config.py                      # pydantic-settings 全局配置
main.py                             # FastAPI 入口（/chat, /chat/stream）
test/                               # 534 测试 / 22 文件
```

## 5. Commands

```bash
# 启动开发服务器
uvicorn main:app --reload --port 8000

# 全部测试（需要 PostgreSQL + pgvector）
pytest test/ -v

# 跳过数据库依赖的测试
pytest test/ --ignore=test/test_rag.py -v

# 仅记忆系统测试
pytest test/test_memory.py test/test_memory_manager.py test/test_phase5_l1.py -v

# 代码格式化
ruff format .

# 代码检查
ruff check .

# 启动 PostgreSQL + pgvector
docker run -d --name bangumi-pg \
  -e POSTGRES_USER=myuser -e POSTGRES_PASSWORD=mypassword \
  -e POSTGRES_DB=bangumidb -p 5432:5432 pgvector/pgvector:pg16

# RAG 评测管线
python -m rag.eval.evaluate --build       # 生成 GT + 标注模板
python -m rag.eval.evaluate --evaluate    # 检索 + 计算指标

# RAG 语料管理
python -m rag.cli.discover                # Phase 1: ID 发现
python -m rag.cli.ingest --clear          # Phase 2: 清空 + 灌入

# RAG 数据库迁移
python scripts/migrate_subject_type.py

# 发测试请求
curl -s -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "你好，最近有什么好看的番？", "depth": "fast", "output_style": "bangumi"}'
```

## 6. Tuning Quick Reference

| 效果 | 文件 | 改什么 |
|------|------|--------|
| 回复太长/太短 | `persona/render.py` | `_WORD_LIMIT` dict |
| 吐槽太狠/太温和 | `persona/profiles.py` | 角色 `snark` 默认值，或 `_SNARK_LEVELS` 文本 |
| 分析太深/太浅 | `persona/profiles.py` | 角色 `depth_taste` 默认值 |
| AI 调了太多轮工具 | `state.py` | `_INTENT_MAX_ITERATIONS` |
| 多轮对话丢上下文 | `memory/short_term.py` | `DEPTH_TOKEN_BUDGETS` |
| 忘了之前聊过什么 | `core/config.py` | `MEMORY_*` 阈值 |
| Render 太保守/太放飞 | `persona/render.py` | `RENDER_TEMPERATURE` |
| Deep 模式不调工具 | `prompts/tool_config.py` | `TOOL_GUIDANCE` + deep 场景提示 |
| 常识问题误调工具 | `nodes/classify.py` | intent 分类规则 |
| 搜索空结果耗时过长 | `prompts/scene_hints.py` | 空结果处理策略 |
| 切换人格 | 请求参数 | `output_style="bangumi_kawaii"` |

## 7. Known Issues

> 已知问题的完整追踪和优先级见 [`docs/design/ROADMAP.md`](docs/design/ROADMAP.md) 待解决章节。
