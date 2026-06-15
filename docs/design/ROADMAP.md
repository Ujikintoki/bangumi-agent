# 开发路线图

> 最后更新: 2026-06-14 | 当前阶段: Phase 5 完成，Phase 5.5 待启动

---

## 当前状态快照

| 指标 | 值 |
|------|-----|
| 总测试数 | 494 |
| 记忆相关测试 | 60 (L1: 45, L2/L3: 15) |
| Agent 数 | 2 (Research + Dialogue) |
| 工具数 | 12 |
| 工具链深度 | 2-3 (search → detail → characters/comments) |
| 记忆层级 | 3 (L1 滑动窗口, L2 语义召回, L3 用户画像) |
| 配置项 | 10 个 MEMORY_* 配置 |
| 文档 | 8 个（ROADMAP + Phase 5 设计 + 6 个 memory 手册） |

---

## 总体路线

```
Phase 4 (done)       Phase 5 (done)         Phase 5.5               Phase 6
双 Agent              三层记忆              Output Boundary          更多工具
                       │                     │                        │
  research        双通道语义召回          prompt 人格剥离          group topics
  + dialogue      时间衰减排序            render() 共享             web_search
  + 12 tools      锚定回退               AGENT_DEFAULTS            发帖辅助
                       │                     │                        │
                  L2: session 记忆        agent × style             记忆层受益
                  L3: 用户画像            四象限可用                 输出边界受益
```

**依赖关系**：记忆层 →（Output Boundary、更多工具并行）

---

## Phase 5: 三层记忆系统 ✅ 已完成

> 详细设计方案见 [`docs/design/phase5-memory-system-design.md`](phase5-memory-system-design.md)（1194 行）  
> 综合手册见 [`docs/memory/`](../memory/README.md)（6 文件）  
> 实现文件：`agent/memory.py` (L1) + `agent/memory_manager.py` (L2/L3, 931 行)  
> 数据库：`database/memory_tables.py` (223 行, 3 张表)  
> 配置：`core/config.py` 中 10 个 MEMORY_* 项

### 已完成 vs 原始计划差异

| 项目 | 原始计划 | 实际实现 |
|------|---------|---------|
| L2 召回策略 | 单一语义通道 | **双通道**：语义 (cos ≤ 0.50) + 时效回退 (cos ≤ 0.70 锚定) |
| L2 排序 | cosine 距离 | **时间衰减**：`combined_score = (1-cos_dist) × 0.5^(days/14)` |
| user_profiles 字段 | `avg_rating` | `avg_session_length`（更准确的活跃度指标） |
| user_profiles 冗余列 | 2 个 | 4 个：`total_sessions`, `avg_session_length`, `dominant_intent`, `last_active_at` |
| public_memories 字段 | 4 个基础字段 | 10 个字段：含 `heat_score`, `tags`, `expires_at`, `is_active` 等 Phase 6 预留 |
| 配置项 | 8 个 | 10 个：增加了 `MEMORY_TIME_DECAY_HALF_LIFE_DAYS`, `MEMORY_RECENCY_FALLBACK_THRESHOLD`, `MEMORY_DIALOGUE_MAX_INJECT_TOKENS` |
| 记忆注入预算 | 不分 Agent 统一 | Research 500, Dialogue 300（独立配置） |
| L1 测试 | `test/test_memory.py` 仅 L1 测试 | 拆分：`test/test_memory.py` (31) + `test/test_phase5_l1.py` (14) |
| `POST /chat/history` 端点 | 计划（可选） | 未实现 — 可通过 DB 直接查询替代 |
| Dialogue 记忆召回 | 未提及 | 已集成，chitchat 也召回（recency fallback 保证追问连续性） |
| 公共模块 | 无 | `agent/guardrails.py` — 共享 `is_terminal_response`, `strip_tool_call_xml`, `check_duplicate_tool_calls`, `format_tool_error` |
| Zhipu 客户端 | 无独立文件 | `clients/zhipu_client.py` — embedding 基础设施 |
| RAG 表重构 | 未计划 | 旧 `models.py` 拆分 → `rag_tables.py` (298 行)，旧表重命名为 `rag_entities` |

### L2 召回策略：双通道 + 时间衰减

**通道 1: 语义通道**
```
pgvector cosine_distance(query_embedding, session_embedding)
  → 过滤: distance ≤ 0.5 (MEMORY_RECALL_THRESHOLD)
  → 评分: combined_score = (1 - distance) × 0.5^(days_ago / 14)
```

**通道 2: 时效回退**（语义命中不足 TOP_K 时触发）
```
按 created_at DESC 取最近 session
  → 计算 cosine_distance
  → 锚定过滤: distance ≤ 0.70 (MEMORY_RECENCY_FALLBACK_THRESHOLD)
  → 评分: combined_score = (1 - distance) × 0.5^(days_ago / 14)
```

**关键设计**：回退通道有"最小语义锚定"——即使是最新 session，cosine distance > 0.70 也不会注入。embedding API 不可用时回退到纯时效排序。

### 优雅降级矩阵

