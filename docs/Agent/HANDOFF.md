# Phase 3 状态转交 — 2026-06-05

> **给接手者**：这是 Bangumi AI Agent Phase 3 实施的完整状态快照。
> 设计文档在 `docs/Agent/00-08_*.md`，蓝图在 `.claude/plans/zippy-kindling-spark.md`。

---

## 1. 项目背景

一个以 [Bangumi](https://bgm.tv) 为主体的**问答/发现式 AI Agent**，帮助用户通过自然语言搜索、发现、了解动漫/漫画/音乐/游戏。

- **框架**：LangGraph StateGraph（ReAct 拓扑）
- **LLM**：OpenAI SDK 兼容（当前用 DeepSeek v4-flash）
- **工具层**：12 个 LangChain `@tool`（9 个无条件 + 3 个需 token），见 `tools/bgm_tools.py`
- **RAG**：PostgreSQL + pgvector，Zhipu embedding-3 (2048d)，见 `rag/`
- **Web**：FastAPI，见 `main.py`

---

## 2. 完成进度

### ✅ Step 1 — AgentState 升级
**文件**：`agent/state.py`

- 消息类型 `list[str]` → `list[BaseMessage]`（LangChain 类型）
- 删除 `needs_tool: bool`
- 新增字段：
  - `last_tool_calls: list[dict]` — LLM 原生工具调用，驱动路由
  - `query_intent: str` — 意图分类结果（"chitchat"|"factual"|"lookup"|"discovery"|"realtime"|"unknown"）
  - `critic_feedback: str` — Critic 定向反馈
  - `session_id: str` — Layer 2 预留
  - `user_id: str` — Layer 3 预留
- **约束**：`last_tool_calls` 仅 `reasoning_node` 写入；`tool_node` / `critic_node` 禁止触碰

### ✅ Step 2 — LLM 接入 + 意图分类器
**文件**：`core/config.py`（新增 LLM 字段）, `agent/llm.py`（新建）, `agent/classifier.py`（新建）, `agent/prompts.py`（新建）, `agent/nodes.py`（reasoning_node 重写）

- **Config**：LLM_API_KEY, LLM_MODEL, LLM_BASE_URL, LLM_TEMPERATURE, LLM_MAX_TOKENS, LLM_AZURE_ENDPOINT 等，使用 `AliasChoices` 兼容 `AZURE_OPENAI_*` / `OPENAI_*` / `LLM_*` 三套环境变量命名
- **LLM Factory** (`agent/llm.py`)：`create_llm()` 单入口，自动检测 Azure / 自定义 endpoint / 标准 OpenAI
- **意图分类器** (`agent/classifier.py`)：两阶段（优先级规则列表 → LLM fallback），6 类输出
- **Prompt 系统** (`agent/prompts.py`)：`BASE_SYSTEM_PROMPT` + 5 个 intent 变体 + 工具依赖约束 + `CRITIC_SYSTEM_PROMPT`
- **reasoning_node**：集成 `classify_intent()` → `build_system_prompt()` → `create_llm().bind_tools()` → LLM.invoke()
- **bind_tools 策略**：chitchat / factual 不绑定工具（零开销），其他绑定 9 个工具

### ✅ Step 3 — tool_node 接入 ToolNode
**文件**：`agent/graph.py`

- `tool_node` 切换为 `ToolNode(get_agent_tools(), handle_tool_errors=True)`
- `build_graph()` 新增 `tools` 参数支持测试注入 mock 工具
- 原手动实现保留在 `agent/nodes.py` 作为 `tool_node_manual_reference`（仅供参考）

### ✅ Step 4 — critic_node 定向反馈 + 逃逸舱
**文件**：`agent/nodes.py`, `core/config.py`（新增 `CRITIC_MODE`）

- **双模式**：`CRITIC_MODE="rule"`（默认，零 Token）或 `"llm"`（三元维度 + 逃逸舱）
- **规则版**：检查（1）工具有数据但无 AI 回复 → REVISE；（2）回复 < 20 字 → REVISE；（3）闲聊 → PASS
- **LLM 版**：三元维度（完整性/具体性/工具利用）+ 逃逸舱（API 无数据 → 强制 PASS）+ 三段式反馈
- **反馈格式**：`REVISE: <缺陷> | <建议> | <缺失>`

### ✅ 测试解耦
**文件**：`test/` 目录

- 旧 monolithic `test/test_agent.py` 已删除
- 拆分 8 个独立文件 + 共享 `conftest.py`：

| 文件 | 数量 | 覆盖 |
|---|---|---|
| `test_state.py` | 13 | State 结构 + 路由 |
| `test_classifier.py` | 34 | 规则层 + LLM fallback |
| `test_llm.py` | 5 | LLM 工厂 + Key 解析 |
| `test_prompts.py` | 8 | 系统提示词 |
| `test_reasoning.py` | 10 | reasoning_node (mock LLM) |
| `test_tool_node.py` | 5 | ToolNode 执行 |
| `test_critic.py` | 13 | 规则版 + LLM 版 |
| `test_graph.py` | 4 | 图谱集成 |

**运行**：`python -m pytest test/ --ignore=test/test_rag.py` → 313 passed

---

## 3. 剩余任务（按优先级）

### ⬜ Step 5 — 短期记忆管理
**设计文档**：`docs/Agent/06_MEMORY.md`

- 创建 `agent/memory.py`
- 实现滑动窗口截断（tiktoken `cl100k_base` 精确计数，**不能用** `len//4`）
- 在 reasoning_node 开头或 tool_node 返回后触发截断
- Token 预算默认 8000

### ⬜ Step 6 — /chat 端点
**设计文档**：`docs/Agent/07_ENDPOINT.md`

- 在 `main.py` 添加 `POST /chat` 和 `POST /chat/stream`
- `ChatRequest`：`message`, `session_id`, `user_id`
- `ChatResponse`：`reply`, `iterations`, `tools_used`, `query_intent`
- 初始化 state 时传递 `user_id` / `session_id`

### ⬜ Step 7 — 测试完善
**设计文档**：`docs/Agent/08_TESTING.md`

- 新增记忆管理测试
- 新增 /chat 端点测试

### ⬜ Step 8 — 端到端验证
- 用 `curl` 对 `/chat` 发 3 类查询验证完整链路
- 长对话 session 内存记忆测试

---

## 4. 关键架构决策速查

| 决策 | 结论 |
|---|---|
| Agent 框架 | LangGraph StateGraph，3 节点拓扑不变 |
| 意图分类位置 | reasoning_node 内部，不新增节点 |
| 意图分类优先级 | discovery > realtime > lookup > factual > chitchat（用 list 不用 dict） |
| Critic 模式 | 默认规则版 (`CRITIC_MODE=rule`)；LLM 版需在 .env 切 `CRITIC_MODE=llm` |
| Critic 逃逸舱 | API 确实无数据 → 强制 PASS，禁止 REVISE |
| Token 计数 | tiktoken `cl100k_base`（**不是** `len//4`） |
| `last_tool_calls` 生命周期 | 仅 reasoning_node 写入，tool_node/critic_node 禁止触碰 |
| 记忆架构 | Layer 1 滑动窗口（Phase 3 实施）；Layer 2/3 会话+画像（预留接口） |
| 不做的事 | MCP、Plan-and-Execute、多 Agent 投票、token 级 streaming |

---

## 5. 当前文件结构

```
agent/
├── __init__.py
├── state.py          # AgentState TypedDict（9 字段）
├── graph.py          # StateGraph 编排 + ToolNode
├── nodes.py          # reasoning_node, critic_node, tool_node_manual_reference
├── llm.py            # create_llm() 多 Provider 工厂
├── classifier.py     # classify_intent() 两阶段分类器
├── prompts.py        # 系统提示词 + Critic prompt + build_system_prompt()
core/
├── config.py         # Settings（含 LLM 字段 + CRITIC_MODE）
.env                  # DeepSeek API Key + 模型配置
docs/Agent/
├── 00_OVERVIEW.md    # 架构总览
├── 01_STATE.md       # State 设计
├── 02_REASONING.md   # 意图分类 + LLM
├── 03_TOOL_EXECUTION.md # ToolNode + 并行边界
├── 04_CRITIC.md      # 定向反馈 + 逃逸舱
├── 05_SYSTEM_PROMPT.md # Prompt 变体 + 路由策略
├── 06_MEMORY.md      # 三层记忆架构
├── 07_ENDPOINT.md    # /chat 端点
├── 08_TESTING.md     # 测试策略
├── HANDOFF.md        # ← 本文件
.claude/plans/
├── zippy-kindling-spark.md  # Phase 3 完整蓝图
```

---

## 6. Graph 拓扑（当前状态）

```
START
  │
  ▼
reasoning_node          ← 意图分类 + LLM function-calling
  │
  ▼ (last_tool_calls 非空?)
  ├─ Yes → ToolNode    ← 并发执行工具，返回 ToolMessage
  │         │
  └─ No ───┴──────────→ critic_node
                          │            ← 规则版/LLM版
                          ▼ (PASS? 熔断?)
                          ├─ PASS/熔断 → END
                          └─ REVISE   → reasoning_node (重试)
```

---

## 7. 环境配置速查

```bash
# .env 关键字段
LLM_API_KEY=sk-xxx          # DeepSeek API Key
LLM_MODEL=deepseek-v4-flash # LLM 模型
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_TEMPERATURE=0.3

# 切换到 LLM 版 Critic（可选）
CRITIC_MODE=rule             # "rule" | "llm"

# 数据库（RAG 用，需要 Docker）
DATABASE_URL=postgresql://myuser:mypassword@localhost:5432/bangumidb
```

---

## 8. 常用命令

```bash
# 运行所有测试
python -m pytest test/ --ignore=test/test_rag.py -v

# 运行单个模块测试
python -m pytest test/test_classifier.py -v
python -m pytest test/test_critic.py -v

# 启动 Docker（RAG 需要）
docker start bangumi-pg

# 启动 API 服务
uvicorn main:app --reload --port 8000
```

---

## 9. 已知问题

1. **DeepSeek `deepseek-v4-flash` thinking mode**：该模型在 reasoning content 回传场景下会报 400 错误（`reasoning_content must be passed back`）。非 thinking 场景正常。如需规避，可换用标准 `deepseek-chat`。
2. **图集成测试 mock 限制**：Python 3.14 + LangGraph 异步上下文中，深度 mock chain（多轮 tool → critic → retry）会触发 `generator raised StopIteration`。核心行为已由各节点 unit test 覆盖，graph test 仅保留浅层集成（chitchat/熔断/factual/discovery 直通）。
3. **RAG 数据尚未注入**：`rag_entities` 表为空，`search_local_bangumi` 工具需要先注入数据（预估 ~80K 实体，~1.5GB）。

---

*最后更新：2026-06-05，Phase 3 Step 1-4 完成 + 测试解耦完成。下一步：Step 5 短期记忆管理。*
