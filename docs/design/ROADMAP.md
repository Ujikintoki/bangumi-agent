# 开发路线图

> 最后更新: 2026-07-26 | 当前阶段: Phase 6 Companion Agent 架构重构（Step 1 完成）

---

## 当前状态快照

| 指标 | 值 |
|------|-----|
| 总测试数 | 565 passed + 23 skipped (L3 deprecated) |
| Agent 数 | 2 (Research + Dialogue) — **Phase 6 目标合并为 1 个 Companion Agent** |
| 工具数 | 14（含 get_character_detail, get_person_detail） |
| 工具返回格式 | dict（2026-07-24 str→dict 重构，A/B/C/D 方法论） |
| 工具链深度 | 默认 1-2 轮, deep 模式 3-8 轮 |
| 记忆层级 | 2 活跃 (L1 滑动窗口, L2 语义召回) + 1 废弃 (L3 用户画像) |
| 配置项 | 12 个 MEMORY_* 配置 |
| output_style | bangumi（默认）/ neutral |

---

## 总体路线

```
Phase 1-3 (done)    Phase 4 (done)    Phase 5 (done)     Phase 5.5 (done)    Phase 6 (进行中)     Phase 7 (重新定义)
基础地基             双 Agent 拓扑      记忆系统            Output Boundary     Companion Agent      更多工具 & 社区数据
     │                   │                 │                    │                   │                    │
  FastAPI            Research Agent   L1 滑动窗口         prompt 人格剥离      产品定位翻转          group topics
  BangumiClient      Dialogue Agent    L2 语义召回         profiles.py         单一 Agent 入口        web_search
  RAG 检索           14 tools          L3 已废弃           四象限可用           损友人格             发帖辅助
  pgvector           3 节点 ReAct      时间衰减排序         intent 体系扩展      depth 参数控制        记忆层受益
                     2 节点 ReAct      tone 侧写           depth="deep"→Skill  输出边界受益
```

**Phase 7 替代旧 ROADMAP 中的 Phase 6（"更多工具"），原计划推迟到 Companion Agent 重构完成之后。**

---

## Phase 1-3: 地基 ✅（2026-05 ~ 2026-06 初）

项目的早期基础层。无独立设计文档，但代码仍在生产使用中。

| Phase | 内容 | 关键文件 |
|-------|------|---------|
| Phase 1 | FastAPI 入口、配置系统、Bangumi API 客户端 | `main.py`, `core/config.py`, `clients/` |
| Phase 2 | RAG 检索（text_processor, ingestion, retriever）、工具函数初版 | `rag/`, `tools/`, `database/` |
| Phase 3 | 第一个 ReAct Agent 实现、Schema 定义 | `agent/`, `schemas/` |

**2026-07-26 审查结论**：这些层与 agent 拓扑完全解耦——Client、RAG、工具、数据库在新 Companion Agent 架构下不需要改动。详见 [`docs/design/phase1-3-audit.md`](phase1-3-audit.md)。

---

## Phase 4: 双 Agent 拓扑 ✅（2026-06-09）

建立 Research + Dialogue 两条独立的 Agent 管线，共享工具层和记忆层。

### 架构决策

| | Research Agent | Dialogue Agent |
|---|---|---|
| 定位 | 深度搜索助手 | 快速聊天（Bangumi娘） |
| 拓扑 | 3 节点（reasoning + tool + critic） | 2 节点（reasoning + tool） |
| 最大迭代 | 12 | 4 |
| 默认人格 | neutral | bangumi |
| Critic | llm（四维度） | 无 |
| Prompt 构建 | `agent/research/prompts.py` | `agent/dialogue/prompts.py` |
| State | `AgentState`（10 字段） | `DialogueState`（7 字段） |

### ReAct 变体

Tool-calling ReAct（非标准 ReAct）：LLM 通过 function calling 决定工具调用，无显式 Thought 输出。Research Agent 在 LLM 主动停止后触发 Critic 质量审查（PASS/REVISE）。

### 关键文件

| 文件 | 角色 |
|------|------|
| `agent/research/graph.py` | Research 图谱编排（条件边：tool / critic / END） |
| `agent/research/nodes.py` | reasoning_node + critic_node（rule/llm 双模式） |
| `agent/research/state.py` | AgentState（10 字段，_MAX_ITERATIONS=12） |
| `agent/research/prompts.py` | INTENT_PROMPTS + CRITIC_SYSTEM_PROMPT |
| `agent/dialogue/graph.py` | Dialogue 图谱编排（条件边：tool / END） |
| `agent/dialogue/nodes.py` | dialogue_reasoning_node（last-chance 熔断） |
| `agent/dialogue/state.py` | DialogueState（7 字段，_MAX_ITERATIONS=4） |
| `agent/dialogue/prompts.py` | build_dialogue_prompt() 薄封装 |
| `agent/classifier.py` | 8 意图 LLM 单阶段分类 |
| `agent/guardrails.py` | 终端回复检测 + XML 泄漏剥离 + 重复工具调用检测 |
| `agent/reasoning_core.py` | 共享辅助函数 |
| `main.py` | `agent_type` 路由 + `_chat_dialogue()` / `_chat_research()` |

