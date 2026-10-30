<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/FastAPI-0.115+-009688?style=for-the-badge&logo=fastapi&logoColor=white" alt="FastAPI">
  <img src="https://img.shields.io/badge/PostgreSQL-16_%2B_pgvector-4169E1?style=for-the-badge&logo=postgresql&logoColor=white" alt="PostgreSQL">
  <img src="https://img.shields.io/badge/LangGraph-异质拓扑-ff6b35?style=for-the-badge" alt="LangGraph">
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
- **按配置注册领域工具** — Bangumi 条目搜索与详情、角色/声优、每日放送、热门趋势与社区内容；用户工具需 `BANGUMI_ACCESS_TOKEN`，本地检索需显式开启 RAG。
- **多轮记忆** — L1 滑动窗口（fast 10000 / deep 16000 tok，tiktoken 精确计数）+ 工具结果压缩；L2 跨会话语义召回（pgvector + 时间衰减）；session 缓存跨请求续聊
- **RAG 三通道检索** — 向量语义（"温馨的日常故事"）+ 名称精确 + 关键词硬过滤，灌入本地 pgvector，支撑 `search_local_bangumi` 工具
- **SSE 流式接口** — `/chat/stream` 保留流式形态（当前推送最终 render 事件，为逐 token 渲染预留）
- **开发者可观测性** — `DEV_MODE=true` 返回 token 统计 + 节点耗时

---

## 快速开始

### 本机基础模式

使用 Python 3.11–3.13（本轮干净安装验证为 3.12）及 uv，不需要 Docker、PostgreSQL 或 embedding。

```bash
uv sync --locked --no-dev
cp .env.example .env
# 编辑本机 .env：设置 LLM_API_KEY、LLM_MODEL，以及供应商需要的 LLM_BASE_URL
# 基础模式保持 MEMORY_ENABLED=false、RAG_ENABLED=false
uv run --no-sync uvicorn main:app --host 127.0.0.1 --port 8000
```

启动命令会持续占用终端；关闭终端或结束进程后，网页标签仍可能保留，但服务已经离线。
网页断连后可用“刷新状态”重新检查连接；它会保留未发送的问题和背景编辑。
背景编辑需要点击“保存背景”才参与后续请求。当前不包含系统级服务托管或自动重启。

打开 http://127.0.0.1:8000/。网页显示模型配置缺项，支持快速/深入对话、新对话及失败反馈。
启用认证的实例在页面填写 Bearer 凭据；凭据仅保留在当前页面内存中。
网页会话保存在 `.local-data/sessions.sqlite3`（可用 `SESSION_DB_PATH` 改位置）；支持列表、返回、重启续聊及删除。
只恢复已完成轮；中断轮会显示状态，不自动重放工具。旧 `/chat`、`/chat/stream` 仍是兼容的临时会话接口。
背景面板可显式保存条目链接、材料、纠正、会话偏好和剧透范围，总长最多8000字；不会自动写长期画像。
名称和普通聊天内的链接仍交给 Agent 理解；背景面板中的 Bangumi 作品/人物/角色链接会校验并保存类型与 ID。
不承诺自动读取任意外链正文；粘贴材料与用户立场保持分开。
匿名模式仅用于本机回环地址，不要绑定局域网或公网。

已验收真实网页对话、公开条目评分查询、保存背景、重启恢复和续聊；具体环境与边界见
[本机网页验收记录](docs/Test/local-web-acceptance-2026-10-29.md)。这不代表全部模型或所有领域任务通过。
当前版本的页面、重启恢复、取消与故障边界复验见[集成验收](docs/Test/local-web-integrated-acceptance-2026-10-31.md)。

### 可选本地数据能力

需要原有 RAG/L2 时使用 `uv sync --locked --extra local-data`，自行准备隔离的 PostgreSQL、
对应语料和 embedding 配置，再显式开启 `RAG_ENABLED` 或 `MEMORY_ENABLED`。
启用任一项会在启动时初始化原有数据库及迁移；基础模式不会执行这一步。
这些能力不承担网页会话历史的恢复。不要把现有真实数据库用于测试。

