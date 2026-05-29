<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/FastAPI-0.115+-009688?style=for-the-badge&logo=fastapi&logoColor=white" alt="FastAPI">
  <img src="https://img.shields.io/badge/PostgreSQL-16-4169E1?style=for-the-badge&logo=postgresql&logoColor=white" alt="PostgreSQL 16">
  <img src="https://img.shields.io/badge/pgvector-HNSW-important?style=for-the-badge" alt="pgvector">
  <img src="https://img.shields.io/badge/License-MIT-yellow?style=for-the-badge" alt="License: MIT">
</p>

<h1 align="center">Bangumi Agentic System</h1>
<p align="center"><strong>面向 Bangumi 生态的 Stateful AI Agent —— 语义理解、工具编排、持久记忆</strong></p>

---

## 项目意图

构建一个具备自然语言推理、跨域工具调度和长程记忆能力的 AI Agent，充当用户在 [Bangumi](https://bgm.tv) 生态中的智能化助手。

核心能力：理解模糊语义 → 编排多步工具调用 → 记住跨会话偏好。

---

## 架构

```
┌──────────────────────────────────────────┐
│  接入层    FastAPI · Bot Webhook          │  🚀
├──────────────────────────────────────────┤
│  编排层    LangGraph ReAct · Skill 调度   │  🚀
├──────────────────────────────────────────┤
│  认知层    短期记忆 · 长期画像 · RAG       │  ⏳
├──────────────────────────────────────────┤
│  工具层    BangumiClient · Tool Functions │  ✅
├──────────────────────────────────────────┤
│  清洗层    Fat Model (Pydantic v2)        │  ✅
├──────────────────────────────────────────┤
│  配置层    pydantic-settings              │  ✅
└──────────────────────────────────────────┘
```

### 当前数据流（搜索场景）

```
用户输入 → search_bangumi_subject()
         → BangumiClient (httpx, 异常在此层全拦截)
         → SlimSubjectResponse (Pydantic 反序列化清洗)
         → JSON 字符串返回 LLM
```

### 工具调用失败回退

搜索未命中 → 日文名重搜 → RAG 语义检索 → 超时指数退避（最多 3 次）

---

## 目录结构

```
bgm-agent-dev/
├── main.py                  # FastAPI 入口
├── core/config.py           # 全局配置 (pydantic-settings)
├── schemas/bangumi.py       # Fat Model 数据清洗 (Pydantic v2)
├── clients/bgm_client.py    # 防爆 HTTP Client (httpx)
├── tools/bgm_tools.py       # LLM Tool 函数
├── database/
│   ├── engine.py            # Engine + pgvector
│   └── models.py            # ORM (SQLModel + pgvector)
├── rag/
│   ├── text_processor.py    # 滑动窗口文本切片 (tiktoken)
│   └── ingestion.py         # 批量向量摄入
├── test/                    # 测试 + Mock 数据
└── docs/ARCHITECTURE.md     # 详细架构文档
```

---

## 快速开始

```bash
# PostgreSQL + pgvector
docker run -d --name bangumi-pg \
  -e POSTGRES_USER=myuser -e POSTGRES_PASSWORD=mypassword \
  -e POSTGRES_DB=bangumidb -p 5432:5432 pgvector/pgvector:pg16

# 依赖 & 配置
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # 填入 BANGUMI_APP_ID / BANGUMI_APP_SECRET

# 启动
uvicorn main:app --reload --port 8000
curl http://localhost:8000/health
```

---

## 技术栈

Python 3.11+ · FastAPI · Pydantic v2 · SQLModel · LangGraph · PostgreSQL 16 + pgvector · httpx · tiktoken · 智谱 embedding-3

---

## 文档

详细信息见 [项目架构与开发手册](docs/ARCHITECTURE.md)。

---

## License

MIT © [Ujikintoki](https://github.com/Ujikintoki)
