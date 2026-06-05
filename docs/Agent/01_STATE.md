# 01 — AgentState: 消息类型升级

## 当前问题

```python
# agent/state.py — 当前
messages: Annotated[list, operator.add]  # list[str]
```

`messages` 是 `list[str]`。LLM 需要的是 LangChain `BaseMessage` 子类：

| 消息类型 | 用途 | 示例 |
|---|---|---|
| `HumanMessage` | 用户输入 | `HumanMessage(content="帮我找类似命运石之门的番")` |
| `AIMessage` | LLM 回复 | `AIMessage(content="...")` / 含 `tool_calls` |
| `ToolMessage` | 工具返回值 | `ToolMessage(content="...", tool_call_id="...")` |
| `SystemMessage` | 系统提示词 | `SystemMessage(content="你是 Bangumi 助手...")` |

## 改动

### `agent/state.py` → 新增

```python
from langchain_core.messages import BaseMessage

class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], operator.add]
    iterations: int
    critic_status: str        # "PENDING" | "PASS" | "REVISE"
    needs_tool: bool
    error_flag: bool
    tool_calls: list[dict]    # ← 新增：本轮 LLM 请求的工具调用
```

### `agent/state.py` → 删除

```python
needs_tool: bool   # ← 不再需要。LLM 的 AIMessage.tool_calls 列表非空 = 需要工具
```

改为：

```python
last_tool_calls: list[dict]  # 上一轮 LLM 请求的工具调用
# 条件边改为检查 len(state["last_tool_calls"]) > 0
```

### 初始化状态（`/chat` 端点中）

```python
initial_state = {
    "messages": [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=user_input)],
    "iterations": 0,
    "critic_status": "PENDING",
    "last_tool_calls": [],
    "error_flag": False,
}
```

## LangChain 消息类型速查

| 类型 | content | 额外字段 | 由谁创建 |
|---|---|---|---|
| `SystemMessage` | str | — | 系统（端点初始化） |
| `HumanMessage` | str | — | 用户 |
| `AIMessage` | str | `tool_calls: list[ToolCall]` | LLM |
| `ToolMessage` | str | `tool_call_id: str` | ToolNode |

> **关键**：`Annotated[list, operator.add]` 保证每个节点返回的 `{"messages": [...]}` 会**追加**到已有列表，而非覆盖。
