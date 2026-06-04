# 07 — /chat 端点与流式输出

## FastAPI 端点

```python
# main.py 新增

from pydantic import BaseModel, Field
from langchain_core.messages import HumanMessage, SystemMessage

class ChatRequest(BaseModel):
    message: str = Field(..., description="用户消息")
    session_id: str = Field(default="default", description="会话 ID")

class ChatResponse(BaseModel):
    reply: str
    iterations: int
    tools_used: list[str]

@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """Agent 对话端点。"""
    from agent.graph import agent_app
    from agent.state import AgentState

    initial_state: AgentState = {
        "messages": [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=request.message),
        ],
        "iterations": 0,
        "critic_status": "PENDING",
        "last_tool_calls": [],
        "error_flag": False,
    }

    # 同步调用（LangGraph 内部处理 async）
    result = agent_app.invoke(initial_state)

    # 提取最终回复
    messages = result["messages"]
    final_ai = None
    for m in reversed(messages):
        from langchain_core.messages import AIMessage
        if isinstance(m, AIMessage) and m.content:
            final_ai = m
            break

    # 统计工具使用
    from langchain_core.messages import ToolMessage
    tools_used = list(set(
        m.name for m in messages if hasattr(m, 'name') and m.name
    ))

    return ChatResponse(
        reply=final_ai.content if final_ai else "抱歉，无法处理您的请求。",
        iterations=result.get("iterations", 0),
        tools_used=tools_used,
    )
```

## 流式输出（可选）

```python
@app.post("/chat/stream")
async def chat_stream(request: ChatRequest):
    from langchain_core.messages import HumanMessage, SystemMessage
    from agent.graph import agent_app

    initial_state = { ... }

    async def generate():
        for event in agent_app.stream(initial_state):
            # event 格式: {"node_name": {"messages": [...]}}
            for node_name, node_output in event.items():
                if "messages" in node_output:
                    for msg in node_output["messages"]:
                        if hasattr(msg, "content") and msg.content:
                            yield f"data: {json.dumps({'node': node_name, 'content': msg.content})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
```

## 错误处理

```python
try:
    result = agent_app.invoke(initial_state)
except Exception as e:
    logger.exception("Agent 执行异常")
    return ChatResponse(
        reply=f"系统异常：{str(e)}",
        iterations=0,
        tools_used=[],
    )
```

## 启动命令

```bash
uvicorn main:app --reload --port 8000

# 测试
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "帮我找类似命运石之门的烧脑番"}'
```
