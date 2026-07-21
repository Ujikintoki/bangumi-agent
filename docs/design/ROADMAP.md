# 开发路线图

> 最后更新: 2026-07-21 | 当前阶段: Phase 5 完成（L3 已移除），Phase 5.5 Lite 落地

---

## 当前状态快照

| 指标 | 值 |
|------|-----|
| 总测试数 | 503 passed + 23 skipped (L3 deprecated) |
| 记忆相关测试 | L1: ~30, L2: ~9 (L3: 23 skipped) |
| Agent 数 | 2 (Research + Dialogue) |
| 工具数 | 14（含 get_character_detail, get_person_detail） |
| 工具链深度 | Research 2-8 轮, Dialogue 1-3 轮 |
| 记忆层级 | 2 活跃 (L1 滑动窗口, L2 语义召回) + 1 废弃 (L3 用户画像) |
| 配置项 | 12 个 MEMORY_* 配置 |
| output_style | 四象限可用 (neutral/bangumi × research/dialogue) |

---

## 总体路线

```
Phase 4 (done)       Phase 5 (done)         Phase 5.5 (done)          Phase 6
双 Agent              记忆系统               Output Boundary           更多工具
                       │                     │                        │
  research        双通道语义召回          prompt 人格剥离          group topics
  + dialogue      时间衰减排序            styles.py 四象限         web_search
  + 14 tools      锚定回退               neutral/bangumi          发帖辅助
                       │                     │                        │
                  L2: session 记忆        agent × style             记忆层受益
                  L3: 已废弃              四象限可用                 输出边界受益
```

**依赖关系**：记忆层 →（Output Boundary、更多工具并行）

---

## Phase 5: 记忆系统 ✅ 已完成（2026-07-21 更新）

> 详细设计方案见 [`docs/design/phase5-memory-system-design.md`](phase5-memory-system-design.md)（1194 行）  
> 综合手册见 [`docs/memory/`](../memory/README.md)（6 文件）  
> 实现文件：`agent/memory.py` (L1, 380 行) + `agent/memory_manager.py` (L2, 1015 行)  
> 辅助文件：`agent/session_cache.py` (194 行) + `agent/guardrails.py` (共享)  
> 数据库：`database/memory_tables.py` (3 张表：session_memories, user_profiles, public_memories)  
> 配置：`core/config.py` 中 12 个 MEMORY_* 项

### 当前架构

| 层级 | 状态 | 实现 | 存储 |
|------|------|------|------|
| L1 短记忆 | ✅ 活跃 | `agent/memory.py` — 滑动窗口 + tiktoken 精确截断 + 孤儿消息清理 | 内存 |
| L2 长记忆 | ✅ 活跃 | `agent/memory_manager.py` — 跨 session 语义召回 + 时间衰减 | PostgreSQL + pgvector |
| L3 用户画像 | 🗑️ 废弃 | `agent/memory_manager.py` — 增量更新偏好/亲和度（代码保留，调用点注释） | PostgreSQL JSONB（表未删） |

**L3 移除理由**（2026-07-20）：画像推断（关键词匹配 9 个类型→实体名）基本不工作；娱乐型对话对话 Agent，"偏好/机战类作品"的边际价值极低；L2 语义召回已提供跨 session 连续性。详见 plan 文件。

### L2 召回策略：双通道 + 时间衰减 + Agent 差异化

**通道 1: 语义通道**
```
pgvector cosine_distance(query_embedding, session_embedding)
  → 过滤: distance ≤ threshold (Research: 0.50, Dialogue: 0.35)
  → 评分: combined_score = (1 - distance) × 0.5^(days_ago / 14)
```

**通道 2: 时效回退**（语义命中不足 TOP_K 时触发）
```
按 created_at DESC 取最近 session
  → 计算 cosine_distance
  → 锚定过滤: distance ≤ 0.60 (MEMORY_RECENCY_FALLBACK_THRESHOLD)
  → 同样时间衰减评分
```

**Agent 差异化**：

| | Research | Dialogue |
|---|---|---|
| L2 注入预算 | 700 tokens | 300 tokens |
| 语义阈值 | cos ≤ 0.50 | cos ≤ 0.35 |
| 跳过意图 | chitchat, factual | chitchat, factual |
| 召回时机 | reasoning_node 首轮（`_memory_context` 缓存） | 同 Research |
| 最后轮保护 | Critic REVISE + 12 轮熔断 | iter ≥ 3 → 强制解绑工具 + 注入紧急指令 |

