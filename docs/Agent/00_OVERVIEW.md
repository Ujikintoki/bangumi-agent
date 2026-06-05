# Phase 3: Agent 接入真实 LLM — 总览

> 目标：将硬编码占位 Agent 替换为真正的 LLM 驱动的 ReAct Agent

---

## 当前状态 vs 目标

| 组件 | 当前（占位） | 目标（真实） |
|---|---|---|
| `reasoning_node` | `_detect_tool_intent()` 关键词匹配 | LLM function-calling 决策 |
| `tool_node` | `"[Tool] Tool execution successful."` | 执行 `get_agent_tools()` 返回的工具 |
| `critic_node` | `iterations < 2 → REVISE` | LLM 评估输出是否充分 |
| `AgentState.messages` | `list[str]` | `list[BaseMessage]`（LangChain 消息类型） |
| `main.py` | 只有 `/health` | 新增 `POST /chat` |

## 开发顺序

按依赖关系排列，每步可独立验证：

```
1. AgentState 消息类型升级 (str → LangChain BaseMessage)
       │
2. reasoning_node 接入 LLM (function-calling 决策)
       │
3. tool_node 接入真实工具 (ToolNode + 12 tools)
       │
4. critic_node 接入 LLM (输出质量评估)
       │
5. 系统提示词与路由策略
       │
6. 短期记忆管理 (Token 预算 + 滑动窗口)
       │
7. POST /chat 端点 + 错误处理
```

## 文件索引

```
docs/phase3/
├── 00_OVERVIEW.md           ← 本文件
├── 01_STATE.md              ← AgentState 升级
├── 02_REASONING.md          ← reasoning_node: LLM function-calling
├── 03_TOOL_EXECUTION.md     ← tool_node: 真实工具执行
├── 04_CRITIC.md             ← critic_node: 输出评估
├── 05_SYSTEM_PROMPT.md      ← 路由策略 + 系统提示词
├── 06_MEMORY.md             ← 短期记忆管理
├── 07_ENDPOINT.md           ← /chat 端点 + 流式输出
└── 08_TESTING.md            ← 测试策略
```

## 关键架构决策（已确定）

| 决策 | 结论 |
|---|---|
| Agent 框架 | LangGraph StateGraph（已就位，拓扑不变） |
| LLM 接入方式 | OpenAI SDK 兼容（支持 OpenAI / DeepSeek / Qwen） |
| 工具调用 | LangGraph `ToolNode` + 本项目 `get_agent_tools()` |
| 自省策略 | LLM 评估（Token 成本约 200 input + 100 output，可接受） |
| 迭代上限 | 3 轮（当前配置，可调） |
| 消息类型 | LangChain `BaseMessage`（HumanMessage, AIMessage, ToolMessage） |
