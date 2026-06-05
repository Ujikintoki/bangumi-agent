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

    # 工具调用
    last_tool_calls: list[dict] # ← 替换 needs_tool，来自 AIMessage.tool_calls

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
| `last_tool_calls` | `list[dict]` | reasoning_node 设置 | 条件边路由：非空 → tool_node，空 → critic_node |
| `query_intent` | `str` | reasoning_node 分类器 | 选择 prompt 变体，影响工具调用策略 |
| `session_id` | `str` | `/chat` 端点传入 | Layer 2 会话记忆预留 |
| `user_id` | `str` | `/chat` 端点传入 | Layer 3 用户画像预留 |
| `error_flag` | `bool` | critic_node 设置 | 熔断后进入兜底模式 |

## 对比：`needs_tool` → `last_tool_calls`

```python
# 旧：硬编码推断
needs_tool: bool  # reasoning_node 用关键词匹配设置

# 新：LLM 原生输出
last_tool_calls: list[dict]  # 直接来自 AIMessage.tool_calls，无需推断
```

路由逻辑改为检查 `last_tool_calls`：

```python
def route_after_reasoning(state: AgentState) -> Literal["tool_node", "critic_node"]:
    if state.get("last_tool_calls"):
        return "tool_node"
    return "critic_node"
```

## `query_intent` 与 `last_tool_calls` 的分工

```
query_intent:   元数据 → 影响 prompt 选择（"应该优先用哪些工具"）
last_tool_calls: 路由信号 → 决定图走向（"是否需要执行工具"）
```

两者不冲突。`query_intent` 是建议性的，LLM 最终通过 `last_tool_calls` 自主决定。

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
    "last_tool_calls": [],
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
  │ 返回: {messages: [AIMessage], last_tool_calls: [...], query_intent: "lookup", iterations: 1}
  ▼
tool_node (如果 last_tool_calls 非空)
  │ ToolNode 并发执行工具
  │ 返回: {messages: [ToolMessage, ToolMessage, ...]}
  ▼
critic_node
  │ 评估最后一条 AIMessage
  │ 返回: {critic_status: "REVISE", critic_feedback: "缺少评分数据，建议调用 get_subject_detail"}
  │ 或:   {critic_status: "PASS", critic_feedback: "回复完整，包含具体评分和描述"}
  ▼
reasoning_node（如果 REVISE）
  │ 收到 critic_feedback → 注入 prompt → 定向修正
  │ ...
  ▼
END（如果 PASS 或 iterations ≥ 3）
```

## `last_tool_calls` 生命周期约束

`last_tool_calls` 是条件边 `route_after_reasoning` 的路由依据，必须严格控制其读写权限：

| 节点 | 对 `last_tool_calls` 的操作 | 规则 |
|---|---|---|
| `reasoning_node` | **写入**：`{"last_tool_calls": response.tool_calls}` | 唯一写入者。每次调用时覆写为 LLM 本轮返回的 tool_calls |
| `tool_node` (ToolNode) | **不触碰** | LangGraph 内置 ToolNode 只返回 `{"messages": [...]}`，不操作其他字段 |
| `critic_node` | **不触碰** | 只设置 `critic_status` 和 `critic_feedback` |

**禁止行为**：
- ❌ `tool_node` 返回 `last_tool_calls: []` — 会清空路由信号，导致跳过后续工具调用
- ❌ `critic_node` 返回 `last_tool_calls: [...]` — REVISE 后会错误地再次进入 tool_node
- ❌ 手动实现 tool_node 时，在返回的 dict 中包含 `last_tool_calls` 字段

**LangGraph 行为说明**：当节点返回的 dict 中包含 `AgentState` 中定义的 key 时，会按该 key 的 reducer 语义更新。对于 `last_tool_calls: list[dict]`（无特殊 reducer），默认行为是**覆盖**。因此：

- `reasoning_node` 每轮写入新的 `last_tool_calls` → 覆盖上一轮的值 → ✅ 正确
- `tool_node` / `critic_node` 不返回该 key → 值保持不变 → ✅ 正确
- 任何节点返回 `last_tool_calls: []` → 清空 → ❌ 路线由错误

> **实现检查清单**：在写 `tool_node` 和 `critic_node` 代码时，确认返回的 dict 中没有 `last_tool_calls` key。