### 写入路径

```
对话历史（Human + AI，跳过 Tool/System）
  → 截断到 3000 tokens
  → DeepSeek 生成 ~200 字 JSON 摘要 {"summary": "...", "entities": [...]}
  → Zhipu embedding-3 向量化 (2048d)
  → UPSERT session_memories（同 user+session 只保留最新一条）
  → ~~_update_user_profile()~~ [L3 deprecated, 已跳过]
  
  全程 fire-and-forget（asyncio.create_task），15 秒硬超时，异常静默降级
```

### 优雅降级矩阵

| 故障点 | 降级行为 | 用户体验 |
|--------|---------|---------|
| embedding API 超时/失败 | embedding=None，回退纯时效排序 | 近期记忆仍可用 |
| 语义检索 DB 异常 | RuntimeError 捕获，scored=[] | 无记忆，agent 正常回复 |
| 摘要 LLM 失败 | 回退 `final_reply[:200]` | 摘要质量略降 |
| session_memory INSERT 失败 | SQLAlchemyError 捕获，skip | 本轮不记，下轮不受影响 |
| 画像更新（已禁用） | — | — |
| `MEMORY_ENABLED=False` | recall/remember 全部返回 no-op | Agent 退化回无记忆模式 |
| `user_id="anonymous"` | recall/remember 全部返回 no-op | 匿名用户不触发记忆 |

### Phase 5 完成标准 ✅
- ✅ session_id 不同 → 记忆隔离
- ✅ user_id 相同 → 跨 session 语义召回历史摘要
- ✅ 双通道召回：语义 + 时效回退（最小语义锚定 cos ≤ 0.60）
- ✅ 时间衰减排序：`combined_score = (1-cos_dist) × 0.5^(days/14)`
- ✅ Fire-and-forget 写入，异常静默降级
- ✅ Research + Dialogue 双 Agent 记忆集成，独立阈值
- ✅ Session 缓存（跨 HTTP 消息桥接）
- ✅ Dialogue 最后一轮强制回复（防止熔断无输出）
- ✅ ~~用户画像增量更新~~ [L3 deprecated]
- ✅ 503 tests 通过 + 23 skipped (L3)

### 与原始计划的关键差异

| 项目 | 原始计划 | 实际实现 |
|------|---------|---------|
| L2 召回 | 单一语义通道 | **双通道**（语义 + 时效回退） |
| L2 排序 | cosine 距离 | **时间衰减** `similarity × 0.5^(days/14)` |
| L3 画像 | 增量更新 | **已移除**（代码保留，调用点注释） |
| Dialogue L2 | 未计划独立配置 | **独立阈值 0.35** + 300 tokens 预算 |
| Dialogue 熔断 | 直接 END | **最后轮强制解绑工具** + 紧急指令注入 |
| Session 缓存 | 未计划 | `session_cache.py` (194 行) |
| Research L2 预算 | 500 tokens | **700** tokens |
| UPSERT 写入 | INSERT | **UPSERT**（防向量空间污染） |
| `_memory_context` 缓存 | 无 | **有**（同 graph 调用不复召回） |
| 测试 | — | 503 passed + 23 skipped |

---

## Phase 5.5: Output Style Control ✅ 完成（2026-06-17）

> **最终方案**：Prompt Appendix 注入（Lite 版），零额外 LLM 调用。  
> 原始计划的后处理 `render()` 六边形架构经测试不合理（额外延迟 + 数据编造风险），已废弃。  
> 废弃的设计文档保留在 [`docs/design/personality-rendering-layer.md`](personality-rendering-layer.md) 供历史参考。

### 实际架构

```
build_system_prompt() / build_dialogue_prompt()
  → BASE/CORE prompt（能力 + 策略，不含人格）
  → intent 变体
  → L2 memory context
  → style appendix（来自 agent/styles.py，"neutral" 时为空字符串）
  → LLM 单次推理完成"推理 + 风格化"
```

模型在一次推理中同时完成推理和风格表达。不需要二次 LLM 调用、不需要 diff 校验、不增加延迟。

### 实现文件

