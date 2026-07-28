# 架构状态 & 路线图

> 最后更新: 2026-07-28

## 当前状态快照

| 指标 | 值 |
|------|-----|
| Agent 入口 | 1 个（`depth` 参数控制深度: auto/quick/deep） |
| Graph 节点 | 5（reasoning + tool + critic + render + START/END；critic 保留注册但未路由） |
| 拓扑 | 纯 ReAct：reasoning ⇄ tool → render → END |
| 工具 | 16 个 LangChain `@tool`（13 无条件 + 3 token 门控），返回结构化 dict（A/B/C/D 方法论） |
| 记忆 | L1 滑动窗口 + 压缩 + SystemMessage 免疫（按 depth 三级预算：6000/10000/16000 tok）+ L2 语义召回（双通道 + 时间衰减），L3 废弃 |
| 人格 | 4 个角色（bangumi / bangumi_cold / bangumi_cute / neutral）+ 5 档离散人格参数 + Render 层 per-personality voice hints |
| 测试 | 573 passed + 23 skipped |

### 四层状态

| 层 | 文件 | 稳定 | 待解决 |
|---|------|------|--------|
| **编排层** | `orchestrate/nodes.py`, `state.py`, `orchestrate/strategies.py`, `orchestrate/classifier.py`, `orchestrate/guardrails.py`, `orchestrate/prompt_builder.py`, `orchestrate/helpers.py` | 🟡 刚稳定 | 2 项 |
| **人格层** | `persona/profiles.py`, `persona/render.py` | 🟡 活跃调参 | 1 项 |
| **记忆层** | `memory/short_term.py`, `memory/long_term.py`, `memory/cache.py` | ✅ 稳 | 1 项 |
| **数据层** | `clients/`, `tools/`, `rag/`, `database/`, `schemas/` | ✅ 稳 | 2 项 |

---

## 演化时间线

```
2026-05~06    06-09          07-21        06-17~07-22    07-25/26        07-27            07-27          07-27
Phase 1-3      Phase 4        Phase 5      Phase 5.5      Phase 6         Phase 6.5        Phase 8         Phase 9
地基            双 Agent       记忆          人格化          纠正错配         解耦风格          Context重构      人格深化
──■───────────■─────────────■────────────■──────────────■──────────────■───────────────■──────────────■────→
FastAPI        拆 Research   L1 滑动窗口   CharacterProfile 合并双 Agent    render_node      三级预算         Critic 屏蔽
BangumiClient  + Dialogue    L2 语义召回   AgentProfile      depth 参数      风格解耦          TOOL_GUIDANCE   5档离散人格
RAG + pgvector 引入 Critic   L3 废弃       角色优先          纯 ReAct 拓扑    极简 prompt      工具压缩         四种人格模式
第一个 ReAct    ← Tool Agent 错配开始 →                                                   SystemMsg免疫    Render重设计
```

**核心教训**：Phase 4 的双 Agent 是按 Tool Agent 心智模型（"深度链式"、"数据完整性优先"）设计的，
但产品定位是 Companion Agent（"查数据是为了聊天"）。Phase 6 纠正了拓扑错配，
Phase 6.5 纠正了输出风格错配，Phase 8 纠正了 Context 管理错配，Phase 9 深化了人格表达系统。

---

## 编排层

### 当前

纯 ReAct 拓扑：reasoning ⇄ tool → render → END。Critic 屏蔽——三种 depth 共享同一推理逻辑，差异仅在参数。

| 模式 | 迭代上限 | Critic | Token 预算 | 人格参数覆盖 | 工具策略 |
|------|---------|--------|-----------|-------------|---------|
| quick | 3 | 无 | 6000 | depth_taste=0.35, initiative=0.15 | 1 轮够用就停，最后一轮强制回复 |
| auto | 5 | 无 | 10000 | 角色默认值 | 1-2 轮，最后一轮强制回复 |
| deep | 12 | 无（屏蔽） | 16000 | depth_taste=0.90, initiative=0.85 | 高预算高迭代，消化态引导 |

**路由**：两级（tool_calls → tool_node，其他 → render_node → END）。

**depth 本质**：不是行为逻辑不同，是预算和人格参数不同。三种模式跑同一段 ReAct 代码。

**文件**：`agent/graph.py`, `agent/orchestrate/nodes.py`, `agent/state.py`, `agent/orchestrate/strategies.py`, `agent/orchestrate/deep_strategies.py`, `agent/orchestrate/prompt_builder.py`, `agent/orchestrate/classifier.py`, `agent/orchestrate/guardrails.py`, `agent/orchestrate/helpers.py`

### 待解决