直接依赖统一维护于 pyproject.toml，uv.lock 固定版本；requirements.txt 是包含可选能力及验证工具的兼容导出，
通过 `uv export --all-extras --group dev --no-hashes --output-file requirements.txt` 更新，不手工编辑。

---

## API

### 网页会话接口

`GET /api/readiness` 返回模型配置缺项、能力开关与本机保存位置，不返回密钥。
`POST/GET /api/sessions` 创建/列出会话；`GET/DELETE /api/sessions/{id}` 读取/删除。
`PATCH /api/sessions/{id}/context` 更新用户确认的背景；写操作携带当前 `version`，删除用查询参数。
`POST /api/sessions/{id}/turns` 默认登记后返回202，由应用继续执行，网页自动查询；`?wait=true` 可等待结果。接收 `request_id`（UUID）、`version`、`message`、`depth` 和可选 `output_style`。
`POST /api/sessions/{id}/turns/{request_id}/cancel` 明确取消，等待服务端确认。
`GET /api/sessions/{id}/turns/{request_id}` 查询执行状态、固定原因及已保存结果；同 ID 同输入重放不会重新生成，输入冲突返回409。
会话接口沿用 Bearer 归属，拒绝跨来源网页访问；匿名访问只接受回环主机。
回复与结局在 SQLite 提交成功后返回；存储故障返回503，不宣称已保存。删除后在途结果不能恢复正文。
刷新状态可找回已生成但未送达的回答；历史依据带保存时间，不代表当前数值。

### POST /chat

私有多用户预览设置 `AUTH_ENABLED=true`、`API_USER_TOKENS`（用户 ID 到独立随机凭据的 JSON 对象）。
凭据至少 32 个 ASCII 字符、不得含空白；管理员私下分发，客户端用 `Authorization: Bearer <个人凭据>`。
这不是 Bangumi OAuth，也不使用 `BANGUMI_ACCESS_TOKEN` 识别本服务用户。
默认关闭认证仅允许匿名聊天，拒绝自报实名；`ENVIRONMENT=production` 必须开启认证。
原有实名客户端需增加 Bearer 头，否则返回 403。无效/缺失凭据返回 401，同会话并发返回 409。
认证/会话缓存/限流仍仅支持单应用进程，公开部署尚待验收。
匿名 session ID 相当于续聊凭据，应保密；旧内存会话重启后不保留。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `message` | `str` | *必填* | 用户消息 |
| `depth` | `"fast"` \| `"deep"` | `"fast"` | 深度控制（迭代上限 + Token 预算） |
| `output_style` | `"bangumi"` \| `"bangumi_kawaii"` \| `"neutral"` | `"bangumi"` | 人格模式 |
| `session_id` | `str` | 自动生成 | 多轮会话 ID（L1 续聊） |
| `user_id` | `str` | `"anonymous"` | 兼容字段；实名必须与 Bearer 身份一致，关闭认证时仅匿名 |

| 响应字段 | 类型 | 说明 |
|----------|------|------|
| `reply` | `str` | Agent 回复 |
| `session_id` | `str` | 服务端实际会话 ID，后续请求原样传回 |
| `iterations` | `int` | 循环轮数 |
| `tools_used` | `list[str]` | 本轮调用的工具名称 |
| `query_intent` | `str` | 意图分类（chat/fetch/explore/discuss/realtime/profile/fallback） |
| `output_style` | `str` | 实际使用的人格 |
| `depth` | `str` | 实际使用的深度模式 |
| `telemetry` | `dict` | 可观测性数据（仅 `DEV_MODE=true`） |

### POST /chat/stream

同上参数，返回 SSE（`text/event-stream`）。当前 graph 推理为非流式，SSE 的成功/执行错误事件携带 `session_id`；成功时先存 L1 再推送最终 `render` 事件 + `[DONE]`，接口形态为逐 token 渲染预留。

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
| `SESSION_DB_PATH` | `.local-data/sessions.sqlite3` | 本机可见会话、背景及回复依据 |
| `RAG_ENABLED` | `False` | 注册本地检索工具并初始化其数据库 |
| `MEMORY_ENABLED` | `False` | L2 跨会话记忆开关 |
| `TRACE_LEVEL` | `metadata` | `off` 关闭；默认元数据不采输入输出正文 |
| `TRACE_PATH` | `.local-data/observability` | 独立 Trace 索引、私有 artifact 与诊断 scope |
| `DEV_MODE` | `False` | `/chat` 返回从统一 Trace 派生的兼容摘要 |
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

