<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/FastAPI-0.115+-009688?style=for-the-badge&logo=fastapi&logoColor=white" alt="FastAPI">
  <img src="https://img.shields.io/badge/PostgreSQL-16_%2B_pgvector-4169E1?style=for-the-badge&logo=postgresql&logoColor=white" alt="PostgreSQL">
  <img src="https://img.shields.io/badge/LangGraph-ReAct-ff6b35?style=for-the-badge" alt="LangGraph">
  <img src="https://img.shields.io/badge/tests-530+-brightgreen?style=for-the-badge" alt="Tests">
  <img src="https://img.shields.io/badge/version-0.2.0--beta-informational?style=for-the-badge" alt="Version">
</p>

<h1 align="center">BGM Agent</h1>
<p align="center">
  <strong>Bangumi 站内 AI 看板娘</strong><br>
  <sub>FastAPI + LangGraph 异质拓扑 + DeepSeek function-calling + PostgreSQL/pgvector</sub>
</p>

---

## 目录

- [核心特性](#核心特性)
- [快速开始](#快速开始)
- [API](#api)
- [配置](#配置)
- [架构](#架构)
- [开发](#开发)
- [文档索引](#文档索引)
- [License](#license)

---

## 核心特性

- **4 种人格 × 2 种深度** — 通过 `output_style` + `depth` 参数切换。两层独立管线：Character Card（System Prompt）决定思考方式，Render Node（独立 LLM 调用）决定语言风格
- **异质拓扑（Pipeline + ReAct）** — fetch/realtime/profile 走确定性 pipeline，explore/discuss 走 ReAct 自主探索，chat 直通渲染
- **隐式终止** — 标准 function calling，LLM 输出文本（无 tool_calls）= 结束，无显式终止工具
- **16 个工具** — Bangumi 条目搜索与详情、角色与声优、每日放送、热门趋势、单集讨论、用户画像、本地 RAG 语义搜索（13 个无条件 + 3 个需 Access Token）
- **多轮记忆** — L1 滑动窗口（fast 10000 / deep 16000 tok）+ 工具结果压缩 + L2 跨会话语义召回（pgvector + 时间衰减）
- **SSE 流式输出** — `/chat/stream` 端点，按节点推送推理过程
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

# 3. 配置（最小可跑：只需要 LLM_API_KEY）
cp .env.example .env
# 编辑 .env，至少填：
#   LLM_MODEL=deepseek-v4-flash
#   LLM_API_KEY=sk-your-key
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
| `depth` | `"fast"` \| `"deep"` | `"fast"` | 深度控制 |
| `output_style` | `"bangumi"` \| `"bangumi_cold"` \| `"bangumi_cute"` \| `"neutral"` | `"bangumi"` | 人格模式 |
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
| `telemetry` | `dict` | 可观测性数据（仅 `DEV_MODE=true`） |

### POST /chat/stream

同上参数，返回 SSE（`text/event-stream`），按节点推送 `reasoning` → `tool` → `render` 事件。

---

## 配置

`.env` 中的关键环境变量：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `LLM_MODEL` | — | 推荐 `deepseek-v4-flash` |
| `LLM_API_KEY` | — | DeepSeek / OpenAI / Azure API key |
| `LLM_BASE_URL` | — | 自定义 endpoint |
| `DATABASE_URL` | `postgresql://myuser:mypassword@localhost:5432/bangumidb` | PostgreSQL 连接 |
| `ZHIPU_API_KEY` | — | 智谱 embedding（RAG + L2 记忆需要） |
| `BANGUMI_ACCESS_TOKEN` | — | Bangumi API token（用户相关工具，可选） |
| `MEMORY_ENABLED` | `True` | L2 跨会话记忆开关 |
| `DEV_MODE` | `False` | 开启后 `/chat` 响应附带 telemetry |

完整配置项见 `.env.example` 和 `core/config.py`。

---

## 架构

```
START → classify_node ─┬─ chat ──────────────→ END
                        ├─ fetch ─────────────→ fetch_search → fetch_detail → synthesize → END
                        ├─ realtime/profile ──→ search → synthesize → END
                        └─ explore/discuss ───→ reasoning_node ⇄ tool_node → END
```

| 层 | 职责 |
|-----|------|
| **编排层** | Graph 拓扑、路由、意图分类、策略 |
| **人格层** | CharacterProfile + Render 风格转换 |
| **记忆层** | L1 滑动窗口 + 压缩 + L2 语义召回 |
| **数据层** | 工具函数 + HTTP Client + RAG + pgvector |

上层依赖下层，下层不感知上层。详细架构、编码规范、调参速查见 [`CLAUDE.md`](CLAUDE.md)。

---

## 开发

```bash
# 全部测试（需要 PostgreSQL + pgvector）
pytest test/ -v

# 跳过数据库依赖
pytest test/ --ignore=test/test_rag.py -v

# 仅记忆系统
pytest test/test_memory.py test/test_memory_manager.py test/test_phase5_l1.py -v

# 代码格式化
ruff format .

# 代码检查
ruff check .
```

---

## 文档索引

| 文档 | 内容 |
|------|------|
| [`CLAUDE.md`](CLAUDE.md) | 架构详解、编码规范、调参速查、已知问题 |
| [`docs/design/ROADMAP.md`](docs/design/ROADMAP.md) | 当前状态、待解决问题、路线图 |
| [`docs/design/architecture-evolution.md`](docs/design/architecture-evolution.md) | 架构演化历史（Phase 1–10） |
| [`docs/design/`](docs/design/) | 设计决策记录 |
| [`docs/eval/`](docs/eval/) | 评测体系设计 |

---

## License

MIT © [Ujikintoki](https://github.com/Ujikintoki)
