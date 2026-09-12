<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/FastAPI-0.115+-009688?style=for-the-badge&logo=fastapi&logoColor=white" alt="FastAPI">
  <img src="https://img.shields.io/badge/PostgreSQL-16_%2B_pgvector-4169E1?style=for-the-badge&logo=postgresql&logoColor=white" alt="PostgreSQL">
  <img src="https://img.shields.io/badge/LangGraph-异质拓扑-ff6b35?style=for-the-badge" alt="LangGraph">
  <img src="https://img.shields.io/badge/tests-539-brightgreen?style=for-the-badge" alt="Tests">
  <img src="https://img.shields.io/badge/version-0.1.1-informational?style=for-the-badge" alt="Version">
</p>

<h1 align="center">BGM Agent</h1>
<p align="center">
  <strong>住在 Bangumi 里的动画损友</strong><br>
  <sub>FastAPI + LangGraph 异质拓扑（Pipeline + ReAct）+ DeepSeek function-calling + PostgreSQL/pgvector</sub>
</p>

---

BGM Agent 不是"帮你查数据的 AI"——它是有自己的品位和脾气、可以被反驳、记得你聊过什么的站内损友。会推荐一部并说出理由，而不是列 20 个条目；会说"我觉得这部过誉了"，也会承认"你说得对，我重新看了一下"。完整产品愿景见 [`docs/design/claude-on-bangumi-vision.md`](docs/design/claude-on-bangumi-vision.md)。

---

## 目录