上层依赖下层，下层不感知上层。开发约束见 [`AGENTS.md`](AGENTS.md)，详细边界见[架构蓝图](docs/architecture-blueprint.md)。

---

## 测试

```bash
# 先准备编码缓存（此步需要下载公开文件，不调用模型或数据库）
TIKTOKEN_CACHE_DIR=/private/tmp/bgm-agent-tiktoken python -c 'import tiktoken; tiktoken.get_encoding("cl100k_base")'

# 已审阅的离线单元测试集：不加载 .env，阻断网络与 PostgreSQL
TIKTOKEN_CACHE_DIR=/private/tmp/bgm-agent-tiktoken python scripts/test_offline.py

# 全量测试需独立数据库和显式外部服务配置，不能在真实库直接运行
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
test/           # 单元与集成测试，数量以收集结果为准
docs/           # design/、eval/、memory/、Rag/、Tools/
```

---

## 文档索引

日常讨论先读 [产品基线](PRODUCT.md) → [当前工作](ROADMAP.md)。完整阅读顺序与维护状态见
[文档地图](docs/README.md)；旧探索、调研、验收和历史计划不作为另一份当前任务清单。

| 文档 | 内容 |
|------|------|
| [`PRODUCT.md`](PRODUCT.md) | 当前产品定位、边界与价值信号 |
| [`AGENTS.md`](AGENTS.md) | 轻量开发规则与安全边界（AI 协作入口） |
| [`ROADMAP.md`](ROADMAP.md) | 当前状态、待解决问题、路线图 |
| [`docs/design/claude-on-bangumi-vision.md`](docs/design/claude-on-bangumi-vision.md) | 7 月产品愿景——长期北极星与历史来源 |
| [`docs/architecture-blueprint.md`](docs/architecture-blueprint.md) | **宏观架构蓝图**——9 板块清单、分层接口契约、编排模式库、演化地图 |
| [`docs/design/evolution-roadmap-phase7-9.md`](docs/design/evolution-roadmap-phase7-9.md) | Phase 7-9 分层演进路线 |
| [`docs/design/`](docs/design/) | 设计决策（`R-*` 方法论与基线、`O-*` 历史文档） |
| [`docs/eval/`](docs/eval/) | 评测体系（RAG / 人格 / 记忆 / 工具可靠性 / E2E） |
| [`docs/memory/`](docs/memory/) | 记忆系统配置与调试 |
| [`docs/Rag/rag_overview.md`](docs/Rag/rag_overview.md) | RAG 管线概览 |

---

## License

MIT © [Ujikintoki](https://github.com/Ujikintoki)

网页连接状态回归（开发验证，需 Node.js；运行网页不需要 Node.js）：
`node --test test/web_connection.test.cjs`。

请求状态、取消、超时与故障测试见[轮次生命周期合同](docs/Architecture/turn-lifecycle.md)。
隔离的真实HTTP/强杀重启验收：`uv run --no-sync python test/lifecycle_http_probe.py`（需回环网络，不调用真实模型）。

### 本机 Agent Trace

默认采集统一 Trace 元数据；`TRACE_LEVEL=off` 关闭采集，`TRACE_PATH` 指定独立观测目录。
详细输入输出需要在提交轮次前以本机命令授予限时会话 scope；DEV_MODE 只返回兼容摘要。
查询、脱敏导出、保留清理及删除边界见[Trace 维护入口](docs/Architecture/agent-trace-design.md#13-当前维护入口2026-10-30)。
无付费合成示例：`python scripts/trace_synthetic.py --output /private/tmp/agent-trace-example.json`（需已准备 tokenizer 缓存）。
