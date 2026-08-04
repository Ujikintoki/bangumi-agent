# 架构状态 & 路线图

> 最后更新: 2026-08-04

## 当前状态快照

| 指标 | 值 |
|------|-----|
| Agent 入口 | 1 个（`depth` 参数控制深度: fast/deep） |
| Graph 节点 | 9（classify + fetch_search + fetch_detail + realtime_search + profile_search + synthesize + reasoning + tool + START/END） |
| 拓扑 | v5 异质：Pipeline（fetch/realtime/profile）+ ReAct（explore/discuss/fallback）+ chat 直通 |
| 工具 | 15 个 LangChain `@tool`（12 无条件 + 3 token 门控），返回结构化 dict。隐式终止，无 submit_facts |
| 记忆 | L1 滑动窗口 + 压缩 + SystemMessage 免疫（按 depth 两级预算：10000/16000 tok）+ L2 语义召回（双通道 + 时间衰减），L3 废弃 |
| 人格 | 4 个角色（bangumi / bangumi_cold / bangumi_cute / neutral）+ 5 档离散人格参数 + Render 层 per-personality voice hints |
| 测试 | 冒烟 18/18 pass；单元 ~570 pass |

### 四层状态

| 层 | 文件 | 稳定 | 待解决 |
|---|------|------|--------|
| **编排层** | `orchestrate/` 全部, `graph.py`, `state.py` | 🟡 刚稳定（Phase 4.1） | 4 项 |
| **人格层** | `persona/profiles.py`, `persona/render.py` | 🟡 活跃调参 | 1 项 |
| **记忆层** | `memory/short_term.py`, `memory/long_term.py`, `memory/cache.py` | ✅ 稳 | 2 项 |
| **数据层** | `clients/`, `tools/`, `rag/`, `database/`, `schemas/` | ✅ 稳 | 2 项 |

---

> 演化历史见 [`docs/design/architecture-evolution.md`](architecture-evolution.md)。

## 编排层

### 当前

v5 异质拓扑（Phase 4）：classify → 按 intent 分发。

```
START → classify_node ─┬── [chat] ──────────→ END
                         ├── [fetch] ─────────→ fetch_search → tool → fetch_detail → tool → synthesize → END
                         ├── [realtime] ──────→ realtime_search → tool → synthesize → END
                         ├── [profile] ───────→ profile_search → tool → synthesize → END
                         └── [explore|discuss|fallback] → reasoning_node ⇄ tool_node → END
```

- **Pipeline**（fetch/realtime/profile）：编译时确定性步骤，每步独立 LLM 节点 + 独立 prompt + 独立工具集
- **ReAct**（explore/discuss/fallback）：运行时 LLM 自主探索，隐式终止（输出文本 = END）
- **Chat**：直通 END，main.py 直接 render
- **置信度路由**：classifier confidence < 0.7 → ReAct fallback

| intent | 最大迭代 (fast) | 最大迭代 (deep) | 工具子集 |
|--------|----------------|----------------|---------|
| chat | 0 | 0 | — |
| fetch | 3 (search→detail→synthesize) | 3 | search, detail, person, character |
| explore | 3 | 5 | fetch + opinions, characters, episodes, trending, local_search |
| discuss | 4 | 6 | explore + entity_comments, episode_comments |
| realtime | 2 (search→synthesize) | 2 | calendar, trending, hot_topics |
| profile | 2 (search→synthesize) | 2 | user_profile, user_timeline |
| fallback | 2 | 2 | 同 fetch |

**隐式终止**（Phase 4.1）：无 submit_facts 工具。LLM 输出文本（no tool_calls）→ END。main.py 统一渲染路径，render 失败时降级清理（去 emoji/markdown）。

**文件**：`agent/graph.py`, `agent/orchestrate/nodes.py`, `agent/state.py`, `agent/orchestrate/strategies.py`, `agent/orchestrate/deep_strategies.py`, `agent/orchestrate/prompt_builder.py`, `agent/orchestrate/classifier.py`, `agent/orchestrate/guardrails.py`, `agent/orchestrate/helpers.py`

### 待解决

**P0 — 必须修（产品级 bug）**