- [核心特性](#核心特性)
- [快速开始](#快速开始)
- [API](#api)
- [配置](#配置)
- [架构](#架构)
- [测试](#测试)
- [RAG 管线](#rag-管线)
- [项目结构](#项目结构)
- [文档索引](#文档索引)
- [License](#license)

---

## 核心特性

- **7 intent 异质拓扑** — chat 直通渲染；fetch/realtime/profile 走确定性 Pipeline（独立 subgraph + 专属 prompt + 专属工具集）；explore/discuss 走 ReAct 自主探索。**隐式终止**：LLM 输出文本（无 tool_calls）即结束，无显式终止工具
- **3 种人格 × 2 种深度** — `output_style`（bangumi / bangumi_kawaii / neutral）× `depth`（fast / deep）。两层独立管线：Character Card（System Prompt）决定思考方式，Render Node（独立 LLM 调用）决定语言风格
- **16 个工具** — Bangumi 条目搜索与详情、角色/声优、每日放送、热门趋势、话题讨论、用户画像，以及本地 RAG 语义搜索（13 个无条件 + 3 个需 `BANGUMI_ACCESS_TOKEN`）
- **多轮记忆** — L1 滑动窗口（fast 10000 / deep 16000 tok，tiktoken 精确计数）+ 工具结果压缩；L2 跨会话语义召回（pgvector + 时间衰减）；session 缓存跨请求续聊
- **RAG 三通道检索** — 向量语义（"温馨的日常故事"）+ 名称精确 + 关键词硬过滤，灌入本地 pgvector，支撑 `search_local_bangumi` 工具
- **SSE 流式接口** — `/chat/stream` 保留流式形态（当前推送最终 render 事件，为逐 token 渲染预留）
- **开发者可观测性** — `DEV_MODE=true` 返回 token 统计 + 节点耗时

---

## 快速开始

### 前置条件

- Python 3.11+
- Docker（PostgreSQL + pgvector）

### 4 步启动

```bash
# 1. 克隆
git clone <repo-url> && cd bgm-agent-dev

# 2. 安装依赖
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 3. 配置（最小可跑：LLM_API_KEY 必填，ZHIPU_API_KEY 供 RAG + L2 记忆）
cp .env.example .env
# 编辑 .env：
#   LLM_API_KEY=sk-your-key
#   LLM_MODEL=deepseek-v4-flash
#   LLM_BASE_URL=https://api.deepseek.com/v1

# 4. 启动 PostgreSQL + pgvector，然后运行
docker run -d --name bangumi-pg \
  -e POSTGRES_USER=myuser -e POSTGRES_PASSWORD=mypassword \
  -e POSTGRES_DB=bangumidb -p 5432:5432 pgvector/pgvector:pg16

uvicorn main:app --reload --port 8000
```

```bash
# 发第一个请求
curl -s -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "你好，最近有什么好看的番？", "depth": "fast", "output_style": "bangumi"}'
```

---

## API

### POST /chat

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `message` | `str` | *必填* | 用户消息 |
| `depth` | `"fast"` \| `"deep"` | `"fast"` | 深度控制（迭代上限 + Token 预算） |
| `output_style` | `"bangumi"` \| `"bangumi_kawaii"` \| `"neutral"` | `"bangumi"` | 人格模式 |
| `session_id` | `str` | 自动生成 | 多轮会话 ID（L1 续聊） |
| `user_id` | `str` | `"anonymous"` | 跨会话记忆用户 ID（L2） |

| 响应字段 | 类型 | 说明 |
|----------|------|------|
| `reply` | `str` | Agent 回复 |
| `iterations` | `int` | 循环轮数 |
| `tools_used` | `list[str]` | 本轮调用的工具名称 |
| `query_intent` | `str` | 意图分类（chat/fetch/explore/discuss/realtime/profile/fallback） |
| `output_style` | `str` | 实际使用的人格 |
| `depth` | `str` | 实际使用的深度模式 |
| `telemetry` | `dict` | 可观测性数据（仅 `DEV_MODE=true`） |

### POST /chat/stream

同上参数，返回 SSE（`text/event-stream`）。当前 graph 推理为非流式，SSE 推送最终 `render` 事件 + `[DONE]`，接口形态为逐 token 渲染预留。

### GET /health

返回 `status` / `environment` / `version`。

---

## 配置

`.env` 中的关键环境变量（完整清单见 `.env.example`，定义与默认值见 `core/config.py`）：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `LLM_API_KEY` | — | DeepSeek / Azure OpenAI / OpenAI 兼容 API key（必填） |
| `LLM_MODEL` | `deepseek-v4-flash` | 与 .env.example 默认一致；OpenAI 兼容模型均可 |
| `LLM_BASE_URL` | — | 自定义 endpoint（如 `https://api.deepseek.com/v1`） |
| `DATABASE_URL` | `postgresql://myuser:mypassword@localhost:5432/bangumidb` | PostgreSQL + pgvector 连接 |
| `ZHIPU_API_KEY` | — | 智谱 embedding-2（RAG + L2 记忆需要） |
| `BANGUMI_ACCESS_TOKEN` | — | Bangumi Bearer Token（解锁 3 个用户相关工具，可选） |
| `MEMORY_ENABLED` | `True` | L2 跨会话记忆开关 |
| `DEV_MODE` | `False` | 开启后 `/chat` 响应附带 telemetry |
| `BGM_LOG_LEVEL` | `INFO` | 日志级别（`DEBUG` 可看逐节点日志） |

---

## 架构

```
START → classify_node ─┬─ chat ───────────────────────────────→ END
                        ├─ fetch → fetch_pipeline ─────────────────→ END
                        │           (search → tool → detail → tool → synthesize)
                        ├─ realtime → realtime_pipeline ──────────→ END
                        │              (search → tool → synthesize)
                        ├─ profile → profile_pipeline ────────────→ END
                        │             (search → tool → synthesize)
                        └─ explore/discuss/fallback/低置信度
                             → reasoning_node ⇄ tool_node → END
```

| 层 | 职责 |
|-----|------|
| **编排层** | Graph 拓扑、路由、意图分类、prompt 装配、护栏 |
| **人格层** | CharacterProfile + Render 风格转换 |
| **记忆层** | L1 滑动窗口 + 压缩 + L2 语义召回 + session 缓存 |
| **数据层** | 工具函数 + HTTP Client + RAG + pgvector |

上层依赖下层，下层不感知上层。详细架构、编码规范、调参速查见 [`CLAUDE.md`](CLAUDE.md)。

---

## 测试

```bash
# 全部测试（539 个，需要 PostgreSQL + pgvector）
pytest test/ -v

# 跳过数据库依赖的测试
pytest test/ --ignore=test/test_rag.py -v

# 仅记忆系统
pytest test/test_memory.py test/test_memory_manager.py test/test_phase5_l1.py -v

# 代码格式化 / 检查
ruff format .
ruff check .
```

---

## RAG 管线

```bash
# Phase 1: ID 发现 → Phase 2: 富化 + 灌入（--clear 清空旧数据）
python -m rag.cli.discover
python -m rag.cli.ingest --clear

# 离线语料准备 + 灌库自检（不产出指标）
python -m rag.cli.collect
python -m rag.cli.verify --dry-run

# 评测（--build 生成 GT + 标注模板，--evaluate 检索 + 计算指标）
python -m eval.rag_eval --build
python -m eval.rag_eval --evaluate
```

检索三通道（`hybrid_search` 向量语义 / `name_search` 名称精确 / `keyword_search` 关键词硬过滤）由生产工具 `search_local_bangumi` 统一接入。概览见 [`docs/Rag/rag_overview.md`](docs/Rag/rag_overview.md)。

---

## 项目结构

```
agent/          # 编排层（graph/state/config/nodes/routing/prompts/guardrails）+ 人格层 + 记忆层
tools/          # 16 个 LangChain 工具（bgm_tools.py）
clients/        # HTTP 客户端（指数退避重试 + sanitizers + 智谱 client）
rag/            # RAG 检索管线（ingestion / retriever / cli / eval）
database/       # SQLModel ORM + pgvector
schemas/        # Pydantic v2 工具输入 schema
core/config.py  # 全局配置（.env / 环境变量）
main.py         # FastAPI 入口（/health, /chat, /chat/stream）
eval/           # 分类器 / RAG 离线评测脚本
test/           # 539 测试 / 20 文件
docs/           # design/、eval/、memory/、Rag/、Tools/
```

---

## 文档索引

| 文档 | 内容 |
|------|------|
| [`CLAUDE.md`](CLAUDE.md) | 架构详解、编码规范、文件地图、调参速查（AI 协作入口） |
| [`ROADMAP.md`](ROADMAP.md) | 当前状态、待解决问题、路线图 |
| [`docs/design/claude-on-bangumi-vision.md`](docs/design/claude-on-bangumi-vision.md) | **产品愿景**——"住在 Bangumi 里的动画损友"，功能与人格修改的锚 |
| [`docs/architecture-blueprint.md`](docs/architecture-blueprint.md) | **宏观架构蓝图**——9 板块清单、分层接口契约、编排模式库、演化地图 |
| [`docs/design/evolution-roadmap-phase7-9.md`](docs/design/evolution-roadmap-phase7-9.md) | Phase 7-9 分层演进路线 |
| [`docs/design/`](docs/design/) | 设计决策（`R-*` 方法论与基线、`O-*` 历史文档） |
| [`docs/eval/`](docs/eval/) | 评测体系（RAG / 人格 / 记忆 / 工具可靠性 / E2E） |
| [`docs/memory/`](docs/memory/) | 记忆系统配置与调试 |
| [`docs/Rag/rag_overview.md`](docs/Rag/rag_overview.md) | RAG 管线概览 |

---

## License

MIT © [Ujikintoki](https://github.com/Ujikintoki)