| 故障点 | 降级行为 | 用户体验 |
|--------|---------|---------|
| embedding API 超时/失败 | embedding=None，回退纯时效排序 | 近期记忆仍可用 |
| 语义检索 DB 异常 | RuntimeError 捕获，scored=[] | 无记忆，agent 正常回复 |
| 摘要 LLM 失败 | 回退 `final_reply[:200]` | 摘要质量略降 |
| session_memory INSERT 失败 | SQLAlchemyError 捕获，skip 画像更新 | 本轮不记，下轮不受影响 |
| 画像更新失败 | 异常捕获，仅 WARNING 日志 | 画像保持旧状态 |
| `MEMORY_ENABLED=False` | recall/remember 全部返回 no-op | Agent 退化回无记忆模式 |
| `user_id="anonymous"` | recall/remember 全部返回 no-op | 匿名用户不触发记忆 |

### Phase 5 完成标准 ✅
- ✅ session_id 不同 → 记忆隔离
- ✅ user_id 相同 → 跨 session 语义召回历史摘要
- ✅ 双通道召回：语义 (cos ≤ 0.5) + 时效回退 (cos ≤ 0.70，最小语义锚定)
- ✅ 时间衰减排序：`combined_score = (1-cos_dist) × 0.5^(days/14)`
- ✅ 用户画像增量更新（偏好类型、实体亲和度、行为特征）
- ✅ Fire-and-forget 写入，异常静默降级
- ✅ Research + Dialogue 双 Agent 记忆集成
- ✅ 494 tests 通过
- ✅ 完整设计文档 + 6 文件记忆手册

---

## Phase 5.5: Output Boundary 重构

### 目标
将人格设定从 Agent 核心 System Prompt 中剥离，移入独立的 output boundary 渲染层。**两个 agent 共享同一个 `render()` 函数。**

### 设计文档
详见 [`docs/design/personality-rendering-layer.md`](personality-rendering-layer.md)

### Step 分解

#### Step 5.5.1: 新建 `agent/personality/` 模块（新增，不影响现有代码）
- `__init__.py` — export `render(content, style, llm) -> str`
- `renderer.py` — 渲染引擎：`"neutral"` 透传，其余风格调轻量 LLM 改写
- `styles.py` — `STYLE_REGISTRY: dict[str, StyleConfig]` + `AGENT_DEFAULTS`
- `prompts.py` — `RENDER_BANGUMI_PROMPT`（从 `DIALOGUE_SYSTEM_PROMPT` 迁移人格内容）

#### Step 5.5.2: 请求/响应模型更新
- **修改 `main.py`**: `ChatRequest` 加 `output_style: Literal["neutral", "bangumi"] = "neutral"`
- **修改 `main.py`**: `ChatResponse` 加 `output_style: str`
- **修改 `main.py`**: `AGENT_DEFAULTS` — research→neutral, dialogue→bangumi

#### Step 5.5.3: main.py 响应管道插入 render()
- **修改 `main.py`**: `_chat_dialogue` / `_chat_research` / `chat_stream` 中
  `_extract_final_reply()` 之后调用 `render(content, style, llm)`
- 跳过渲染的条件: `style=="neutral"` | agent 异常 | render LLM 失败

#### Step 5.5.4: 剥离 DIALOGUE_SYSTEM_PROMPT 人格
- **修改 `agent/dialogue/prompts.py`**: 删除 Bangumi娘人设/语气/字数约束
- **迁移到 `agent/personality/prompts.py`**: 作为 `RENDER_BANGUMI_PROMPT`
- Dialogue 的 System Prompt 改为中性能力描述 + 浅层工具策略（保留）

#### Step 5.5.5: 精简 BASE_SYSTEM_PROMPT 风格指令
- **修改 `agent/research/prompts.py`**: 删除 "回答风格：简洁、具体、可操作"
- 保留: 能力描述、工具依赖规则、数据模型约束、输出格式规则、退出条件

#### Step 5.5.6: 测试更新
- **修改 `test/test_dialogue.py`**: 验证剥离后 prompt 不再含人设字段
- **新建测试**: 四个象限组合 (research/dialogue × neutral/bangumi) 输出正确性
- **新建测试**: 渲染层禁止编造数据（diff 校验）

### Phase 5.5 完成标准
- `agent_type` 和 `output_style` 完全正交
- research + bangumi、dialogue + neutral 等四个象限均可用
- 渲染层不编造数据：输出中的评分/排名/名称全部来自中性输入
- 不影响现有 494 tests

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
| `agent/dialogue/prompts.py` | Phase 5.5 待改 | Bangumi娘人格（待剥离至 personality 模块） |
| `agent/research/prompts.py` | Phase 5.5 待改 | 风格指令（待精简） |

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

**继续 Phase 5 维护：**
> 阅读 `docs/memory/README.md`，根据 `docs/design/ROADMAP.md` 了解当前状态和待修复项。

**启动 Phase 5.5：**
> 阅读 `docs/design/personality-rendering-layer.md`，按 Step 5.5.1 开始新建 `agent/personality/` 模块。

**修复剩余问题：**
> 阅读 `docs/design/ROADMAP.md` 的 🟡 仍待修复节，先修 P0-2（短名误判）。

**了解项目全貌：**
> 阅读 `CLAUDE.md`，然后读取 `docs/design/` 目录下所有设计文档。
