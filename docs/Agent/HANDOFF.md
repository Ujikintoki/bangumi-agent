# Phase 3 完成状态转交 — 2026-06-06

> **Phase 3 全部 9 步已完成。** Agent 核心就位，LLM + 工具 + Critic + 记忆 + 端点全线贯通。

---

## 1. 完成清单

| Step | 内容 | 状态 |
|---|---|---|
| 1 | AgentState 升级（BaseMessage + 9 字段） | ✅ |
| 2 | LLM 接入 + 意图分类器 + Prompts | ✅ |
| 3 | tool_node 接入 LangGraph ToolNode | ✅ |
| 4 | critic_node 定向反馈 + 逃逸舱（双模式） | ✅ |
| 5 | 短期记忆（tiktoken 滑动窗口）+ chitchat 快速通道 | ✅ |
| 6 | POST /chat + /chat/stream 端点 | ✅ |
| 7 | 测试完善（375 tests，含真实 LLM/API/DB） | ✅ |
| 8 | 端到端验证（6 种意图全链路） | ✅ |
| 9 | 文档 & 清理 | ✅ |

---

## 2. 当前文件结构

```
agent/
├── state.py          # AgentState TypedDict（9 字段）
├── graph.py          # StateGraph + ToolNode + 快速通道路由
├── nodes.py          # reasoning_node, critic_node（双模式）
├── memory.py         # tiktoken 滑动窗口截断
├── llm.py            # create_llm() 多 Provider 工厂
├── classifier.py     # classify_intent() 两阶段分类
├── prompts.py        # BASE_SYSTEM_PROMPT + 5 intent 变体 + CRITIC_SYSTEM_PROMPT
core/
├── config.py         # Settings（LLM + Critic + Azure/OpenAI/DeepSeek 兼容）
main.py               # FastAPI: /health, POST /chat, POST /chat/stream
test/
├── conftest.py                # 共享 fixtures + mock 工具
├── test_state.py       (16)   # State + 路由
├── test_classifier.py  (34)   # 意图分类
├── test_llm.py          (5)   # LLM 工厂
├── test_prompts.py      (8)   # 提示词
├── test_reasoning.py   (10)   # reasoning_node
├── test_tool_node.py    (5)   # ToolNode
├── test_critic.py      (13)   # Critic 双模式
├── test_graph.py       (12)   # 图谱集成 + 跨模块耦合
├── test_endpoint.py    (12)   # /chat 端点
├── test_memory.py      (21)   # 滑动窗口
├── test_integration.py (19)   # 真实 LLM + API + DB
├── test_tools.py       (18)   # 工具层
├── test_client.py      (21)   # BangumiClient
├── test_schemas.py     (42)   # Schema 验证
└── test_sanitizers.py  (71)   # 数据清洗
```

---

## 3. E2E 验证结果

| 意图 | 查询 | 迭代 | 工具 | 耗时 | 状态 |
|---|---|---|---|---|---|
| chitchat | "你好" | 1 | 0 | 2.9s | ✅ 完美 |
| factual | "什么是三集定律" | 1 | 0 | 4.5s | ✅ 完美 |
| factual | "命运石之门的主角是谁" | 1 | 0 | 1.7s | ✅ 完美 |
| lookup | "进击的巨人评分" | 1* | 1 | 1.9s | ⚠️ 已知问题 |
| discovery | "推荐类似命运石之门" | 5 | 4 | 12.9s | ⚠️ 已知问题 |
| realtime | "今天放什么番" | 3 | 2 | 5.6s | ⚠️ 已知问题 |

> ⚠️ 工具路径查询（lookup/discovery/realtime）的 LLM 容易陷入 tool-calling 循环。根因是 `deepseek-v4-flash` 在收到工具结果后继续调工具而非合成回复。调优方向见 §4。

---

## 4. 已知待优化项

| 优先级 | 问题 | 方向 |
|---|---|---|
| 高 | tool-calling 循环（LLM 不出 tool mode） | CRITIC_MODE=llm 获得更智能的 REVISE 判断；或换 deepseek-v4-pro/gpt-4o |
| 中 | critic 规则版过度严格 | 连续 2 轮 tool_call 后自动引导合成（在 prompt 中加 "请现在回复，不要继续调用工具"） |
| 低 | RAG 数据为空 | 按 `docs/Agent/HANDOFF.md` 的方案注入 ~80K 实体 |
| 低 | LLM 温度可调 | chitchat 可用高温度 (0.7+)，工具调用保持低温度 (0.1-0.3) |

---

## 5. 关键设计决策（不可违反）

| 规则 | 位置 |
|---|---|
| `last_tool_calls` 仅 reasoning_node 写入 | `agent/nodes.py` |
| 意图分类用有序 list 不用 dict（优先队列） | `agent/classifier.py` |
| Token 计数用 tiktoken cl100k_base（不用 len//4） | `agent/memory.py` |
| Critic 逃逸舱：API 无数据 → 强制 PASS | `agent/nodes.py:_critic_node_llm` + `agent/prompts.py:CRITIC_SYSTEM_PROMPT` |
| chitchat/factual 不绑定工具 | `agent/nodes.py:_NO_TOOL_INTENTS` |
| chitchat 快速通道直达 END（跳过 critic） | `agent/graph.py:_FAST_PATH_INTENTS` |

---

## 6. 常用命令

```bash
# 全部测试（mock，秒级）
python -m pytest test/ --ignore=test/test_rag.py -k "not Real" -q

# 全部测试（含真实 LLM/API/DB，约 80s）
python -m pytest test/ --ignore=test/test_rag.py -q

# 跳过真实服务
REAL_LLM=0 REAL_API=0 python -m pytest test/ --ignore=test/test_rag.py -q

# 启动服务
uvicorn main:app --reload --port 8000

# 测试端点
curl -X POST http://localhost:8000/chat -H "Content-Type: application/json" -d '{"message":"你好"}'
curl -X POST http://localhost:8000/chat/stream -H "Content-Type: application/json" -d '{"message":"你好"}'

# Docker（RAG 需要）
docker start bangumi-pg
```

---

## 7. Graph 拓扑（最终状态）

```
START → reasoning_node ─┬─ tool_calls非空 → ToolNode → critic_node ─┬─ PASS/熔断 → END
                         │                                            │
                         ├─ chitchat 快速通道 → END                   └─ REVISE → reasoning_node
                         │
                         └─ 其他无tool → critic_node
```

---

*Phase 3 完成。下一步：critic 调优 → RAG 数据注入 → 部署。*