### ⚠️ Phase 6 将重构

Phase 4 的双 Agent 拓扑将被合并为单一 Companion Agent（depth 参数控制深度）。Critic 仅 depth="deep" 时保留。`agent/dialogue/` 目录删除，文件合并到 `agent/` 根级。

---

## Phase 5: 记忆系统 ✅（2026-07-21）

L1 + L2 活跃。L3 用户画像 deprecated（2026-07-20）。

详见 [`docs/design/phase5-memory-system-design.md`](phase5-memory-system-design.md) 和 [`docs/memory/`](../memory/)。

### 架构

| 层级 | 状态 | 实现 | 存储 |
|------|------|------|------|
| L1 短记忆 | ✅ | `agent/memory.py` — 滑动窗口 + tiktoken 截断 | 内存 |
| L2 长记忆 | ✅ | `agent/memory_manager.py` — 跨 session 语义召回 + 时间衰减 | PostgreSQL+pgvector |
| L3 用户画像 | 🗑️ | 代码保留，调用点注释 | PostgreSQL JSONB |

### L2 召回：双通道 + 时间衰减

**语义通道**：pgvector cosine_distance ≤ threshold → `combined_score = (1 - distance) × 0.5^(days/14)`

**时效回退**（语义命中不足时）：按 created_at DESC 取最近 session → 锚定过滤（distance ≤ 0.60）→ 同样时间衰减评分

### Phase 6 影响

双套记忆阈值（Research 0.50/700 tokens, Dialogue 0.35/300 tokens）源于 Phase 4 的双 Agent 架构。Phase 6 合并为一个 Agent 后，建议合并为两套：`MEMORY_QUICK_*`（默认 Companion 模式）和 `MEMORY_DEEP_*`（depth="deep" 模式）。

---

## Phase 5.5: Output Style Control ✅（2026-06-17 初版，2026-07-22 架构重构 v3，2026-07-25 翻转优先级）

### v3 架构（2026-07-22）

结构化 CharacterProfile + AgentProfile dataclass + 统一 prompt builder。角色优先——角色身份在最前面，能力是角色的附属。详见 [`agent/profiles.py`](../../agent/profiles.py) 和 [`agent/prompt_builder.py`](../../agent/prompt_builder.py)。

### Phase 6 Step 1（2026-07-25, `4fed77f`）：翻转"数据完整性优先"→"对话优先"

- Profiles: "数据完整性优先" → "数据服务于观点"、"1-2轮足够"
- Intent 策略: lookup/realtime 加早停信号
- `_DATA_INTERPRETATION` 改为可选参数，Dialogue agent 不再接收

### Phase 6 Step 2（待实施）：Companion 人格重写

- `BANGUMI_CHARACTER` → Companion 损友人格（"让对话有趣"）
- `BANGUMI_RESEARCH_CHARACTER` → 删除
- Prompt 14 层 → 8 层，expression_guide 从 layer 12 提前到 layer 2

---

## Phase 6: Companion Agent 架构重构（进行中）

> **这是产品定位的根本性调整。** Phase 4 的双 Agent 架构假设的是 Tool Agent 心智模型（"深度链式调用"、"数据完整性优先"、"Critic 审查数据够不够全"）。Phase 6 将架构切换为 Companion Agent 心智模型（"查数据是为了聊天"、"1-2 轮够了"、"人味 > 完整"）。

### 产品定位

**Bangumi 的 AI 看板娘**——一个住在站内的、有性格的二次元损友。她可以查数据，但她存在的理由不是查数据——是陪你聊动画。

在 Agent 光谱上卡在 "ChatGPT" 和 "Character.AI" 之间——有真实数据支撑的聊天角色。

### 架构目标（Phase 6 Step 2）

