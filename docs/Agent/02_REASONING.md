# 02 — reasoning_node: LLM Function-Calling

## 职责

接收用户消息 + 对话历史 → 调用 LLM → LLM 决定回复 OR 调用工具

## 核心逻辑

```python
from langchain_core.messages import AIMessage
from core.config import get_settings

def reasoning_node(state: AgentState) -> dict:
    # 1. 兜底模式
    if state.get("error_flag"):
        return {"messages": [AIMessage(content="抱歉，系统当前繁忙，请稍后再试。")]}

    # 2. 初始化 LLM
    settings = get_settings()
    llm = ChatOpenAI(
        model=settings.LLM_MODEL,          # "gpt-4o" / "deepseek-chat" / "qwen-plus"
        api_key=settings.LLM_API_KEY,
        base_url=settings.LLM_BASE_URL,
        temperature=0.3,                   # 低温度保证工具调用稳定性
    )

    # 3. 绑定工具
    tools = get_agent_tools()
    llm_with_tools = llm.bind_tools(tools)

    # 4. 调用 LLM
    response: AIMessage = llm_with_tools.invoke(state["messages"])

    # 5. 判断是否有工具调用
    has_tool_calls = bool(response.tool_calls)

    return {
        "messages": [response],
        "iterations": state.get("iterations", 0) + 1,
        "last_tool_calls": response.tool_calls,  # [] 或 [ToolCall, ...]
    }
```

## LLM 返回的 AIMessage 示例

**无工具调用（LLM 直接回答）：**
```python
AIMessage(
    content="顶上战争是白胡子海贼团与海军本部之间的大战...",
    tool_calls=[]
)
→ route_after_reasoning → "critic_node"（跳过工具）
```

**有工具调用：**
```python
AIMessage(
    content="",
    tool_calls=[
        {"name": "get_episode_comments", "args": {"episode_id": 1088, "comments_limit": 10}, "id": "call_1"}
    ]
)
→ route_after_reasoning → "tool_node"
```

## 路由逻辑更新

`route_after_reasoning` 改为检查 `last_tool_calls`：

```python
def route_after_reasoning(state: AgentState) -> Literal["tool_node", "critic_node"]:
    if state.get("last_tool_calls"):
        return "tool_node"
    return "critic_node"
```

## LLM 配置

需要在 `core/config.py` 新增：

```python
LLM_API_KEY: str = ""           # OpenAI / DeepSeek / Qwen API Key
LLM_MODEL: str = "gpt-4o"      # 或 "deepseek-chat" / "qwen-plus"
LLM_BASE_URL: str = "https://api.openai.com/v1"  # 可切换为 DeepSeek/Qwen 地址
LLM_TEMPERATURE: float = 0.3
```

## 关键注意事项

1. **`bind_tools` 的顺序**：工具定义中 `description` + `args_schema` 的 `Field(description=...)` 直接影响 LLM 的工具选择准确率。已在 Phase 2 完成。
2. **并行调用**：LLM 可能一次返回多个 `tool_calls`（如同时搜索条目 + 查角色）。LangGraph ToolNode 已支持并发执行。
3. **temperature = 0.3**：工具调用场景需要低温度，减少幻觉和错误参数。
4. **重入安全**：`reasoning_node` 可能被多次调用（critic REVISE 后）。LLM 需要看到完整的消息历史来理解上下文。