| 文件 | 角色 |
|------|------|
| `agent/styles.py` (99 行) | 风格定义 canonical source — 两个 dict，两个附录字符串 |
| `agent/dialogue/prompts.py` | `DIALOGUE_CORE_PROMPT`（去人格化能力描述）+ `build_dialogue_prompt(output_style=)` |
| `agent/research/prompts.py` | `build_system_prompt(output_style=)` — BASE → memory → intent → critic → style |
| `main.py` | `ChatRequest.output_style`, `ChatResponse.output_style`, `_resolve_output_style()`, `AGENT_DEFAULT_STYLES` |
| `agent/research/state.py` | `AgentState.output_style: str` |
| `agent/dialogue/state.py` | `DialogueState.output_style: str` |

### 两个风格注册表

| Dict | 使用者 | `"neutral"` | `"bangumi"` |
|------|--------|-------------|-------------|
| `STYLE_APPENDICES` | Dialogue | `""`（零 token） | 完整人格：腹黑萝莉 + 30-80 字限制 + 150 字工具上限 |
| `STYLE_APPENDICES_RESEARCH` | Research | `""`（零 token） | 软版本：同人格，**无字数限制**，强调数据完整性 |

关键差异：Dialogue Bangumi 含硬字数上限（闲聊 30-80，含工具 ≤150），Research Bangumi 无字数限制并显式声明"不要因为风格要求而缩减数据或跳过工具调用"。

### Core/Style 分离

**Dialogue 侧**：`DIALOGUE_CORE_PROMPT` (53 行) — 纯能力 + 工具策略 + 输出格式 + 对话连续性。不含 "Bangumi娘"、"腹黑萝莉"、"毒舌吐槽役"。人格全部在 `agent/styles.py` 的 `BANGUMI_STYLE_APPENDIX`。

**Research 侧**：`BASE_SYSTEM_PROMPT` — 能力 + 数据模型约束 + 对话连续性 + "回答风格：简洁、具体、可操作"（未完全剥离，已知偏差）。

### 风格决议逻辑

```
_resolve_output_style():
  if request.output_style is not None → 用显式值
  else → AGENT_DEFAULT_STYLES[agent_type]
    - dialogue → "bangumi"
    - research → "neutral"
```

### 设计决策：为什么 Lite > 完整版

| 维度 | Lite（实际） | 完整版 render()（废弃） |
|------|-------------|----------------------|
| LLM 调用增量 | **零** | +1 次/响应 (~500ms) |
| 数据编造风险 | **无** — 模型直接基于数据输出 | 需 diff 校验防止 render 编造评分/排名 |
| 延迟 | 无额外延迟 | dialogue <2s 预算被挤压 25%+ |
| 模块复杂度 | 1 文件 99 行 | `agent/personality/` 4 文件 |
| 四象限 | ✅ 全可用 | ✅ 全可用 |

### 测试覆盖

`test/test_prompts.py` — 7 个测试：
- `test_research_neutral_excludes_style` — neutral 无 "腹黑"/"吐槽"
- `test_research_bangumi_includes_style` — bangumi 有人格 + 无字数限制 + 数据完整性声明
- `test_dialogue_neutral_excludes_persona` — neutral 无人格，有核心能力
- `test_dialogue_bangumi_includes_persona` — bangumi 有人格 + 字数限制
- `test_dialogue_core_prompt_has_no_persona` — CORE 本身不含人格
- `test_style_registry_keys` — 两个 dict 都有 neutral→"" 和 bangumi→非空

### 已知偏差

1. `BASE_SYSTEM_PROMPT` 仍含 "回答风格：简洁、具体、可操作" — neutral 模式下的风格残留
2. `DIALOGUE_CORE_PROMPT` 第 34 行有 "你是吐槽役，不是论文写手" — CORE 中的轻微人格泄漏
3. `/chat/stream` 不会在 SSE 事件中报告 `output_style`
4. 原设计文档 `personality-rendering-layer.md` (347 行) 描述的是已废弃的六边形方案

---

## Phase 6: 更多工具 & 社区数据

### 目标
接入 Bangumi 小组讨论、网页搜索，支持辅助发帖/影评场景。

### 新增工具