| | 当前（Phase 4） | 目标（Phase 6） |
|---|---|---|
| Agent 入口 | 2 个（agent_type 路由） | 1 个（depth 参数控制深度） |
| 默认模式 | 双轨并行 | Companion 损友（1-5 轮，无 Critic） |
| 深度模式 | Research Agent（独立入口） | Research Skill（depth="deep" 激活，保留 Critic） |
| 人格 | 3 个角色（含 Research 变体） | 2 个角色（bangumi/neutral，Research 不变人格） |
| Prompt 层数 | 14 | 8（expression_guide 提前到 layer 2） |
| 迭代上限 | Research 12, Dialogue 4 | 默认 5, deep 12 |
| Critic | Research 默认运行 | 仅 depth="deep" 运行 |
| 文件结构 | `agent/research/` + `agent/dialogue/` | 合并到 `agent/` 根级 |

### 待实施（10 项）

1. 合并两个 Graph → 单一 `agent/graph.py`（depth 条件启用 Critic）
2. 合并两个 State → 统一 `AgentState`（含 `depth` 字段）
3. 合并两个 reasoning node → `agent/nodes.py`（depth 分支消化态引导 + last-chance）
4. 重写 `BANGUMI_CHARACTER` 为 Companion 损友人格
5. 删除 `BANGUMI_RESEARCH_CHARACTER`
6. Prompt 14→8 层，expression_guide 从 layer 12 提到 layer 2
7. intent 策略双版本：Companion 浅层版（`agent/prompts.py`）+ deep 深度版（`agent/research/prompts.py` 保留）
8. `main.py`：`depth` 参数替换 `agent_type`，合并两个 handler
9. Critic 条件注册（仅 depth=="deep" 时添加到 Graph）
10. 删除 `agent/dialogue/` 全目录

### 已自动迁移的文件（linter）

- `agent/profiles.py` — Companion 人格 + COMPANION_PROFILE
- `agent/prompt_builder.py` — 8 层，expression_guide 提前
- `agent/prompts.py` — **新建** Companion 浅层 intent 策略
- `agent/research/prompts.py` — 重构为深度模式专用
- `test/test_prompts.py` — 测试更新

### 效果验证（Step 1，排除 session 污染）

| Test | Before | After Step 1 |
|------|--------|-------------|
| Test 6 热门趋势 | 12 迭代, 7 工具 | **2 迭代, 2 工具** |
| Test 2 京吹评分 | 7 迭代, 编造 | 6 迭代, 无编造 |
| Test 8 星际牛仔 Dialogue | 答非所问 | 正确+毒舌 |

---

## Phase 7: 更多工具 & 社区数据（重新定义，原 Phase 6）

> 原 ROADMAP 中 Phase 6 的内容。推迟到 Companion Agent 重构完成后。

### 目标
接入 Bangumi 小组讨论、网页搜索，支持辅助发帖/影评场景。

### 新增工具（计划）

#### 7.1: 小组讨论抓取 (`get_group_topics`)
- 新 API 端点: Bangumi p1 `/p1/groups/{group_name}/topics`
- 工具: `get_group_topics(group_name, limit=20)` → 格式化讨论列表

#### 7.2: 网页搜索 (`web_search`)
- 接 Tavily / Bing Search API
- 工具: `web_search(query, limit=5)` → 标题+摘要 URL 列表

#### 7.3: 文本润色 (`polish_text`)
- 工具: `polish_text(draft, style="spoiler_free")` → LLM 润色
- 场景: 用户草稿影评 → 去剧透 + 优化表达

#### 7.4: 记忆层受益
- Group 分析结果走 `remember_public()` 写入 `public_memories`（表已建，索引已就绪）

---

## 紧急修复状态

> 以下问题在 2026-06-10 边缘审计中发现。

### ✅ 已修复

| # | 问题 | 修复 |
|---|------|------|
| P0-1 | LLM 调用无超时 | `LLM_REQUEST_TIMEOUT=60.0` |
| P1-1 | Dialogue 无重复工具调用检测 | guardrails.py 共享 |
| P1-2 | Dialogue 无逃逸舱 | guardrails.py 共享 `is_terminal_response()` |
| P1-3 | Dialogue chitchat/factual 无 XML 安全网 | guardrails.py 共享 `strip_tool_call_xml()` |
| P2-2 | `_extract_final_reply` 兜底无区分度 | 按异常类型返回不同消息 |
| P2-4 | tiktoken `encode()` 无 try/except | 降级为 `len//4` |
| P3-3 | ToolNode 泄漏堆栈 | `format_tool_error` |

### 🟡 仍待修复

| # | 问题 | 位置 | 改动量 |
|---|------|------|--------|
| P0-2 | 分类器短作品名误判 ("EVA", "K", "86") | `agent/classifier.py` | ~5 行 |
| P2-1 | messages 为空时路由崩溃 | `graph.py` | ~3 行 |
| P2-3 | Critic 硬阈值边缘误伤 | `research/nodes.py` | ~3 行 |
| P3-1 | RAG retriever 每次调用重建 | `tools/bgm_tools.py` | ~10 行 |
| P3-2 | `create_llm()` 每次调用新建实例 | `agent/llm.py` | ~5 行 |

