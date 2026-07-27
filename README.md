<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/FastAPI-0.115+-009688?style=for-the-badge&logo=fastapi&logoColor=white" alt="FastAPI">
  <img src="https://img.shields.io/badge/PostgreSQL-16_%2B_pgvector-4169E1?style=for-the-badge&logo=postgresql&logoColor=white" alt="PostgreSQL">
  <img src="https://img.shields.io/badge/LangGraph-ReAct-ff6b35?style=for-the-badge" alt="LangGraph">
  <img src="https://img.shields.io/badge/tests-~520-brightgreen?style=for-the-badge" alt="Tests">
  <img src="https://img.shields.io/badge/version-0.1.0-informational?style=for-the-badge" alt="Version">
</p>

<h1 align="center">BGM Agent</h1>
<p align="center">
  <strong>Bangumi 的 AI 看板娘</strong><br>
  <sub>一个住在 <a href="https://bgm.tv">bangumi.tv</a> 站内的、有性格的二次元损友。<br>她可以查数据，但她存在的理由不是查数据——是陪你聊动画。</sub>
</p>

---

## 她是谁

一个 **Companion Agent**（知识型损友），卡在"ChatGPT 通用助手"和"Character.AI 角色扮演"之间——有真实数据支撑的聊天角色。

**她可以：**
- 有自己的品位和立场（"这部 8.5 分说实话水了"）
- 反问用户、承认不知道、推荐一部并说出理由
- 查 Bangumi 站内评分、排名、声优、排期、社区讨论——然后当八卦聊
- 深度分析（在你明确需要时）：search → detail → characters 链式调用

**她不是：**
- 不是搜索引擎——不会穷举结果
- 不是数据看板——不会列评分分布表
- 不是维基百科——不会逐条罗列所有信息

```
用户: EVA 评分怎么样？

BGM Agent: EVA 评分很能打——旧剧场版 8.86 稳居全站前十，TV 版 9.1 分
          更是离谱。不过我觉得分数里情怀加成不少，真要论精神冲击还是
          旧剧场版更凶。你是从 TV 入坑的还是直接剧场版？
```

---

## 核心特性

- **Companion Agent 单一体架构**：一个 agent 入口，`depth` 参数控制深度（`auto`/`quick`/`deep`），对用户透明
- **16 个 Bangumi API 工具**（13 无条件 + 3 token 门控）：条目搜索与详情、角色与声优、每日放送、热门趋势、单集讨论、社区评论、用户画像、本地 RAG 语义搜索——返回结构化高密度数据
- **人格渲染**：2 种角色（bangumi 损友 / neutral 助手），`render_node` 在工具调用后将"数据报告"改写为角色聊天风格，按 depth 分档字数限制
- **语义记忆**：L1 滑动窗口（同 session）+ L2 跨会话 pgvector 语义召回（双通道 + 时间衰减），让 agent 记住你们聊过什么
- **混合 RAG 检索**：语义前缀防稀释 + 多态分桶排序 + pgvector HNSW 索引，覆盖"类似命运石之门的烧脑番"这类 API 搜不到的模糊查询
- **SSE 流式输出**：`/chat/stream` 端点，按节点推送推理过程

---

## 快速开始

### 前置条件

- Python 3.11+
- Docker（用于 PostgreSQL + pgvector）

### 4 步启动

```bash
# 1. 克隆
git clone <repo-url> && cd bgm-agent-dev

# 2. 安装依赖
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 3. 配置（最小可跑：只需要 LLM_API_KEY）
cp .env.example .env
# 编辑 .env，至少填一个 LLM provider：
#   LLM_MODEL=deepseek-v4-flash
#   LLM_API_KEY=sk-your-key
#   LLM_BASE_URL=https://api.deepseek.com/v1
# 可选：ZHIPU_API_KEY（RAG 语义检索）
# 可选：BANGUMI_ACCESS_TOKEN（用户画像/日志/时光机工具）

# 4. 启动 PostgreSQL + pgvector，然后运行
docker run -d --name bangumi-pg \
  -e POSTGRES_USER=myuser -e POSTGRES_PASSWORD=mypassword \
  -e POSTGRES_DB=bangumidb -p 5432:5432 pgvector/pgvector:pg16

uvicorn main:app --reload --port 8000
```

### 发第一个请求

```bash
curl -s -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "你好，最近有什么好看的番？", "depth": "auto"}' | python3 -m json.tool
```

---

## 配置

`.env` 中的关键环境变量：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `LLM_MODEL` | `gpt-4o` | 推荐 `deepseek-v4-flash` |
| `LLM_API_KEY` | — | DeepSeek / OpenAI / Azure API key |
| `LLM_BASE_URL` | — | 自定义 endpoint（如 `https://api.deepseek.com/v1`） |
| `LLM_TEMPERATURE` | `0.3` | 推理温度 |
| `DATABASE_URL` | `postgresql://myuser:mypassword@localhost:5432/bangumidb` | PostgreSQL 连接 |
| `ZHIPU_API_KEY` | — | 智谱 API key（RAG embedding，可选） |
| `BANGUMI_ACCESS_TOKEN` | — | Bangumi API token（用户相关工具，可选） |
| `MEMORY_ENABLED` | `True` | L2 跨会话记忆开关 |
| `CRITIC_MODE` | `llm` | 自省模式（`llm` 或 `rule`），仅 depth=deep 生效 |

完整配置项见 `.env.example` 和 `core/config.py`。

