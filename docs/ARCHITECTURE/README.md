# 数据层架构总览

> BGM Agent v0.1.1 | 2026-07-30 | 审计基准文档

---

## 一、数据层在四层架构中的位置

```
┌─────────────────────────────────────────────┐
│  编排层 (orchestrate/)                       │  ← 怎么思考
│  reasoning_node → tool_node → render_node   │
├─────────────────────────────────────────────┤
│  人格层 (persona/)                           │  ← 怎么说话
├─────────────────────────────────────────────┤
│  记忆层 (memory/)                            │  ← 能记住什么
├─────────────────────────────────────────────┤
│  数据层 (clients/ + tools/ + rag/ + db/)     │  ← 能查什么 ★ 本文档
└─────────────────────────────────────────────┘
```

数据层是四层中最稳定的层，自 dict 结构化重构（A/B/C/D 字段方法论）后基本未动。对上层提供 16 个工具函数和 1 个本地语义搜索引擎。

---

## 二、五子系统全景

```
                      ┌──────────────────────┐
                      │    编排层 (调用方)     │
                      └──────┬───────────────┘
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
     ┌────────────┐  ┌────────────┐  ┌──────────────┐
     │ 16 个工具   │  │ RAG 工具    │  │ 记忆系统      │
     │ (tools/)   │  │ (唯一本地)  │  │ (memory/)    │
     └─────┬──────┘  └─────┬──────┘  └──────┬───────┘
           │               │                │
           ▼               │                ▼
  ┌────────────────┐       │       ┌────────────────┐
  │ BangumiClient  │       │       │ ZhipuAiClient  │
  │ (clients/)     │       │       │ (clients/)     │
  └───────┬────────┘       │       └───────┬────────┘
          │                │               │
          ▼                ▼               ▼
  ┌─────────────────────────────────────────────────┐
  │              PostgreSQL + pgvector               │
  │  rag_entities | bangumi_chunks | session_       │
  │  memories | user_profiles | public_memories     │
  └─────────────────────────────────────────────────┘
          │
          ▼
  ┌────────────────┐
  │  Bangumi API   │
  │  next.bgm.tv   │
  └────────────────┘
```