#### 6.1: 小组讨论抓取 (`get_group_topics`)
- **新 API 端点**: Bangumi p1 `/p1/groups/{group_name}/topics`（需确认 Bangumi API 是否开放）
- **工具**: `get_group_topics(group_name, limit=20)` → 格式化讨论列表
- **Client 层**: `BangumiClient.get_group_topics()`
- **Schema**: `schemas/tools_input.py` 新增 `GetGroupTopicsInput`

#### 6.2: 讨论帖深度分析 (`analyze_topic_sentiment`)
- **工具**: 给定 topic_id，拉取正文 + 评论 → LLM 摘要社区观点
- 输出: 争议点、主流观点、热评摘要

#### 6.3: 网页搜索 (`web_search`)
- 接 Tavily / Bing Search API / Google Custom Search
- 工具: `web_search(query, limit=5)` → 返回标题+摘要 URL 列表
- **新的 Provider 配置**: `core/config.py` 新增 `SEARCH_API_KEY`, `SEARCH_PROVIDER`

#### 6.4: 文本润色 (`polish_text`)
- **工具**: `polish_text(draft, style="spoiler_free")` → LLM 润色
- 不调外部 API，纯 LLM 调用
- 场景: 用户草稿影评 → Agent 去剧透 + 优化表达

### Step 总结
- Step 6.1-6.4: 新增 4 个工具（Client + Tool + Schema 三位一体）
- Step 6.5: 记忆层受益 — group 分析结果走 `remember_public()` 写入公共记忆（`public_memories` 表已建，索引已就绪）
- Step 6.6: 输出边界受益 — 润色/吐槽内容自动走 `render(style="bangumi")` 人格化
- Step 6.7: 测试更新

---

## 紧急修复状态

> 以下问题在 [2026-06-10 边缘审计](./audit-2026-06-10.md) 中发现，部分已在 `a46b72c` 中修复。标注状态反映当前代码。

### ✅ 已修复

| # | 问题 | 修复方式 |
|---|------|---------|
| P0-1 | LLM 调用无超时 | `core/config.py` 新增 `LLM_REQUEST_TIMEOUT=60.0`，`agent/llm.py` 的 `create_llm()` 支持 `request_timeout` 参数 |
| P1-1 | Dialogue 无重复工具调用检测 | `agent/guardrails.py` 共享 `check_duplicate_tool_calls()`，Dialogue 已导入使用 |
| P1-2 | Dialogue 无逃逸舱 | `agent/guardrails.py` 共享 `is_terminal_response()`（12 条正则），Dialogue 消化态检测后调用 |
| P1-3 | Dialogue chitchat/factual 无 XML 安全网 | `agent/guardrails.py` 共享 `strip_tool_call_xml()`，Dialogue 回复前调用 |
| P2-2 | `_extract_final_reply` 兜底无区分度 | `main.py` 按异常类型返回不同兜底消息（超限/空回复/工具错误/通用异常） |
| P2-4 | tiktoken `encode()` 无 try/except | `agent/memory.py` 的 `count_tokens()` 含 try/except + 降级为 `len//4` 估算 |
| P3-3 | ToolNode `handle_tool_errors=True` 泄漏堆栈 | 改为 `handle_tool_errors=format_tool_error`（`agent/guardrails.py`），仅保留错误摘要 |

### 🟡 仍待修复

| # | 问题 | 修复位置 | 改动量 | 优先级 |
|---|------|---------|--------|--------|
| P0-2 | 分类器对短作品名误判 ("EVA", "K", "86") | `agent/classifier.py` — 短名优先走 LLM fallback | ~5 行 | 中 |
| P2-1 | messages 为空时路由读 `messages[-1]` 崩溃 | `graph.py` — 加空列表检查 | ~3 行 | 低 |
| P2-3 | Critic `< 20 字` 硬阈值边缘误伤 | `research/nodes.py` — 将阈值从 20 降至 12 | ~3 行 | 低 |
| P3-1 | RAG retriever 每次调用重建 | `tools/bgm_tools.py` — 单例化或连接复用 | ~10 行 | 低 |
| P3-2 | `create_llm()` 每次调用新建实例 | `agent/llm.py` — 加模块级缓存 | ~5 行 | 低 |

### ℹ️ 已知次要问题（非紧急修复列表）