---

## API

### POST /chat

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `message` | `str` | *必填* | 用户消息 |
| `depth` | `"auto"` \| `"quick"` \| `"deep"` | `"auto"` | 深度控制 |
| `output_style` | `"neutral"` \| `"bangumi"` | `"bangumi"` | 人格风格 |
| `session_id` | `str` | 自动生成 | 多轮会话 ID |
| `user_id` | `str` | `"anonymous"` | 跨会话记忆用户 ID |

| 响应字段 | 类型 | 说明 |
|----------|------|------|
| `reply` | `str` | Agent 回复 |
| `iterations` | `int` | ReAct 循环轮数 |
| `tools_used` | `list[str]` | 调用的工具列表 |
| `query_intent` | `str` | 意图分类 |
| `output_style` | `str` | 实际使用的人格 |
| `depth` | `str` | 实际使用的深度模式 |

### POST /chat/stream

同上参数，返回 SSE（`text/event-stream`），按节点推送 `reasoning`、`tool`、`critic`（仅 deep）、`render` 事件。

---

## 架构

```
用户请求 (POST /chat)
        │
        ▼
    FastAPI ── depth 参数解析 + agent_type 兼容映射
        │
        ▼
┌──────────────────────────────────────────┐
│           Companion Agent                 │
│                                           │
│  reasoning_node ── LLM invoke (绑工具)    │
│       │                                   │
│       ├── tool_calls → tool_node ──┘      │
│       ├── chitchat   → END                │
│       ├── depth=deep → critic_node        │
│       │   ├── REVISE → reasoning ──┘      │
│       │   └── PASS   → render_node → END  │
│       └── 有工具调用 → render_node → END  │
│                                           │
│  人格渲染: expression_guide + _RENDER_STYLE│
│  记忆注入: L1 滑动窗口 + L2 语义召回       │
└──────────────────────────────────────────┘
```

| 层 | 职责 | 目录 |
|-----|------|------|
| 编排层 | StateGraph 拓扑、路由、Critic 条件分支、意图策略 | `agent/orchestrate/` |
| 人格层 | 角色定义 + Render 层风格转换 | `agent/persona/` |
| 记忆层 | L1 滑动窗口 + L2 跨会话语义召回 + session 缓存 | `agent/memory/` |
| 数据层 | 16 工具 + HTTP 客户端 + RAG + pgvector | `tools/`, `clients/`, `rag/`, `database/` |

---

## 开发

```bash
# 运行全部测试（需要 PostgreSQL + pgvector）
pytest test/ -v

# 仅不需要数据库的测试
pytest test/ --ignore=test/test_rag.py -v

# 仅记忆系统测试
pytest test/test_memory.py test/test_memory_manager.py -v
```

### 项目结构

```
agent/
├── state.py, graph.py, llm.py    # Agent 入口、状态定义、LLM 工厂
├── orchestrate/                  # 编排层 — 推理、策略、分类、护栏
├── persona/                      # 人格层 — profiles、render
├── memory/                       # 记忆层 — L1 短记忆、L2 长记忆、缓存
tools/                            # 16 个 LangChain @tool 函数
clients/                          # HTTP 客户端（httpx 异步、重试）+ sanitizers
rag/                              # RAG 检索（语义前缀、分桶排序、HNSW 索引）
database/                         # SQLModel ORM + pgvector
schemas/                          # Pydantic v2 工具输入 schema
core/                             # pydantic-settings 全局配置
test/                             # ~520 个测试（22 个文件）
docs/                             # 设计文档、记忆手册、API 参考
```

### 文档

| 文档 | 说明 |
|------|------|
| [`CLAUDE.md`](CLAUDE.md) | 四层架构手册、调参速查、编码规范 |
| [`docs/design/ROADMAP.md`](docs/design/ROADMAP.md) | 架构状态 & 路线图 |
| [`docs/memory/`](docs/memory/) | 记忆系统手册（6 文件） |
| [`docs/design/`](docs/design/) | 设计决策记录（架构评审、数据层重构、Phase 1-3 审计） |
| [`docs/Rag/`](docs/Rag/) | RAG 策略与表结构 |
| [`docs/tool-guide.md`](docs/tool-guide.md) | 工具增/改/删操作指南 |

---

## 路线图

```
Phase 1-3 ✅    Phase 4 ✅     Phase 5 ✅    Phase 5.5 ✅   Phase 6 ✅    Phase 6.5 ●    Phase 7
地基             双 Agent       记忆          人格化          纠正错配       解耦风格        更多工具
──■───────────■─────────────■────────────■──────────────■────────────■─────────────■──→
FastAPI         拆 Research   L1 滑动窗口   CharacterProfile 合并双 Agent    render_node    小组讨论
BangumiClient   + Dialogue    L2 语义召回   AgentProfile      depth 参数      极简 prompt    网页搜索
RAG + pgvector  引入 Critic   L3 废弃       角色优先          Critic 条件路由  字数分档       配置清理
第一个 ReAct     ← Tool Agent 错配开始 →                                                public_memories
                                                                         四层架构清晰
```

- **● 当前**：Phase 6.5 Render Layer — 解耦"准确回答"与"聊天风格"，Agent 负责准确、Render 负责风格
- **下一步**：Phase 7 — 更多工具 + 配置清理 + `public_memories` 群体智慧

---

## License

MIT © [Ujikintoki](https://github.com/Ujikintoki)
