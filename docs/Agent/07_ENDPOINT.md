# 07 — /chat 端点与流式输出

## FastAPI 端点

### ChatRequest / ChatResponse

```python
# main.py 新增
from pydantic import BaseModel, Field

class ChatRequest(BaseModel):
    message: str = Field(..., description="用户消息")
    session_id: str = Field(default="default", description="会话 ID（Layer 2 预留）")
    user_id: str = Field(default="anonymous", description="用户 ID（Layer 3 预留）")

class ChatResponse(BaseModel):
    reply: str
    iterations: int
    tools_used: list[str]
    query_intent: str          # ← 新增：返回意图分类结果，便于调试
```

### POST /chat

```python
@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """Agent 对话端点。"""
    from langgraph.graph import StateGraph
    from agent.graph import agent_app
    from agent.state import AgentState
    from langchain_core.messages import HumanMessage, SystemMessage, AIMessage, ToolMessage
    from agent.prompts import BASE_SYSTEM_PROMPT  # 05_SYSTEM_PROMPT.md 中定义

    initial_state: AgentState = {
        "messages": [
            SystemMessage(content=BASE_SYSTEM_PROMPT),
            HumanMessage(content=request.message),
        ],
        "iterations": 0,
        "critic_status": "PENDING",
        "critic_feedback": "",
        "last_tool_calls": [],
        "query_intent": "unknown",
        "session_id": request.session_id,
        "user_id": request.user_id,
        "error_flag": False,
    }

    try:
        result = agent_app.invoke(initial_state)
    except Exception as e:
        logger.exception("Agent 执行异常")
        return ChatResponse(
            reply=f"系统异常：{str(e)}",
            iterations=0,
            tools_used=[],
            query_intent="unknown",
        )

    # 提取最终回复
    messages = result.get("messages", [])
    final_ai = None
    for m in reversed(messages):
        if isinstance(m, AIMessage) and m.content and not (hasattr(m, 'tool_calls') and m.tool_calls):
            final_ai = m
            break

    # 统计工具使用
    tools_used = []
    for m in messages:
        if isinstance(m, ToolMessage):
            if hasattr(m, 'name') and m.name:
                tools_used.append(m.name)

    return ChatResponse(
        reply=final_ai.content if final_ai else "抱歉，无法处理您的请求。",
        iterations=result.get("iterations", 0),
        tools_used=list(dict.fromkeys(tools_used)),  # 去重保序
        query_intent=result.get("query_intent", "unknown"),
    )
```

## 流式输出（节点级 SSE）

Phase 3 做节点级别的事件推送——用户看到每个阶段的变化：

```
[状态] 正在理解你的问题... (reasoning)
[状态] 正在查找数据...        (tool_node)
[状态] 正在组织回复...        (critic)
```

```python
from fastapi.responses import StreamingResponse
import json

@app.post("/chat/stream")
async def chat_stream(request: ChatRequest):
    from agent.graph import agent_app
    from langchain_core.messages import HumanMessage, SystemMessage

    initial_state = { ... }  # 同上

    async def generate():
        async for event in agent_app.astream(initial_state):
            # event 格式: {"node_name": {"messages": [...]}}
            for node_name, node_output in event.items():
                if node_name == "reasoning_node":
                    intent = node_output.get("query_intent", "unknown")
                    yield f"data: {json.dumps({'node': 'reasoning', 'intent': intent}, ensure_ascii=False)}\n\n"
                elif node_name == "tool_node":
                    tools = []
                    if "messages" in node_output:
                        for msg in node_output["messages"]:
                            if hasattr(msg, 'name'):
                                tools.append(msg.name)
                    yield f"data: {json.dumps({'node': 'tool', 'tools': tools}, ensure_ascii=False)}\n\n"
                elif node_name == "critic_node":
                    status = node_output.get("critic_status", "PENDING")
                    yield f"data: {json.dumps({'node': 'critic', 'status': status}, ensure_ascii=False)}\n\n"

        # 最终回复
        yield f"data: {json.dumps({'node': 'done'})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
```

## 错误处理

```python
try:
    result = agent_app.invoke(initial_state)
except ValueError as e:
    # 意图分类失败等参数错误
    logger.warning(f"参数错误: {e}")
    return ChatResponse(reply=f"输入格式有误：{str(e)}", iterations=0, tools_used=[], query_intent="unknown")
except Exception as e:
    # LLM 调用失败、网络错误等
    logger.exception("Agent 执行异常")
    return ChatResponse(reply="系统繁忙，请稍后重试。", iterations=0, tools_used=[], query_intent="unknown")
```

## 启动命令

```bash
uvicorn main:app --reload --port 8000

# 测试非流式
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "帮我找类似命运石之门的烧脑番", "session_id": "test-001", "user_id": "user-001"}'

# 测试流式
curl -X POST http://localhost:8000/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"message": "今天放什么番", "session_id": "test-001"}'

# 测试意图分类
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "你好"}'
# → {"reply": "...", "query_intent": "chitchat", "tools_used": [], ...}
```