| # | 问题 | 严重度 | 文件 | 改动量 |
|---|------|--------|------|--------|
| 1 | 字数控制形同虚设——fast 模式近半数回复超过 200 字限制 | 🔴 P0 | `persona/render.py` `_WORD_LIMIT` | prompt 强化或硬截断 |
| 2 | Deep 模式 ReAct intent 偶发 0 工具调用 | 🔴 P0 | `orchestrate/prompt_builder.py` | prompt 策略调整 |
| 3 | "今天星期几"等常识问题被误分类为 realtime | 🔴 P0 | `orchestrate/classifier.py` | 加纯常识判断 |

**P1 — 影响体验**

| # | 问题 | 严重度 | 文件 | 改动量 |
|---|------|--------|------|--------|
| 4 | 长程多轮（8 轮+）话题跳转后 R8 完全失忆 | 🟡 P1 | `memory/short_term.py` | fast 10000 tok 多话题不足 |
| 5 | Streaming 仅节点级（非逐 token） | 🟡 | `main.py` `/chat/stream` | 升级到 astream_events |

**P2 — 技术债**

| # | 问题 | 严重度 | 文件 | 改动量 |
|---|------|--------|------|--------|
| 6 | `_memory_context` 空字符串缓存 bug | 🟡 | `cache.py` | ~5 行 |
| 7 | 双套记忆阈值命名过时（`MEMORY_DIALOGUE_*`） | 🟢 | `core/config.py` | 重命名 |
| 8 | Bare title 不追问确认 | 🟢 | `orchestrate/strategies.py` | 追问策略 |
| 9 | `create_llm()` 无缓存 | 🟢 | `agent/llm.py` | 加 lru_cache |

**Phase 4-4.1 已解决：**
- ~~搜索不存在的条目跑满 5 轮~~ → pipeline fetch 3 步即停，硬熔断兜底
- ~~Render 消息重复~~ → main.py `_replace_last_ai_content` 替换而非追加
- ~~Deep 超迭代~~ → 硬熔断 `iterations >= max` 直接 END
- ~~submit_facts 显式终止反模式~~ → 彻底移除，统一隐式终止
- ~~多轮 session 返回空~~ → 测试脚本 `"depth": "auto"` → `"fast"` 修复
- ~~classifier fetch 过重~~ → "讲什么"/"配过什么" → explore，不确定时→explore

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

- **L1**：`agent/memory/short_term.py`：
  - 按 depth 两级预算（fast 10000 / deep 16000 tok）
  - SystemMessage 永不截断
  - 工具结果压缩（上一轮 ToolMessage → 关键字段摘要，2000→80 tokens）
  - 孤儿 ToolMessage 清理（防止 API 400 错误）
  - 管理入口 `manage_memory()`：压缩 → 截断超大 → 滑动窗口 → 清理孤儿
- **L2**：`agent/memory/long_term.py` — 双通道召回（语义 + 时效回退）+ 时间衰减
- **Session 缓存**：`agent/memory/cache.py` — 跨 HTTP 请求多轮上下文

### 待解决

| # | 问题 | 严重度 | 文件 | 改动量 |
|---|------|--------|------|--------|
| 1 | 长程多轮（8 轮+）话题跳转后 R8 完全失忆——auto 10000 tok 预算对多话题对话不足 | 🟡 P1 | `memory/short_term.py` | 预算策略或前 N 轮摘要注入 |
| 2 | `_memory_context` 空字符串缓存：`""` 是 falsy → 重复触发 embedding 调用 | 🟡 | `cache.py` | ~5 行，用 `is not None` 判断 |

---

## 数据层

### 当前

15 个工具 + BangumiClient + RAG + pgvector。Phase 4.1 移除 submit_facts_to_render（隐式终止替代）。

**工具**（`tools/bgm_tools.py`）：search, get_detail, get_calendar, get_trending, get_hot_topics, get_episode_comments, get_opinions, get_episodes, get_comments, get_characters, get_person_detail, get_character_detail, get_user_profile, get_blog, user_timeline, search_local_bangumi

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
| [`docs/Tools/tools_file.md`](../Tools/tools_file.md) | 15 个工具设计详情 |
| [`docs/database-admin.md`](../database-admin.md) | PostgreSQL + pgvector 运维手册 |
| [`docs/Rag/`](../Rag/) | RAG 策略、表结构、上下文（3 文件） |
| [`docs/tmp/real_data_test.md`](../tmp/real_data_test.md) | Phase 5 测试数据基线 |
