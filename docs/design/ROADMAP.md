# 架构状态 & 路线图

> 最后更新: 2026-07-27

## 当前状态快照

| 指标 | 值 |
|------|-----|
| 测试 | 573 passed + 23 skipped |
| Agent 入口 | 1 个（`depth` 参数控制深度: auto/quick/deep） |
| Graph 节点 | 5（reasoning + tool + critic + render + START/END） |
| 工具 | 16 个 LangChain `@tool`（13 无条件 + 3 token 门控），返回结构化 dict（A/B/C/D 方法论） |
| 记忆 | L1 滑动窗口 + L2 语义召回（双通道 + 时间衰减），L3 废弃 |
| 人格 | 2 个角色（bangumi/neutral）+ Render 层风格转换 |

### 四层状态

| 层 | 文件 | 稳定 | 待解决 |
|---|------|------|--------|
| **编排层** | `orchestrate/nodes.py`, `state.py`, `orchestrate/strategies.py`, `orchestrate/classifier.py`, `orchestrate/guardrails.py`, `orchestrate/prompt_builder.py`, `orchestrate/helpers.py` | 🟡 刚稳定 | 3 项 |
| **人格层** | `persona/profiles.py`, `persona/render.py` | 🟡 活跃调参 | 1 项 |
| **记忆层** | `memory/short_term.py`, `memory/long_term.py`, `memory/cache.py` | ✅ 稳 | 1 项 |
| **数据层** | `clients/`, `tools/`, `rag/`, `database/`, `schemas/` | ✅ 稳 | 2 项 |

---

## 演化时间线

```
2026-05~06    06-09          07-21        06-17~07-22    07-25/26        07-27
Phase 1-3      Phase 4        Phase 5      Phase 5.5      Phase 6         Phase 6.5
地基            双 Agent       记忆          人格化          纠正错配         解耦风格
──■───────────■─────────────■────────────■──────────────■──────────────■────────→
FastAPI        拆 Research   L1 滑动窗口   CharacterProfile 合并双 Agent    render_node
BangumiClient  + Dialogue    L2 语义召回   AgentProfile      depth 参数      风格解耦
RAG + pgvector 引入 Critic   L3 废弃       角色优先          Critic 条件路由  极简 prompt
第一个 ReAct    ← Tool Agent 错配开始 →                                    四层架构清晰
```

**核心教训**：Phase 4 的双 Agent 是按 Tool Agent 心智模型（"深度链式"、"数据完整性优先"）设计的，
但产品定位是 Companion Agent（"查数据是为了聊天"）。Phase 6 纠正了拓扑错配，
Phase 6.5 纠正了输出风格错配。现在四层架构中，编排层不再预设"查数据是为了交报告"。

---

## 编排层

### 当前

5 节点 StateGraph：reasoning → tool → (条件 critic) → (条件 render) → END。

| 模式 | 迭代上限 | Critic | Render | 工具策略 |
|------|---------|--------|--------|---------|
| quick | 3 | 无 | 有工具时触发 | 1 轮 |
| auto | 5 | 无 | 有工具时触发 | 1-2 轮 |
| deep | 12 | 有 | 有工具时触发 | 深度链式 |

**路由**：五级优先级（tool_calls → chitchat → deep/critic → render → END）。

**文件**：`agent/graph.py`, `agent/orchestrate/nodes.py`, `agent/state.py`, `agent/orchestrate/strategies.py`, `agent/orchestrate/deep_strategies.py`, `agent/orchestrate/prompt_builder.py`, `agent/orchestrate/classifier.py`, `agent/orchestrate/guardrails.py`, `agent/orchestrate/helpers.py`

### 待解决

| # | 问题 | 文件 | 改动量 |
|---|------|------|--------|
| 1 | Deep 模式未充分触发链式调用（仅 1-2 轮 search，没走 detail） | `orchestrate/deep_strategies.py` | 策略调整 |
| 2 | Bare title 仍直接搜而非先追问 | `orchestrate/strategies.py` + `persona/profiles.py` | ~10 行 |
| 3 | Render 后历史中出现两条连续 AIMessage | `graph.py` 或 `persona/render.py` | 中等 |

---

## 人格层

### 当前

2 个 CharacterProfile（bangumi/neutral）+ Render 层风格转换。

**CharacterProfile**（`agent/persona/profiles.py`）：
- `BANGUMI_CHARACTER`：二次元损友——"让对话有趣"，"数据是吐槽的弹药"
- `NEUTRAL_CHARACTER`：中性助手——准确、简洁、可操作

**Render 层**（`agent/persona/render.py`）：
- 仅工具调用后触发，极简 prompt（~380 chars）
- 按 depth 分档字数：quick=120, auto=200, deep=350
- expression_guide（通用语气）与 _RENDER_STYLE（数据呈现）职责分离、无重叠

### 待解决

