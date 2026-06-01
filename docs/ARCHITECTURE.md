# 项目架构与开发手册

> 版本 0.1.0 | 2026-05-29

---

## 目录

- [1. 项目意图](#1-项目意图)
- [2. 系统架构](#2-系统架构)
- [3. 数据流与工作流程](#3-数据流与工作流程)
- [4. 分层详解](#4-分层详解)
  - [4.1 配置层](#41-配置层)
  - [4.2 数据清洗层](#42-数据清洗层)
  - [4.3 执行与工具层](#43-执行与工具层)
  - [4.4 认知与记忆层](#44-认知与记忆层)
  - [4.5 编排层](#45-编排层计划中)
  - [4.6 接入层](#46-接入层计划中)
- [5. 技术栈与选型理由](#5-技术栈与选型理由)
- [6. 关键设计决策](#6-关键设计决策)
- [7. 目录结构](#7-目录结构)
- [8. 本地开发](#8-本地开发)
- [9. 编码规范](#9-编码规范)

---

## 1. 项目意图

Bangumi Agentic System 面向 [Bangumi](https://bgm.tv) 生态，构建一个具备以下能力的 Stateful AI Agent：

- **语义理解**：将用户的模糊自然语言（"类似《命运石之门》那种烧脑番"）映射到具体条目；
- **工具编排**：在搜索命中时获取详情，在搜索未命中时回退到向量语义检索，而非直接返回空结果；
- **持久记忆**：跨会话记住用户偏好，在后续推荐中自动应用。

系统最终以 Telegram / Discord Bot 形态运行。

---

## 2. 系统架构

系统采用分层架构，自上而下六层。当前已完成下三层（配置、清洗、工具），认知层正在开发中，编排层和接入层为后续计划。

```
┌──────────────────────────────────────────────────────┐
│  接入层      FastAPI · MCP Server · Bot Webhook      │  🚀 计划中
├──────────────────────────────────────────────────────┤
│  编排层      LangGraph ReAct 状态机 · Skill 调度      │  🚀 计划中
├──────────────────────────────────────────────────────┤
│  认知层      短期记忆 · 长期画像 · Hybrid RAG         │  ⏳ 进行中
├──────────────────────────────────────────────────────┤
│  工具层      BangumiClient · Tool Functions          │  ✅ 已完成
├──────────────────────────────────────────────────────┤
│  清洗层      Fat Model (Pydantic v2)                 │  ✅ 已完成
├──────────────────────────────────────────────────────┤
│  配置层      pydantic-settings · .env                │  ✅ 已完成
└──────────────────────────────────────────────────────┘
```

层级依赖规则：每层仅依赖其直接下层，不跨层调用。

---

## 3. 数据流与工作流程

### 3.1 当前已实现的数据流（搜索场景）

```
用户输入 "搜一下进击的巨人"
        │
        ▼
┌──────────────────┐
│  tools/bgm_tools │  search_bangumi_subject(keyword="进击的巨人")
│  (Tool 函数)      │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│  clients/        │  BangumiClient.search_subjects()
│  bgm_client      │  → POST /v0/search/subjects
└────────┬─────────┘
         │ httpx 异常在此层被 try-except 拦截
         │ 转为 {"error": "...", "status_code": N}
         │
         ▼
┌──────────────────┐
│  schemas/        │  SlimSubjectResponse.model_validate()
│  bangumi         │  → model_validator 自动展平嵌套字段
└────────┬─────────┘
         │
         ▼
  返回 JSON 字符串给 LLM
```

### 3.2 完整 Agent 工作流（编排层实现后）

```mermaid
graph TD
    U[用户输入] --> P[感知: 组装上下文]
    P --> R[推理: LLM 决策]
    R -->|需调用工具| A[行动: 执行 Tool]
    R -->|信息充足| REP[回复用户]
    A --> O[观察: 解析工具结果]
    O -->|成功| R
    O -->|失败| RETRY[重试 / 回退策略]
    RETRY -->|切换策略| A
    RETRY -->|超过上限| REP
```

工具调用失败时的回退策略：

1. `search_bangumi_subject` 未命中 → 尝试日文名重搜
2. 重搜仍未命中 → 回退到 RAG 语义检索（向量相似度匹配）
3. 网络超时 → 指数退避重试（最多 3 次）

---

## 4. 分层详解

### 4.1 配置层

**文件**: `core/config.py`

基于 `pydantic-settings.BaseSettings`，`.env` 文件自动加载。必填项缺失时启动即报错。

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    PROJECT_NAME: str = "BGM Agent"
    VERSION: str = "0.1.0"
    DATABASE_URL: str = "postgresql://myuser:mypassword@localhost:5432/bangumidb"
    BANGUMI_APP_ID: str       # 必填
    BANGUMI_APP_SECRET: str   # 必填
```

使用 `@lru_cache` 装饰的 `get_settings()` 保证全局单例。

### 4.2 数据清洗层

**文件**: `schemas/bangumi.py`

采用 "Fat Model" 策略：Pydantic 模型在反序列化阶段完成数据净化，未通过校验的数据无法流入上层。

两个模型按需渐进暴露：

| 模型 | 用途 | 字段数 |
|:---|:---|:---|
| `SlimSubjectResponse` | 搜索列表 | 8（id, type, name, name_cn, short_summary, score, rank, tags） |
| `DetailedSubjectResponse` | 条目详情 | 继承 + total_episodes, eps, volumes, collection 等 |

核心机制：

- `model_validator(mode="before")` — 在构造前展平嵌套字段（`rating.score` → `score`）和精简数组（`tags: [{name, count}]` → `tags: [str]`）
- `ConfigDict(extra="ignore")` — 严格丢弃 API 新增的未知字段
- 所有字段设置 `default` — 防御性处理缺失字段

### 4.3 执行与工具层

**文件**: `clients/bgm_client.py`, `tools/bgm_tools.py`

#### HTTP Client

`BangumiClient` 封装 `httpx.AsyncClient`，核心约束：

- **永不抛出异常**：`TimeoutException`、`HTTPStatusError`、`HTTPError` 全部在 Client 层被 `try-except` 捕获
- **结构化错误**：统一返回 `{"error": str, "status_code": int | None}`
- **可重试**：Agent 根据 `status_code` 自主决策（429 → 指数退避）

```python
# 成功路径
[SlimSubjectResponse(id=9717, name="魔法少女まどか☆マギカ", score=8.3), ...]

# 失败路径
{"error": "请求超时: ...", "status_code": None}
{"error": "HTTP 错误: ...", "status_code": 404}
```

#### Tool 函数

每个函数带有详尽的 Google Style Docstring，包含典型场景、参数说明和返回值结构：

| 函数 | Bangumi API | 关键参数 |
|:---|:---|:---|
| `search_bangumi_subject` | `POST /v0/search/subjects` | keyword, subject_type(1-6), sort |
| `get_bangumi_subject_detail` | `GET /v0/subjects/{id}` | subject_id |

验证：`test/test_tools.py` — 原生 Tool Calling 测试通过。

### 4.4 认知与记忆层

**状态**: ⏳ 开发中

#### 文本切片

**文件**: `rag/text_processor.py` — `BangumiTextProcessor`

- 基于 `tiktoken` (cl100k_base)，不依赖 LangChain / LlamaIndex
- 滑动窗口：`chunk_size=300` tokens，`chunk_overlap=50` tokens
- 清洗管道：全角空格 → 换行拍平 → 连续空白规范化

#### 向量存储

**文件**: `database/models.py` — `BangumiChunk` (SQLModel + pgvector)

```python
class BangumiChunk(SQLModel, table=True):
    id: uuid.UUID          # PK
    entity_type: str       # "subject" / "character"
    entity_id: int         # Bangumi 条目 ID
    chunk_text: str        # 纯文本正文
    embedding: list[float] # Vector(1536)，pgvector HNSW 索引
    meta_info: dict        # JSONB — tags, score 等结构化元数据
```

#### Hybrid RAG 检索策略

**正文与元数据严格分离**：

```
Query → Embedding → pgvector HNSW (cosine_distance < threshold)
                  → 召回 Top-K 候选
                  → JSONB WHERE 硬过滤 (score > N, tags @> [...])
                  → 返回最终结果
```

- 正文 (`chunk_text`) 参与向量化，用于语义召回
- 标签/评分 (`meta_info` JSONB) 不参与向量化，仅用于 SQL WHERE 硬过滤

### 4.5 编排层（计划中）

**状态**: 🚀 计划中

- LangGraph ReAct 状态图，控制 感知→推理→行动→观察 循环
- 工具调用失败自动重试 + 策略切换
- Skill Orchestrator：原子工具 → 复杂业务流（如 "生成追番报告"）
- Token 预算管理：滑动窗口 + 阈值触发摘要压缩

### 4.6 接入层（计划中）

**状态**: 🚀 计划中

- FastAPI Webhook 网关（Telegram / Discord）
- MCP Server Protocol 标准化接口
- OAuth 2.0 鉴权流转

---

## 5. 技术栈与选型理由

| 类别 | 选型 | 理由 |
|:---|:---|:---|
| 语言 | Python 3.11+ | `async/await` 原生，AI 生态第一语言 |
| Web | FastAPI 0.115+ | ASGI 异步原生，类型驱动自动校验 |
| 数据校验 | Pydantic v2 | `model_validator(mode="before")` 支持反序列化阶段清洗 |
| 配置 | pydantic-settings | .env 强类型校验，单例缓存 |
| ORM | SQLModel | Pydantic + SQLAlchemy 统一，减少模型重复 |
| Agent | LangGraph | 图结构状态管理，支持循环与分支 |
| LLM | openai SDK | 兼容 OpenAI / DeepSeek / Qwen |
| 数据库 | PostgreSQL 16 + pgvector | JSONB + HNSW 向量索引在同一查询中完成 |
| HTTP | httpx 0.28+ | 异步原生，连接池，超时控制 |
| Token | tiktoken | cl100k_base 精确 Token 计数 |
| Embedding | 智谱 embedding-3 | 1536d，中文/日文混排适配 |

### pgvector vs 独立向量库

选择 pgvector 的核心原因：Hybrid RAG 需要向量检索 + JSONB 硬过滤在一条 SQL 中完成。独立向量库需要在应用层两次查询 + 手动合并。

### tiktoken vs 字符级切片

LLM 上下文窗口按 Token 计算。tiktoken 使用与模型一致的 BPE 编码器保证精度，字符级切片误差大。

---

## 6. 关键设计决策

### Fat Model：在反序列化阶段阻断脏数据

Thin Model 可能让 LLM 看到"看起来合法"但缺少关键字段的数据。Fat Model 通过 `model_validator` + `extra="ignore"` 物理阻断。

### 正文与元数据分离 Embedding

标签/评分等结构化字段与自然语言不同质，混合 Embedding 稀释语义信号。分离策略：语义靠向量，精确过滤靠 SQL。

### 异步优先

所有网络 I/O 路径使用 `async/await`。高并发 Bot 场景下避免单用户请求阻塞其他用户。

### 自研切片不依赖 LangChain

tiktoken 已是 OpenAI SDK 的传递依赖，引入 LangChain 仅为切片是过度设计。自研实现可对 Bangumi 数据做针对性调优。

---

## 7. 目录结构

```
bgm-agent-dev/
├── main.py                  # FastAPI 入口 + /health
├── requirements.txt         # Python 依赖
├── bangumi_openapi.yaml     # Bangumi OpenAPI 规范
│
├── core/
│   └── config.py            # 全局配置 (pydantic-settings)
│
├── schemas/
│   └── bangumi.py           # Fat Model 数据清洗 (Pydantic v2)
│
├── clients/
│   └── bgm_client.py        # 防爆 HTTP Client (httpx)
│
├── tools/
│   └── bgm_tools.py         # LLM Tool 函数 (Google Style Docstring)
│
├── database/
│   ├── engine.py            # Engine + pgvector 初始化
│   └── models.py            # ORM 模型 (SQLModel + pgvector)
│
├── rag/
│   ├── text_processor.py    # 滑动窗口文本切片 (tiktoken)
│   └── ingestion.py         # 批量向量摄入管道
│
├── test/
│   ├── test_client.py       # HTTP Client 测试
│   ├── test_tools.py        # Tool Calling 测试
│   ├── test_dataclean.py    # Fat Model 清洗测试
│   ├── test_rawtext.py      # 文本预处理测试
│   └── mock_data/           # 离线 Mock 数据
│
└── docs/
    └── ARCHITECTURE.md      # 项目架构与开发手册（本文件）
```

---

## 8. 本地开发

### 环境准备

- Python 3.11+
- Docker Desktop
- Bangumi OAuth 凭证（[注册](https://bgm.tv/dev)）

### 启动

```bash
# 1. PostgreSQL + pgvector
docker run -d --name bangumi-pg \
  -e POSTGRES_USER=myuser -e POSTGRES_PASSWORD=mypassword \
  -e POSTGRES_DB=bangumidb -p 5432:5432 \
  pgvector/pgvector:pg16

# 2. 依赖
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 3. 配置
cp .env.example .env  # 填入 BANGUMI_APP_ID、BANGUMI_APP_SECRET

# 4. 启动
uvicorn main:app --reload --port 8000

# 5. 验证
curl http://localhost:8000/health
# → {"status":"ok","environment":"development","version":"0.1.0"}
```

### 运行测试

```bash
cd test
python test_client.py    # HTTP Client（需网络）
python test_tools.py     # Tool Calling（需网络）
python test_dataclean.py # Fat Model 清洗（离线）
python test_rawtext.py   # 文本切片（离线）
```

---

## 9. 编码规范

| 规则 | 说明 |
|:---|:---|
| 异步优先 | 所有网络 I/O 方法使用 `async/await` |
| 防御性编程 | 第三方 API 调用必须 `try-except`，返回结构化错误 |
| 类型安全 | Tool 函数入参/返回值有 Type Hint + Google Style Docstring |
| 无状态 | Token 与记忆不得全局变量硬编码，依赖持久化存储 |
| 模糊澄清 | 用户输入语义模糊时主动询问，不假设默认值 |

## 10. LangGraph DAG

                 START
                   │
                   ▼
            reasoning_node
                   │
                   ▼ (条件边: needs_tool?)
            ┌──────┴──────┐
            │             │
         tool_node     (skip)
            │             │
            └──────┬──────┘
                   ▼
             critic_node
                   │
                   ▼ (条件边: PASS? 超限?)
            ┌──────┴──────┐
            │             │
           END      reasoning_node