| # | 问题 |
|---|------|
| - | 流式端点 `/chat/stream` 仅节点级，非逐 token 流 |
| - | `user_profiles` 表注释与配置不一致：docstring 说 `total_sessions >= 3` 注入画像，但 `MEMORY_MIN_SESSIONS_FOR_PROFILE` 默认为 5 |
| - | 记忆写入的摘要 LLM 无独立超时配置，复用 `request_timeout=10` |

---

## 涉及文件索引

| 文件 | 阶段 | 角色 |
|------|------|------|
| `agent/memory.py` | Phase 5 | L1 短记忆 — 滑动窗口 + 两层截断 |
| `agent/memory_manager.py` | Phase 5 | L2/L3 长记忆 — 召回 + 写入 + 画像 (931 行) |
| `agent/guardrails.py` | Phase 5 穿插 | 共享防御模块 — 终端检测 + XML 清洗 + 重复检测 + 错误格式化 |
| `database/memory_tables.py` | Phase 5 | ORM 模型 — session_memories + user_profiles + public_memories |
| `database/rag_tables.py` | Phase 5 穿插 | 重构拆分 — 旧 `models.py` 中 RAG 表移至此 |
| `clients/zhipu_client.py` | Phase 5 | 智谱 embedding 客户端 |
| `core/config.py` | Phase 5 | 10 个 MEMORY_* + LLM_REQUEST_TIMEOUT 配置 |
| `main.py` | Phase 5 | Fire-and-forget 写入调度 + 区分化兜底消息 |
| `agent/research/nodes.py` | Phase 5 | L2 记忆召回集成（首轮注入 System Prompt） |
| `agent/dialogue/nodes.py` | Phase 5 | L2 记忆召回集成 + 防御机制补全 |
| `agent/styles.py` | Phase 5.5 Lite | 风格注册表 — 两个 dict，neutral/bangumi 附录 |
| `agent/session_cache.py` | Phase 5 穿插 | 跨 HTTP 消息缓存 — TTL 1h, max 1000 session |
| `agent/dialogue/prompts.py` | Phase 5.5 Lite 已改 | CORE（能力+策略）+ 从 styles.py 注入人格附录 |
| `agent/research/prompts.py` | Phase 5.5 Lite 已改 | 风格指令已由 styles.py 附录替代 |

---

## 设计文档索引

| 文档 | 内容 |
|------|------|
| [`CLAUDE.md`](../../CLAUDE.md) | 项目架构、命令、约定、当前状态 |
| [`docs/design/phase5-memory-system-design.md`](phase5-memory-system-design.md) | Phase 5 完整设计方案（1194 行） |
| [`docs/design/personality-rendering-layer.md`](personality-rendering-layer.md) | Output Boundary 设计规范 — 六边形架构、render() 共享、四象限 |
| [`docs/design/ROADMAP.md`](ROADMAP.md) | 本文档 — 路线图 & 任务分解 |
| [`docs/memory/README.md`](../memory/README.md) | 记忆系统综合手册入口 |
| [`docs/memory/architecture.md`](../memory/architecture.md) | 三层记忆架构、数据流、模块关系 |
| [`docs/memory/implementation.md`](../memory/implementation.md) | 核心算法、代码路径、关键函数 |
| [`docs/memory/configuration.md`](../memory/configuration.md) | 配置项详解、调优指南 |
| [`docs/memory/testing.md`](../memory/testing.md) | 测试覆盖、运行方法、扩写指南 |
| [`docs/memory/debugging.md`](../memory/debugging.md) | 日志关键字、常见问题排查 |

---

## 新对话快速启动指南

在新的 Claude Code session 中，用以下提示启动工作：

**了解记忆系统：**
> 阅读 `CLAUDE.md` Phase 5 节和 `docs/design/ROADMAP.md`，理解 L1/L2 当前实现和 L3 废弃原因。

**启动 Phase 6：**
> 阅读 `docs/design/ROADMAP.md` Phase 6 节，从 `get_group_topics` 或 `web_search` 开始新增工具。

**修复已知问题：**
> 阅读 `CLAUDE.md` "当前已知问题"节，优先修 `_memory_context` 空字符串缓存失效（sentinel 值，~3 行）。
> 阅读 `docs/design/ROADMAP.md` 的 🟡 仍待修复节，先修 P0-2（短名误判）。

**了解项目全貌：**
> 阅读 `CLAUDE.md`，然后读取 `docs/design/` 目录下所有设计文档。