### ℹ️ 已知次要问题

- 流式端点 `/chat/stream` 仅节点级，非逐 token
- 记忆写入的摘要 LLM 无独立超时配置
- `MEMORY_MIN_SESSIONS_FOR_PROFILE` [L3 deprecated] 零消费者

---

## 配置项待清理（Phase 6 Step 2 后）

| 配置项 | 问题 | 建议 |
|--------|------|------|
| `LLM_TEMPERATURE=0.3` | Tool Agent 优化，压制 Companion 人味 | Companion 模式 0.7-0.9 |
| `MEMORY_MAX_INJECT_TOKENS=700` | Research Agent 的旧默认值 | ~300（Companion 回复 30-150 字） |
| `MEMORY_DIALOGUE_*` (2 项) | 命名还叫 DIALOGUE | 改名为 `MEMORY_QUICK_*` |
| `CRITIC_MODE="llm"` | 注释未说明仅 deep 生效 | 更新注释 |
| `MEMORY_MIN_SESSIONS_FOR_PROFILE=5` | L3 废弃，零消费者 | 删除 |

---

## 涉及文件索引

| 文件 | 阶段 | 角色 |
|------|------|------|
| `main.py` | Phase 1/4 | FastAPI 入口 — **Phase 6 重构核心目标** |
| `core/config.py` | Phase 1 | 配置系统 — **Phase 6 默认值调整** |
| `clients/` | Phase 1 | HTTP 客户端 + sanitizer — 不需要动 |
| `rag/` | Phase 2 | RAG 检索 — 不需要动 |
| `tools/bgm_tools.py` | Phase 2 | 工具函数（dict 返回） — 不需要动 |
| `database/` | Phase 1/5 | ORM + 记忆表 — 不需要动 |
| `schemas/` | Phase 2 | 工具输入 schema — 不需要动 |
| `agent/memory.py` | Phase 5 | L1 短记忆 |
| `agent/memory_manager.py` | Phase 5 | L2 语义召回 |
| `agent/session_cache.py` | Phase 5 | 跨 HTTP 缓存 |
| `agent/classifier.py` | Phase 4 | 意图分类 |
| `agent/guardrails.py` | Phase 4 | 共享护栏 |
| `agent/reasoning_core.py` | Phase 5.5 | 共享辅助函数 |
| `agent/profiles.py` | Phase 5.5 | **已迁移** — Companion 人格 |
| `agent/prompt_builder.py` | Phase 5.5 | **已迁移** — 8 层组装 |
| `agent/prompts.py` | Phase 6 | **新建** — Companion 浅层策略 |
| `agent/research/prompts.py` | Phase 4/6 | **已迁移** — deep 模式策略 + Critic prompt |
| `agent/research/graph.py` | Phase 4 | Research 图谱 — **Phase 6 合并目标** |
| `agent/research/nodes.py` | Phase 4 | reasoning + critic — **Phase 6 合并目标** |
| `agent/research/state.py` | Phase 4 | AgentState — **Phase 6 合并目标** |
| `agent/dialogue/` | Phase 4 | Dialogue Agent — **Phase 6 删除** |

---

## 设计文档索引

| 文档 | 内容 |
|------|------|
| [`CLAUDE.md`](../../CLAUDE.md) | 项目架构、Phase 6 蓝图、产品定位 |
| [`docs/design/ROADMAP.md`](ROADMAP.md) | 本文档 |
| [`docs/design/phase1-3-audit.md`](phase1-3-audit.md) | Phase 1-3 地基审查（2026-07-26） |
| [`docs/design/phase5-memory-system-design.md`](phase5-memory-system-design.md) | Phase 5 记忆系统完整设计 |
| [`docs/design/data-layer-redesign-discussion.md`](data-layer-redesign-discussion.md) | 工具层 str→dict 迁移决策过程 |
| [`docs/design/bangumi-api-schema-methodology.md`](bangumi-api-schema-methodology.md) | A/B/C/D 字段方法论 |
| [`docs/design/personality-rendering-layer.md`](personality-rendering-layer.md) | Output Boundary 原始设计（已废弃，历史参考） |
| [`docs/design/architecture-review-2026-07-22.md`](architecture-review-2026-07-22.md) | 宏观架构 review |
| [`docs/tmp/real_data_test.md`](../tmp/real_data_test.md) | 9 个测试用例 + A/B 对照实验 |
| [`docs/memory/`](../memory/) | 记忆系统手册（6 文件） |
