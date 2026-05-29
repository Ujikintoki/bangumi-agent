## 项目愿景与定位 (Project Vision)

本项目旨在构建一个具备长期记忆、多工具调用、语义检索（RAG）以及标准协议接入（MCP）能力的 **Stateful AI Agent** 系统。该系统以 Bangumi（番组计划）生态为核心业务域，充当用户的"智能化追番管家"。系统不仅能处理传统的 API 增删改查，更能理解模糊语义、编排复杂技能（Skills），并持久化用户偏好。

---

## 系统边界与上下文交互 (System Context & Boundaries)

系统作为一个独立运行的微服务中枢，连接用户、大语言模型以及外部业务系统。

### 内部执行器 (Internal Actor)

- **Agent Core**: 系统大脑，负责意图路由、ReAct 循环、工具调度与上下文截断/摘要。

### 外部依赖 (External Dependencies)

- **Bangumi Open API (OAuth 2.0)**: 提供核心业务数据与用户鉴权。系统需妥善保管 Access Token 并实现无感刷新。
- **LLM Providers**: 提供逻辑推理能力。支持云端 API（OpenAI/Claude）或利用 Apple M2 芯片等统一内存架构部署本地化模型（如通过 MLX 或 Ollama 运行 Llama 3）进行低延迟推理。
- **MCP Client** (如 Claude Desktop): 作为可选的标准化前端入口，通过 Model Context Protocol 发起调用。

---

## 核心架构拆解 (Core Architecture Breakdown)

系统架构采用分层设计（Layered Architecture），严格遵循单一职责原则（SRP）：

### 接入与表现层 (Interface & Entrypoint Layer)

- **Web UI** (Streamlit/Gradio): 提供开箱即用的多轮对话交互界面，渲染结构化番剧卡片。
- **MCP Server Protocol**: 提供标准的 MCP 接口，将底层的 Bangumi 技能暴露给第三方兼容客户端。
- **RESTful API** (FastAPI): 处理 OAuth 2.0 回调路由 (`/callback`)，以及前端组件的静态资源请求。

### 智能编排层 (Orchestration Layer — The Brain)

- **ReAct State Machine** (基于 LangGraph): 管理 Agent 的状态流转图。控制 **感知 → 推理 → 行动 → 观察** 的完整生命周期，处理工具调用失败时的循环重试与熔断机制。
- **Skill Orchestrator**: 将多个原子级的 Tools 组装为复杂业务流。例如："生成当季追番报告" = 获取看过的番剧 → 并发查询评分 → 生成总结。

### 认知与记忆层 (Cognitive & Memory Layer)

- **Short-Term Memory** (会话上下文): 维护当前 Session 的消息历史。采用滑动窗口算法，当 Token 达到阈值时自动触发摘要压缩。
- **Long-Term Profiling** (长期画像): 异步守护进程。在对话结束后提取业务实体与用户偏好，存入关系型数据库。
- **RAG 检索管道** (Vector Retrieval): 针对模糊查询设计。抓取站内高分番剧的剧情简介和高赞长评进行向量化存储，支持通过余弦相似度召回长尾番剧。

### 执行与工具层 (Execution & Tooling Layer)

- **API Wrapper**: 封装带有规范 User-Agent 的 HTTP Client。
- **Data Sanitizer** (防幻觉拦截器): 利用 Pydantic 对 Bangumi API 返回的庞大 JSON 进行强类型校验与字段精简，仅向上层暴露 LLM 需要的核心维度（如 `id`, `name`, `rating`, `summary`），防止上下文污染。

### 鉴权与安全层 (Auth & Security Layer)

- **OAuth 2.0 State Manager**: 独立的状态机，负责 code 换取 `access_token`，监控过期时间，并利用 `refresh_token` 保证系统长期稳定运行。

---

## 技术栈选型基准 (Technology Stack)

| 类别           | 选型                                                       |
| -------------- | ---------------------------------------------------------- |
| 核心框架       | Python 3.11+, FastAPI (ASGI 高性能并发)                    |
| Agent 编排     | LangGraph (图结构状态管理)                                 |
| 数据模型与清洗 | Pydantic v2                                                |
| 本地存储       | SQLite (关系型与用户状态), ChromaDB / FAISS (轻量级向量库) |
| 网络通信       | httpx (支持异步 HTTP 请求)                                 |
| 环境与机密管理 | pydantic-settings / .env                                   |

---

## 开发规范与代码生成约束 (Vibe Coding Directives)

为确保代码 Agent 稳定输出，整个工程遵循以下生成约束：

| 约束             | 说明                                                                                                                                                                                                   |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **异步优先**     | 所有涉及网络 I/O 的方法（API 请求、数据库读写、LLM 调用）必须使用 `async/await`。                                                                                                                      |
| **防御性编程**   | 调用第三方 API 时必须带有 `try-except` 块，捕获异常后需返回标准的 JSON 格式错误说明，交由 Agent 决定是否重试。                                                                                         |
| **边缘情况处理** | 处理第三方 API 响应时需考虑：API 限流（429）时实现指数退避重试；网络超时（timeout）时返回可读的错误提示而非崩溃；空数据（空列表或 `None`）时优雅降级而非报错；字段缺失时使用默认值或跳过而非中断流程。 |
| **模糊意图处理** | 当用户请求语义模糊或信息不完整时，Agent 应主动询问澄清关键参数（如番剧名称、年份、类型）而非盲目假设默认值，确保交互的准确性和可预期性。                                                               |
| **类型安全**     | 所有 Tool 的入参和返回值必须有明确的 Type Hint，并配有详细的 Docstring（大模型依赖此判断工具用途）。                                                                                                   |
| **无状态微服务** | 鉴权 Token 与记忆不得以全局变量硬编码在内存中，必须依赖持久化存储层。                                                                                                                                  |
