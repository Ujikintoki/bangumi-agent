# 开发路线图

> 最后更新: 2026-06-13 | 当前阶段: Phase 5 完成，Phase 5.5 待启动

---

## 总体路线

```
Phase 4 (done)       Phase 5 (done)         Phase 5.5               Phase 6
双 Agent              三层记忆              Output Boundary          更多工具
                       │                     │                        │
  research        session 记忆          prompt 人格剥离          group topics
  + dialogue      长记忆 + 摘要           render() 共享             web_search
  + 12 tools      公共记忆               AGENT_DEFAULTS            发帖辅助
                       │                     │                        │
                  session_id/user_id     agent × style             记忆层受益
                  从空转变持久化          四象限可用                 输出边界受益
```

**依赖关系**：记忆层 →（Output Boundary、更多工具并行）

---

## Phase 5: 三层记忆系统

### 目标
让 `session_id` 和 `user_id` 从"预留字段"变成真正工作的持久化层。每次 `/chat` 不再是金鱼记忆。

### 三层记忆设计

| 层级 | 存储内容 | 生命周期 | 实现方式 |
|------|---------|---------|---------|
| **L1 短记忆** | 当前 session 的对话历史 | 单 session | ✅ 已有 — `manage_memory()` 滑动窗口 + 两层截断 |
| **L2 长记忆** | 用户偏好、评分倾向、历史查询摘要 | 跨 session，按 user_id | **新增** — PostgreSQL 表 + LLM 定期摘要写入 |
| **L3 公共记忆** | 从 group/topics 蒸馏的社区共识、热门趋势快照 | 全局共享 | **新增** — RAG 写入 pipeline + 定期 batch 分析 |

### Step 分解

#### Step 5.1: 数据库表设计
- **新建文件**: `database/memory_tables.py`
- session_memories 表: `(id, session_id, user_id, summary_text, embedding, created_at, updated_at)`
- user_profiles 表: `(id, user_id, preferences_json, avg_rating, favorite_genres, last_active_at)`
- public_memories 表: `(id, topic, summary_text, embedding, source_type, created_at)` — 预留 Phase 6 使用
- **写 migration SQL**，确保 pgvector HNSW 索引同步创建
- **不删除**现有 `rag_entities` 表

#### Step 5.2: 记忆管理器
- **新建文件**: `agent/memory_manager.py`（与现有 `memory.py` 配合，不替换）
- `remember_session()`: session 结束时，LLM 将对话摘要为 200 字短文 → 写入 session_memories + 生成 embedding
- `recall_session()`: 新对话开始时，根据当前用户输入做语义检索最近的 session 摘要 → 注入 System Prompt
- `update_user_profile()`: 根据评分/收藏行为增量更新用户偏好
- `remember_public()`: 写入公共记忆（Phase 6 时 group 分析结果调用）

#### Step 5.3: Agent 集成
- **修改文件**: `agent/research/nodes.py`, `agent/dialogue/nodes.py`
- reasoning_node 首轮推理时:
  1. 调用 `recall_session(user_id, user_message)` → 获取相关历史摘要
  2. 将摘要注入 System Prompt（格式: `"## 用户历史\n用户之前关注过：..."`）
- Agent 返回最终回复后（在 main.py 响应返回前）:
  3. 调用 `remember_session(session_id, user_id, messages, final_reply)` → 写入摘要

#### Step 5.4: main.py 管道
- **修改文件**: `main.py`
- `/chat` 响应返回后异步触发 `remember_session()`（不阻塞用户响应）
- 新增 `POST /chat/history` 端点（可选）: 查询用户历史摘要

#### Step 5.5: 测试
- **新建文件**: `test/test_memory_manager.py`
- 测试: session 摘要写入/召回、用户偏好更新、跨 session 记忆连续性

### Phase 5 完成标准
- ✅ session_id 不同 → 记忆隔离
- ✅ user_id 相同 → 跨 session 语义召回历史摘要
- ✅ 双通道召回：语义 (cos ≤ 0.5) + 时效回退 (cos ≤ 0.70，最小语义锚定)
- ✅ 时间衰减排序：`combined_score = (1-cos_dist) × 0.5^(days/14)`
- ✅ 用户画像增量更新（偏好类型、实体亲和度、行为特征）
- ✅ Fire-and-forget 写入，异常静默降级
- ✅ 不影响现有测试（486 passed, 8 pre-existing 失败）
- ✅ 完整设计文档：`docs/design/phase5-memory-system-design.md`

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
- 不影响现有 438 tests

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
- Step 6.5: 记忆层受益 — group 分析结果走 `remember_public()` 写入公共记忆
- Step 6.6: 输出边界受益 — 润色/吐槽内容自动走 `render(style="bangumi")` 人格化
- Step 6.7: 测试更新

