# 03 — tool_node: 真实工具执行

## 职责

执行 LLM 请求的工具调用，返回结果注入消息历史。

## 当前占位

```python
def tool_node(state: AgentState) -> dict:
    return {"messages": ["[Tool] Tool execution successful."]}
```

## 目标实现

使用 LangGraph 内置 `ToolNode`，一行代码替换：

```python
from langgraph.prebuilt import ToolNode
from tools.bgm_tools import get_agent_tools

tools = get_agent_tools()
tool_node = ToolNode(tools)
```

### ToolNode 自动做的事

1. 读取 `state["messages"]` 中最后一条 `AIMessage`
2. 提取 `AIMessage.tool_calls`
3. 并发执行所有工具（`.ainvoke()`）
4. 返回 `{"messages": [ToolMessage(content=..., tool_call_id=...), ...]}`

### 工具执行流程

```
AIMessage(tool_calls=[call_1(search), call_2(detail)])
    │
    ▼
ToolNode
    │
    ├─ call_1 → search_bangumi_subject.ainvoke({"keyword": "..."}) → "JSON结果"
    ├─ call_2 → get_bangumi_subject_detail.ainvoke({"subject_id": 8}) → "详情JSON"
    │
    ▼
[ToolMessage(content="JSON结果", tool_call_id="call_1"),
 ToolMessage(content="详情JSON", tool_call_id="call_2")]
```

### 完整示例

```python
from langgraph.prebuilt import ToolNode
from langgraph.graph import StateGraph, END, START
from tools.bgm_tools import get_agent_tools

tools = get_agent_tools()
tool_node = ToolNode(tools)

# graph.py 中只需替换注册
graph.add_node("tool_node", tool_node)
```

### 如果不用 ToolNode（手动实现）

```python
async def tool_node(state: AgentState) -> dict:
    tools = get_agent_tools()
    tools_by_name = {t.name: t for t in tools}
    last_message = state["messages"][-1]
    tool_messages = []

    for tc in last_message.tool_calls:
        tool = tools_by_name.get(tc["name"])
        if tool:
            try:
                result = await tool.ainvoke(tc["args"])
                tool_messages.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))
            except Exception as e:
                tool_messages.append(ToolMessage(content=f"工具执行失败: {e}", tool_call_id=tc["id"]))
        else:
            tool_messages.append(ToolMessage(content=f"未知工具: {tc['name']}", tool_call_id=tc["id"]))

    return {"messages": tool_messages}
```

> **推荐**：直接用 `ToolNode`。它是 LangGraph 的官方组件，处理了并发、错误、重试等细节。

## 工具调用 → 工具返回 → LLM 再推理

```
第1轮: reasoning → AIMessage(tool_calls=[get_episode_comments])
         → tool_node → ToolMessage("吐槽箱: ...")
         → critic → REVISE

第2轮: reasoning → LLM 看到 ToolMessage 的返回内容
         → AIMessage(content="最新一集评价不错，观众普遍认为...")
         → critic → PASS → END
```

这就是 ReAct 循环的 `Action → Observation → Reasoning` 闭环。
