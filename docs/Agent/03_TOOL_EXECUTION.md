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

## 并行调用边界

LangGraph 的 `ToolNode` 默认**并发执行**所有 `tool_calls`。这是一个性能优势——但同时搜索 + 查详情可以减少延迟。但存在一个需要关注的风险：

### 问题：有依赖关系的工具被并发调用

```python
# ❌ LLM 可能错误地并行调用这两个工具
AIMessage(tool_calls=[
    {"name": "search_bangumi_subject", "args": {"keyword": "进击的巨人"}, "id": "call_1"},
    {"name": "get_bangumi_subject_detail", "args": {"subject_id": ???}, "id": "call_2"},
    #                                              ↑ subject_id 还没拿到！
])
```

`call_2` 需要 `call_1` 的返回结果，但 ToolNode 并发执行——`call_2` 会在 `call_1` 返回之前就发起。

### 解决方案

**Phase 3 策略——Prompt 约束（推荐）**：

在系统提示词中明确约束：

```
工具调用约束：
- 如果某个工具的参数需要另一个工具的返回结果，**不要并行调用它们**
- 典型错误：同时调用 search 和 get_detail——必须先 search 拿到 subject_id，再 get_detail
- 可以并行调用的场景：同时搜索多个条目、同时查两个不相关的信息
```

**Phase 4+ 可选方案——串行依赖声明**：

在工具 schema 中声明依赖关系，ToolNode 根据依赖自动串行化：

```python
# 未来可能的声明方式（Phase 4+，暂不实现）
class GetSubjectDetailInput(BaseModel):
    subject_id: int = Field(..., description="条目 ID")
    # 元数据声明：此工具依赖 search_bangumi_subject 的输出
    _depends_on: list[str] = ["search_bangumi_subject"]
```

## 工具调用 → 工具返回 → LLM 再推理

这是 ReAct 循环的 `Action → Observation → Reasoning` 闭环：

```
第1轮: reasoning → AIMessage(tool_calls=[get_episode_comments])
         → tool_node → ToolMessage("吐槽箱: ...")
         → critic → REVISE: "评论引用了但没有给出评分，建议补充调用 get_subject_detail"

第2轮: reasoning → 收到 critic_feedback → LLM 看到 ToolMessage + 改进建议
         → AIMessage(tool_calls=[get_bangumi_subject_detail])
         → tool_node → ToolMessage("评分: 8.5, ...")
         → critic → PASS → END
```

## 可选优化：工具结果摘要（Phase 4+）

> **标注为 Phase 4+ 优化项，Phase 3 不实现。**

当工具返回大量数据时（如 `get_episode_comments` 返回 500 条评论），在 ToolNode 和 reasoning_node 之间插入一个轻量摘要步骤：

```
ToolNode 返回 500 条评论 JSON
    │
    ▼
结果压缩器（小模型 / 规则截断）
    │  提取：情感分布（正面 60%、负面 15%）、高频关键词、代表性评论 top 5
    ▼
压缩后的 ToolMessage（~500 tokens 替代原始 3000 tokens）
```

**Phase 3 不做此优化的理由**：现有工具已有 `limit` 参数控制返回量（默认 10-30 条），大部分场景数据量可控。如果实际使用中遇到 token 爆量，再插入结果摘要步骤——接口不变，插拔式添加。

## 手动实现（兜底方案）

如果不用 `ToolNode`，手动实现作为理解参考：

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

## ⚠️ tool_node 的 State 安全约束

`tool_node`（无论内置 ToolNode 还是手动实现）**绝对不能触碰 `last_tool_calls` 字段**。

| 行为 | 后果 |
|---|---|
| 返回 `{"last_tool_calls": []}` | 清空路由信号 → `route_after_reasoning` 认为无工具调用 → 跳过后续工具 |
| 返回 `{"last_tool_calls": [...]}` | 覆盖 LLM 的决策 → REVISE 后错误循环 |
| 返回 `{"messages": [...]}` 且不含 `last_tool_calls` | ✅ 正确——LangGraph 保持该字段不变 |

**关键规则**：`last_tool_calls` 的**唯一写入者**是 `reasoning_node`。`tool_node` 和 `critic_node` 只返回自己负责的字段（`messages`、`critic_status`、`critic_feedback`），不返回 `last_tool_calls`。

> LangGraph 内置的 `ToolNode` 只返回 `{"messages": [ToolMessage, ...]}`，天然满足此约束。手动实现时需特别注意。
