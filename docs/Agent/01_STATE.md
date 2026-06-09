# 01 — AgentState: 消息类型升级 + 新字段

## 当前问题

```python
# agent/state.py — 当前
messages: Annotated[list, operator.add]  # list[str]
needs_tool: bool
```

`messages` 是 `list[str]`。LLM 需要的是 LangChain `BaseMessage` 子类。`needs_tool` 由硬编码关键词匹配决定，LLM 时代应由 `AIMessage.tool_calls` 驱动。

## 目标 AgentState

```python
from typing import Annotated, TypedDict
from langchain_core.messages import BaseMessage

class AgentState(TypedDict):
    # 消息历史（operator.add = 追加语义）
    messages: Annotated[list[BaseMessage], operator.add]

    # 迭代计数
    iterations: int

    # Critic 状态
    critic_status: str          # "PENDING" | "PASS" | "REVISE"
    critic_feedback: str        # ← 新增：Critic 的具体改进建议

    # 意图分类
    query_intent: str           # ← 新增："chitchat"|"factual"|"lookup"|"discovery"|"realtime"|"unknown"

    # 会话 & 用户标识（记忆架构预留）
    session_id: str             # ← 新增：会话 ID（Layer 2 预留）
    user_id: str                # ← 新增：用户 ID（Layer 3 预留）

    # 错误标记
    error_flag: bool
```

## 字段详解

| 字段 | 类型 | 来源 | 用途 |
|---|---|---|---|
| `messages` | `list[BaseMessage]` | 端点初始化 + 各节点追加 | 完整对话历史 |
| `iterations` | `int` | reasoning_node 递增 | 熔断判断（≥3 强制结束） |
| `critic_status` | `str` | critic_node 设置 | 条件边路由（PASS→END, REVISE→重试） |
| `critic_feedback` | `str` | critic_node 设置 | REVISE 时的具体改进建议，注入下一轮 reasoning |
| `query_intent` | `str` | reasoning_node 分类器 | 选择 prompt 变体 + chitchat 快速通道路由 |
| `session_id` | `str` | `/chat` 端点传入 | Layer 2 会话记忆预留 |
| `user_id` | `str` | `/chat` 端点传入 | Layer 3 用户画像预留 |
| `error_flag` | `bool` | critic_node 设置 | 熔断后进入兜底模式 |

## 路由：原生消息路由（不再使用 `last_tool_calls`）

```python
def route_after_reasoning(state: AgentState) -> Literal["tool_node", "critic_node", "__end__"]:
    messages = state.get("messages", [])
    last_msg = messages[-1] if messages else None
    has_tool_calls = (
        isinstance(last_msg, AIMessage)
        and hasattr(last_msg, "tool_calls")
        and last_msg.tool_calls
    )
    if has_tool_calls:
        return "tool_node"
    if state.get("query_intent") == "chitchat":
        return END  # 快速通道
    return "critic_node"
```

**关键**：直接读取 `state["messages"][-1]` 的 `tool_calls` 属性判定路由，不依赖任何冗余状态字段。路由逻辑完全由消息本身驱动。

## LangChain 消息类型速查

| 类型 | content | 额外字段 | 由谁创建 |
|---|---|---|---|
| `SystemMessage` | str | — | 端点初始化 / intent prompt 注入 |
| `HumanMessage` | str | — | 用户 / critic_feedback 注入 |
| `AIMessage` | str | `tool_calls: list[ToolCall]` | LLM |
| `ToolMessage` | str | `tool_call_id: str`, `name: str` | ToolNode |

> **关键**：`Annotated[list, operator.add]` 保证每个节点返回的 `{"messages": [...]}` 会**追加**到已有列表，而非覆盖。

## 初始化状态（`/chat` 端点中）

```python
initial_state: AgentState = {
    "messages": [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=user_input),
    ],
    "iterations": 0,
    "critic_status": "PENDING",
    "critic_feedback": "",
    "query_intent": "unknown",
    "session_id": session_id,
    "user_id": user_id,
    "error_flag": False,
}
```

## 状态在各节点间的流转

```
START
  │ state = {messages: [System, Human], query_intent: "unknown", ...}
  ▼
reasoning_node
  │ 分类器设置 query_intent
  │ LLM 调用 → AIMessage(tool_calls=[...])
  │ 返回: {messages: [AIMessage], query_intent: "lookup", iterations: 1}
  ▼
tool_node（如果 AIMessage.tool_calls 非空）
  │ ToolNode 并发执行工具
  │ 返回: {messages: [ToolMessage, ...]}
  ▼
reasoning_node（固定边：消化工具结果，消化态解绑工具）
  │ 收到 ToolMessage → 强制输出文本回复（不绑工具）
  │ 返回: {messages: [AIMessage 文本回复], ...}
  ▼
critic_node（无工具调用时）
  │ 评估最后一条 AIMessage
  │ 返回: {critic_status: "REVISE", critic_feedback: "..."}
  │ 或:   {critic_status: "PASS", critic_feedback: "..."}
  ▼
reasoning_node（如果 REVISE，重新绑定工具）
  │ 收到 critic_feedback → 注入 prompt → 定向修正
  │ ...
  ▼
END（如果 PASS 或 iterations ≥ 10）
```

## `query_intent` 与路由的关系

`query_intent` 承担两种角色：
1. **Prompt 选择**：决定 `build_system_prompt()` 注入哪个 intent 变体
2. **快速通道路由**：`chitchat` 直达 END，跳过 tool/critic

工具调用的路由决策则完全由消息本身驱动——检查 `messages[-1]` 的 `tool_calls` 属性。`query_intent` 是建议性的（影响 LLM 行为），路由是声明性的（读取消息状态）。