| 子系统 | 文件 | 行数 | 状态 | 职责 |
|--------|------|------|------|------|
| **schemas/** | `tools_input.py` | 365 | ✅ 稳 | 16 个工具输入契约 |
| **clients/** | `base.py` + `client.py` + `sanitizers.py` + `zhipu_client.py` | ~3013 | ✅ 稳 | HTTP 通信 + 数据清洗 + embedding |
| **tools/** | `bgm_tools.py` | 1118 | ✅ 稳 | 16 个 LangChain @tool 函数 |
| **rag/** | `retriever.py` + `ingestion.py` + `text_processor.py` | ~1557 | 🟡 v0/v1 共存 | 本地语义搜索 |
| **database/** | `engine.py` + `rag_tables.py` + `memory_tables.py` | ~785 | 🟡 无 Alembic | 表结构 + 索引 + 迁移 |

**合计：~5079 行源码**

---

## 三、子系统间依赖

```
schemas/tools_input.py      ← 被 tools/, clients/ 导入（Pydantic 输入模型）
clients/base.py             ← 被 clients/client.py 继承
clients/client.py           ← 被 tools/bgm_tools.py 导入
clients/sanitizers.py       ← 被 clients/client.py 导入（纯函数，可独立测试）
clients/zhipu_client.py     ← 被 rag/, agent/memory/ 导入（embedding 基础设施）
database/rag_tables.py      ← 被 rag/, database/engine.py 导入（ORM 模型）
database/memory_tables.py   ← 被 database/engine.py, agent/memory/ 导入
database/engine.py          ← 被 main.py (lifespan), rag/, agent/memory/ 导入
```

**依赖方向**：schemas → clients → tools → 编排层。下层完全不感知上层。

---

## 四、关键设计决策

### 4.1 错误处理：字典 `_error` 模式（不抛异常）

```
BaseClient._request() → {"_error": "请求超时"}  (永不抛异常)
BangumiClient.method() → 检查 if "_error" in raw → 提前返回
tool_function() → 包装为 {"_error": f"搜索失败。{raw['_error']}"}
```

三层嵌套错误处理，所有异常在到达 LLM 前被转换为 dict。缺点是丢失异常类型信息，优点是对 LLM 极简——只需要检查 `_error` 键。

### 4.2 数据清洗：纯函数 + 白名单优先

```python
# sanitizers.py — 所有清洗函数是纯函数
def sanitize_subject_detail(raw: dict) -> dict:
    return {
        "id": raw.get("id", 0),
        "name": raw.get("name", ""),
        "name_cn": raw.get("name_cn", ""),
        # 只取白名单字段，缺了给默认值
    }
```

"要什么"优于"丢什么"。每个字段显式决策保留/丢弃/转换。

### 4.3 RAG 单表多态

放弃多表 JOIN，所有实体（Subject/Character/Person）存入同一张 `rag_entities` 表，通过 `entity_type` 列区分，主键前缀化防碰撞（`"subject_10"` / `"character_5"` / `"person_3"`）。JSONB `meta_info` 列承载各实体特有字段，入库前用 Pydantic 模型强类型校验。

### 4.4 工具动态注册

```python
def get_agent_tools() -> list:
    tools = [13 个无条件工具]
    if BANGUMI_ACCESS_TOKEN:
        tools += [3 个 token 门控工具]
    return tools
```

13 个无条件工具 + 3 个需 Access Token。编排层调用 `get_agent_tools()` 获取当前可用工具集。

### 4.5 手动 DDL 迁移

项目不使用 Alembic。所有表结构变更通过 `database/engine.py:init_db()` 中的内联 DDL 执行。变更必须是可幂等的（`IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS`）。

---

## 五、数据流：一次典型的用户查询

```
用户: "高达Seed 评分怎么样？"
  │
  ▼
reasoning_node 决定调用 search_bangumi_subject(keyword="高达Seed")
  │
  ▼
tools/bgm_tools.py: search_bangumi_subject()
  │  ① 构造 SearchBangumiInput(keyword="高达Seed", entity_type="subject", limit=5)
  │  ② client.search(input)
  │
  ▼
clients/client.py: BangumiClient.search()
  │  ③ POST https://next.bgm.tv/p1/search/subjects
  │     └─ BaseClient._request() 处理重试/认证/超时
  │  ④ 收到 HTTP 200 JSON → sanitize_search_subjects(raw)
  │
  ▼
clients/sanitizers.py: sanitize_search_subjects()
  │  ⑤ 白名单提取: id, name, name_cn, type→中文, score, rank
  │  ⑥ 返回 {"results": [{...}, ...], "total": N}
  │
  ▼
tools/bgm_tools.py: 检查 _error → 返回 dict 给 LLM
  │
  ▼
reasoning_node 拿到搜索结果 → 决定调用 get_bangumi_subject_detail(subject_id=...)
  │  （重复 ③-⑥，不同端点和清洗器）
  │
  ▼
编排层汇总数据 → render_node 风格渲染 → 用户看到回复
```

---

## 六、文档导航

| 文档 | 内容 |
|------|------|
| [README.md](README.md) | 本文档 — 数据层架构总览 |
| [clients.md](clients.md) | 客户端层：BaseClient、BangumiClient、Sanitizers、Zhipu 客户端 |
| [tools.md](tools.md) | 工具层：16 个工具全景、动态注册、数据流 |
| [rag.md](rag.md) | RAG 子系统：检索流水线、摄入流水线、v0/v1 共存状态 |
| [database.md](database.md) | 数据库层：表结构、索引、连接池、迁移策略 |
| [assessment.md](assessment.md) | 开发者预览就绪评估：风险矩阵、修复优先级 |