| # | 问题 | 文件 | 改动量 |
|---|------|------|--------|
| 1 | Deep 模式偶发超出迭代上限（13-14 轮 vs max 12），无 Critic 兜底 | `orchestrate/deep_strategies.py` | 策略调整 |
| 2 | Bare title 仍直接搜而非先追问 | `orchestrate/strategies.py` + `persona/profiles.py` | ~10 行 |

---

## 人格层

### 当前

4 个 CharacterProfile + 5 档离散人格参数 + Render 层 per-personality voice hints。

**两层表达管线**：
```
Character Card (System Prompt) → 决定 agent 怎么思考（WHAT to think）
Render Node (独立 LLM 调用)   → 决定输出怎么表达（HOW to say it）
```

**四种人格模式**：

| key | 人格 | snark | depth_taste | initiative |
|-----|------|-------|-------------|------------|
| `bangumi` | 二次元损友 | 0.65 (L4) | 0.70 (L4) | 0.60 (L3) |
| `bangumi_cold` | 高冷腹黑评论家 | 0.95 (L5) | 0.90 (L5) | 0.25 (L2) |
| `bangumi_cute` | 可爱安利爱好者 | 0.15 (L1) | 0.50 (L3) | 0.65 (L4) |
| `neutral` | 中性助手 | 0.20 (L1) | 0.40 (L2) | 0.50 (L3) |

**5 档离散参数**（`_render_tone()` in `profiles.py`）：每维 5 段 prompt 文本，按阈值查找——档位增加不改变 System Prompt 长度。

**Render 层**（`agent/persona/render.py`）：per-personality voice hints（~50 chars）+ `_style_modifiers()` 按参数选 0-3 条微调规则。短闲聊（无工具 + <60 字）跳过 render。

### 待解决

| # | 问题 | 文件 | 改动量 |
|---|------|------|--------|
| 1 | bangumi_cold / bangumi_cute Character Card 措辞可进一步调优 | `persona/profiles.py` | ~20 行 |

---

## 记忆层

### 当前

L1 + L2 活跃，L3 废弃。

- **L1**：`agent/memory/short_term.py` — Phase 8 重构：
  - 按 depth 三级预算（quick 6000 / auto 10000 / deep 16000 tok）
  - SystemMessage 永不截断
  - 工具结果压缩（上一轮 ToolMessage → 关键字段摘要，2000→80 tokens）
  - 孤儿 ToolMessage 清理（防止 API 400 错误）
  - 管理入口 `manage_memory()`：压缩 → 截断超大 → 滑动窗口 → 清理孤儿
- **L2**：`agent/memory/long_term.py` — 双通道召回（语义 + 时效回退）+ 时间衰减
- **Session 缓存**：`agent/memory/cache.py` — 跨 HTTP 请求多轮上下文

### 待解决

| # | 问题 | 文件 | 改动量 |
|---|------|------|--------|
| 1 | `_memory_context` 空字符串缓存：`""` 是 falsy → 重复触发 embedding 调用 | `cache.py` | ~5 行 |

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

## 未来工作

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
| `MEMORY_DIALOGUE_*` (2 项) | 命名过时（继承 Phase 4 "Dialogue Agent"） | 改名 `MEMORY_QUICK_*` |
| `CRITIC_MODE="llm"` | 注释未说明 Critic 已屏蔽 | 更新注释 |
| `MEMORY_MIN_SESSIONS_FOR_PROFILE=5` | L3 废弃，零消费者 | 删除 |

### 记忆层：受益

- Group 分析结果走 `remember_public()` 写入 `public_memories`（表已建，索引已就绪）

### 人格层：可能的扩展

- 第三种自定义人格模式（如 "玩梗资历/老宅" otaku mode）
- 场景自适应人格切换（按 intent 自动选人格参数）
- Render prompt 进一步精简（当前 ~200-250 tokens，目标 ~150）

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
| [`docs/design/claude-on-bangumi-vision.md`](claude-on-bangumi-vision.md) | Claude on Bangumi 产品愿景（"是什么"） |
| [`docs/design/evolution-roadmap-phase7-9.md`](evolution-roadmap-phase7-9.md) | 分层演进路线 Phase 7-9（"怎么到那里"） |
| [`docs/memory/`](../memory/) | 记忆系统手册（6 文件） |
| [`docs/tool-guide.md`](../tool-guide.md) | 工具增/改/删操作指南 |
| [`docs/Tools/tools_file.md`](../Tools/tools_file.md) | 16 个工具设计详情 |
| [`docs/database-admin.md`](../database-admin.md) | PostgreSQL + pgvector 运维手册 |
| [`docs/Rag/`](../Rag/) | RAG 策略、表结构、上下文（3 文件） |
| [`docs/tmp/real_data_test.md`](../tmp/real_data_test.md) | Phase 5 测试数据基线 |