| # | 问题 | 文件 | 改动量 |
|---|------|------|--------|
| 1 | Neutral 风格 render 偏弱——仍可能罗列数据（`_RENDER_STYLE` 仅 2 条规则） | `persona/render.py` | ~5 行 |

---

## 记忆层

### 当前

L1 + L2 活跃，L3 废弃。

- **L1**：`agent/memory/short_term.py` — tiktoken 精确截断 + 滑动窗口
- **L2**：`agent/memory/long_term.py` — 双通道召回（语义 + 时效回退）+ 时间衰减
- **Session 缓存**：`agent/memory/cache.py` — 跨 HTTP 请求多轮上下文

### 待解决

| # | 问题 | 文件 | 改动量 |
|---|------|------|--------|
| 1 | 双套记忆阈值（Research/Dialogue）继承自 Phase 4 — 应合并为 depth 分支 | `config.py` + `memory/long_term.py` | 中等 |

---

## 数据层

### 当前

16 个工具 + BangumiClient + RAG + pgvector。自 dict 结构化重构后基本稳定。

**工具**（`tools/bgm_tools.py`）：search, get_detail, get_calendar, get_trending, get_hot_topics, get_episode_discussion, get_opinions, get_episodes, get_comments, get_characters, get_person_detail, get_character_detail, get_user_profile, get_blog, user_timeline, search_local_bangumi

**Client**（`clients/`）：BaseClient → BangumiClient → sanitizers（A/B/C/D 字段方法论）

### 待解决

| # | 问题 | 文件 | 改动量 |
|---|------|------|--------|
| 1 | RAG v0/v1 共存：deprecated `BangumiChunk` 与 `RagEntity` 并行 | `rag/` | 大 |
| 2 | HNSW index 创建失败（2048d，pgvector 上限 2000d） | `database/` | 中等 |

---

## 未来工作（原 Phase 7）

### 数据层：更多工具

| 功能 | 描述 | 改动量 |
|------|------|--------|
| 小组讨论抓取 | `get_group_topics(group_name, limit=20)` → 格式化讨论列表 | 新 API 端点 + 新工具 |
| 网页搜索 | `web_search(query, limit=5)` → Tavily / Bing Search | 新依赖 + 新工具 |
| 文本润色 | `polish_text(draft, style="spoiler_free")` → LLM 润色影评草稿 | 新工具（纯 LLM） |

### 编排层：配置清理

| 配置项 | 问题 | 建议 |
|--------|------|------|
| `LLM_TEMPERATURE=0.3` | Tool Agent 优化值，压制 Companion 人味 | 按 depth 分支：0.5-0.7 (auto), 0.3 (deep) |
| `MEMORY_MAX_INJECT_TOKENS=700` | 旧 Research Agent 默认值 | ~300 匹配 Companion 回复长度 |
| `MEMORY_DIALOGUE_*` (2 项) | 命名过时 | 改名 `MEMORY_QUICK_*` |
| `CRITIC_MODE="llm"` | 注释未说明仅 deep 生效 | 更新注释 |
| `MEMORY_MIN_SESSIONS_FOR_PROFILE=5` | L3 废弃，零消费者 | 删除 |

### 记忆层：受益

- Group 分析结果走 `remember_public()` 写入 `public_memories`（表已建，索引已就绪）

---

## 设计文档索引

| 文档 | 内容 |
|------|------|
| [`CLAUDE.md`](../../CLAUDE.md) | 项目架构、四层详解、调参速查、编码规范 |
| [`docs/design/ROADMAP.md`](ROADMAP.md) | 本文档 — 架构状态 & 路线图 |
| [`docs/design/architecture-review-2026-07-22.md`](architecture-review-2026-07-22.md) | 宏观架构 review（Phase 6 重构的决策依据） |
| [`docs/design/phase1-3-audit.md`](phase1-3-audit.md) | Phase 1-3 地基与 Companion Agent 兼容性审查 |
| [`docs/design/phase5-memory-system-design.md`](phase5-memory-system-design.md) | Phase 5 记忆系统完整设计 |
| [`docs/design/data-layer-redesign-discussion.md`](data-layer-redesign-discussion.md) | 工具层 str→dict 迁移决策过程 |
| [`docs/design/bangumi-api-schema-methodology.md`](bangumi-api-schema-methodology.md) | A/B/C/D 字段决策方法论 |
| [`docs/memory/`](../memory/) | 记忆系统手册（6 文件） |
| [`docs/tool-guide.md`](../tool-guide.md) | 工具增/改/删操作指南 |
| [`docs/Tools/tools_file.md`](../Tools/tools_file.md) | 16 个工具设计详情 |
| [`docs/database-admin.md`](../database-admin.md) | PostgreSQL + pgvector 运维手册 |
| [`docs/Rag/`](../Rag/) | RAG 策略、表结构、上下文（3 文件） |
| [`docs/tmp/real_data_test.md`](../tmp/real_data_test.md) | Phase 5 测试数据基线 |