---

## 紧急修复（可在任意 Phase 间穿插执行）

这些是 [2026-06-10 边缘审计](./audit-2026-06-10.md) 发现的问题，不依赖任何 Phase，可随时修。

### P0 — 可能导致崩溃

| # | 问题 | 修复位置 | 改动量 |
|---|------|---------|--------|
| P0-1 | LLM 调用无超时 | `agent/llm.py` — `create_llm()` 加 `request_timeout=60` | 1 行 |
| P0-2 | 分类器对短作品名误判 ("EVA", "K", "86") | `agent/classifier.py` — 短名优先走 LLM fallback，fallback 结果不为 chitchat 时强制绑定工具 | ~5 行 |

### P1 — Dialogue 防御机制补全

| # | 问题 | 修复位置 | 改动量 |
|---|------|---------|--------|
| P1-1 | Dialogue 无重复工具调用检测 | 从 `research/nodes.py` 移植 `_check_duplicate_tool_calls()` 到 `dialogue/nodes.py` | ~30 行 |
| P1-2 | Dialogue 无逃逸舱（终端回复检测） | 从 `research/nodes.py` 移植 `_is_terminal_response()` + 12 条正则 | ~20 行 |
| P1-3 | Dialogue chitchat/factual 不绑工具但无 XML 安全网 | `dialogue/nodes.py` 加 `_strip_tool_call_xml()` 调用 | ~5 行 |

### P2 — 健壮性加固

| # | 问题 | 修复位置 | 改动量 |
|---|------|---------|--------|
| P2-1 | messages 为空时路由读 `messages[-1]` 崩溃 | `graph.py` — 加空列表检查 | ~3 行 |
| P2-2 | `_extract_final_reply` 兜底无区分度 | `main.py` — 按异常类型返回不同兜底 | ~10 行 |
| P2-3 | Critic `< 20 字` 硬阈值边缘误伤 | `research/nodes.py` — 将阈值从 20 降至 12，或加更多逃逸舱模式 | ~3 行 |
| P2-4 | tiktoken `encode()` 无 try/except | `agent/memory.py` — 加异常捕获 + 降级为 `len//4` 估算 | ~8 行 |

### P3 — 性能/技术债

| # | 问题 | 修复位置 |
|---|------|---------|
| P3-1 | RAG retriever 每次调用重建 | `tools/bgm_tools.py` — 单例化或连接复用 |
| P3-2 | `create_llm()` 每次调用新建实例 | `agent/llm.py` — 加模块级缓存 |
| P3-3 | ToolNode `handle_tool_errors=True` 泄漏堆栈 | `graph.py` — 自定义 error handler 过滤堆栈 |

---

## 设计文档索引

| 文档 | 内容 |
|------|------|
| [`CLAUDE.md`](../../CLAUDE.md) | 项目架构、命令、约定、当前状态 |
| [`docs/design/personality-rendering-layer.md`](personality-rendering-layer.md) | Output Boundary 设计规范 v2 — 六边形架构、render() 共享、四象限 |
| [`docs/design/ROADMAP.md`](ROADMAP.md) | 本文档 — 路线图 & 任务分解 |
| [`docs/memory/`](../memory/README.md) | 记忆系统综合手册 — 架构、实现、配置、测试、调试 |

---

## 新对话快速启动指南

在新的 Claude Code session 中，用以下提示启动工作：

**启动 Phase 5:**
> 阅读 `CLAUDE.md` 和 `docs/design/ROADMAP.md`，开始 Phase 5.1 数据库表设计。

**启动 Phase 5.5:**
> 阅读 `docs/design/personality-rendering-layer.md`，按 Step 5.5.1 开始新建 `agent/personality/` 模块。

**执行紧急修复:**
> 阅读 `docs/design/ROADMAP.md` 的紧急修复章节，先修 P0-1（LLM 超时）和 P0-2（短名误判）。

**了解项目全貌:**
> 阅读 `CLAUDE.md`，然后读取 `docs/design/` 目录下所有设计文档。
