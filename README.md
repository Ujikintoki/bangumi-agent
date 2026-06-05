<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/FastAPI-0.115+-009688?style=for-the-badge&logo=fastapi&logoColor=white" alt="FastAPI">
  <img src="https://img.shields.io/badge/PostgreSQL-16%20%2B%20pgvector-4169E1?style=for-the-badge&logo=postgresql&logoColor=white" alt="PostgreSQL">
  <img src="https://img.shields.io/badge/LangGraph-ReAct-ff6b35?style=for-the-badge" alt="LangGraph">
  <img src="https://img.shields.io/badge/tests-274%20passed-success?style=for-the-badge" alt="Tests">
</p>

<h1 align="center">BGM Agent</h1>
<p align="center">
  <strong>Bangumi 智能助手 — 自然语言理解 · 多工具编排 · 语义检索 · 长期记忆</strong><br>
  <sub>为 <a href="https://bgm.tv">bangumi.tv</a> 生态构建的 Stateful AI Agent</sub>
</p>

---

## 项目状态

| 层级 | 状态 | 说明 |
|---|---|---|
| 接入层 (FastAPI + `/chat`) | 🚀 Phase 3 | Agent 对话端点开发中 |
| 编排层 (LangGraph ReAct) | ⏳ 拓扑就位 | 图谱结构完成，待接入真实 LLM |
| 工具层 (12 个 @tool) | ✅ Phase 2 | 全部就位，走 p1 API |
| 认知层 (RAG + 记忆) | ✅ | 混合检索就位；长期画像待 Phase 4 |
| 清洗层 (Sanitizer) | ✅ | BBCode 剥离、噪音过滤、白名单提取 |
| 配置层 (pydantic-settings) | ✅ | 全局单例 |

---

## 信息架构

```
用户查询
├── LLM 内置知识        → 不触发 Tool（"顶上战争是哪两方？"）
├── Bangumi API Tools   → 动态/实时数据（评论、热度、日历、角色声优）
└── RAG 语义检索         → 模糊发现 + 跨实体关联 + API 搜索回退
```

## 可用工具 (12 个)

| 工具 | 用途 | 认证 |
|---|---|---|
| `search_bangumi_subject` | 条目/角色/人物搜索 | — |
| `get_bangumi_subject_detail` | 条目完整详情 | — |
| `get_calendar` | 每日放送排期 | — |
| `get_trending_topics` | 全站热门趋势 | — |
| `get_episode_comments` | 单集详情 + 吐槽箱 | — |
| `get_subject_discussion` | 条目评论/评测/讨论/剧集 | — |
| `get_entity_comments` | 角色/人物社区评论 | — |
| `get_subject_characters` | 角色列表 + 声优 | — |
| `search_local_bangumi` | RAG 语义搜索 | — |
| `get_user_timeline` | 用户时光机动态 | 🔑 |
| `get_user_profile` | 用户多维度画像 | 🔑 |
| `get_blog` | 日志正文 + 评论 + 关联条目 | 🔑 |

---

## 目录结构

```
bgm-agent-dev/
├── main.py                     # FastAPI 入口 (/health)
├── core/
│   └── config.py               # pydantic-settings 全局单例
├── schemas/
│   └── tools_input.py          # 12 个 Pydantic v2 Tool Schema
├── clients/
│   ├── base.py                 # BaseClient (httpx, 重试, auth)
│   ├── client.py               # BangumiClient (10 个 p1 API 方法)
│   └── sanitizers.py           # 纯函数清洗器 (BBCode, 噪音, 截断)
├── tools/
│   └── bgm_tools.py            # 12 个 @tool + 动态注册表
├── agent/
│   ├── state.py                # AgentState TypedDict
│   ├── nodes.py                # reasoning / tool / critic 节点 (占位)
│   └── graph.py                # LangGraph ReAct 图谱
├── database/
│   ├── engine.py               # 连接池 + HNSW/GIN 索引 DDL
│   └── models.py               # RagEntity 单表多态 + Pydantic Meta
├── rag/
│   ├── text_processor.py       # tiktoken 滑动窗口分块
│   ├── ingestion.py            # 语义前缀 + 关联边重排 + 批量写入
│   ├── retriever.py            # hybrid_search: 标量→向量→分桶排序
│   └── utils.py                # ZhipuAiClient 初始化
├── test/                       # 274 tests, 6 文件, 零依赖 (仅 RAG 需 DB)
│   ├── conftest.py             # 共享 fixtures
│   ├── test_schemas.py         # 74 tests
│   ├── test_sanitizers.py      # 62 tests
│   ├── test_client.py          # 21 tests
│   ├── test_tools.py           # 64 tests
│   └── test_rag.py             # 53 tests (含 E2E: 摄入→检索)
└── docs/
    ├── ARCHITECTURE.md         # 架构详解
    ├── RAG_STRATEGY.md         # RAG 策略文档
    ├── RAG_CONTEXT.md          # RAG 模块上下文 (供子 session)
    └── phase3/                 # Phase 3 开发手册 (8 文档)
```

---

## 快速开始

```bash
# 1. PostgreSQL + pgvector
docker run -d --name bangumi-pg \
  -e POSTGRES_USER=myuser -e POSTGRES_PASSWORD=mypassword \
  -e POSTGRES_DB=bangumidb -p 5432:5432 pgvector/pgvector:pg16

# 2. 依赖
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 3. 配置
cp .env.example .env
# 必填: ZHIPU_API_KEY (RAG 检索)
# 可选: BANGUMI_ACCESS_TOKEN (用户画像/日志/时光机)

# 4. 启动
uvicorn main:app --reload --port 8000
curl http://localhost:8000/health
# → {"status":"ok","environment":"development","version":"0.1.0"}
```

---

## 运行测试

```bash
# 全量 (RAG 测试需要 PostgreSQL + 智谱 API)
pytest test/ -v

# 无需外部依赖 (schemas / sanitizers / tools / client mock)
pytest test/test_schemas.py test/test_sanitizers.py test/test_tools.py test/test_client.py -v

# RAG 专项 (需要 PostgreSQL + pgvector + ZHIPU_API_KEY)
pytest test/test_rag.py -v
```

---

## 技术栈

| 类别 | 选型 |
|---|---|
| 语言 | Python 3.11+ |
| Web | FastAPI 0.115+ (ASGI) |
| 数据校验 | Pydantic v2 |
| Agent 编排 | LangGraph (StateGraph, ReAct) |
| 数据库 | PostgreSQL 16 + pgvector (HNSW) |
| Embedding | 智谱 embedding-3 (2048d) |
| HTTP | httpx (async, 重试, 指数退避) |
| Token 计数 | tiktoken (cl100k_base) |

---

## 文档

| 文档 | 说明 |
|---|---|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | 完整架构与开发手册 |
| [RAG_STRATEGY.md](docs/RAG_STRATEGY.md) | RAG 混合检索设计 |
| [RAG_CONTEXT.md](docs/RAG_CONTEXT.md) | RAG 模块全景上下文 |
| [Agent/](docs/Agent/) | Phase 3 Agent 开发手册 |

---

## 路线图

- [x] Phase 1 — 统一 Client 层
- [x] Phase 2 — 统一 Tool Schema + 补全工具 + Sanitizer + 字段对齐
- [ ] Phase 3 — Agent 接入 LLM + `/chat` 端点 + 系统提示词
- [ ] Phase 4 — 清理遗留代码 + 文档更新 + 可选增强

---

## License

MIT © [Ujikintoki](https://github.com/Ujikintoki)
