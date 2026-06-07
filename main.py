"""
FastAPI 应用启动入口

初始化 FastAPI 实例，配置 CORS 中间件与生命周期事件，
提供健康检查、Agent 对话和流式输出端点。
"""

import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from pydantic import BaseModel, Field

from agent.research.graph import agent_app
from agent.research.prompts import BASE_SYSTEM_PROMPT
from agent.research.state import AgentState
from core.config import get_settings

settings = get_settings()


def _setup_logging() -> None:
    """初始化 bgm-agent 命名空间下的所有 logger。

    默认输出到 stdout，日志级别由环境变量 BGM_LOG_LEVEL 控制（默认 INFO）。
    避免触碰 root logger 以防干扰 uvicorn/sqlalchemy 的独立配置。
    """
    level_name = __import__("os").environ.get("BGM_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    # 根 logger for bgm-agent 命名空间
    root = logging.getLogger("bgm-agent")
    root.setLevel(level)
    root.propagate = False  # 不向 root logger 重复传播

    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setLevel(level)
        fmt = logging.Formatter(
            "[%(asctime)s] %(levelname)-5s %(name)s | %(message)s",
            datefmt="%m-%d %H:%M:%S",
        )
        handler.setFormatter(fmt)
        root.addHandler(handler)

    # 抑制 sqlalchemy engine 的 SQL 日志（太吵），除非显式设了 DEBUG
    if level_name != "DEBUG":
        logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


_setup_logging()
logger = logging.getLogger("bgm-agent")


# ═══════════════════════════════════════════════════════════════════
# 请求/响应模型
# ═══════════════════════════════════════════════════════════════════


class ChatRequest(BaseModel):
    """对话请求。"""

    message: str = Field(..., description="用户消息", min_length=1)
    session_id: str = Field(default="default", description="会话 ID（Layer 2 预留）")
    user_id: str = Field(default="anonymous", description="用户 ID（Layer 3 预留）")


class ChatResponse(BaseModel):
    """对话响应。"""

    reply: str = Field(..., description="Agent 的最终回复")
    iterations: int = Field(..., description="ReAct 循环轮数")
    tools_used: list[str] = Field(default_factory=list, description="本轮调用的工具名称")
    query_intent: str = Field(default="unknown", description="查询意图分类结果")


# ═══════════════════════════════════════════════════════════════════
# FastAPI 生命周期
# ═══════════════════════════════════════════════════════════════════


@asynccontextmanager
async def lifespan(app: FastAPI):
    """管理应用的生命周期。"""
    logger.info("🚀 系统启动 — %s v%s", settings.PROJECT_NAME, settings.VERSION)
    print(f"[lifespan] {settings.PROJECT_NAME} v{settings.VERSION} 启动成功")
    yield
    logger.info("🛑 系统关闭 — %s v%s", settings.PROJECT_NAME, settings.VERSION)
    print(f"[lifespan] {settings.PROJECT_NAME} v{settings.VERSION} 已关闭")


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    lifespan=lifespan,
)

# ── CORS 中间件 ────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ═══════════════════════════════════════════════════════════════════
# 端点
# ═══════════════════════════════════════════════════════════════════


@app.get("/health")
async def health_check() -> dict:
    """基础健康检查。"""
    return {
        "status": "ok",
        "environment": settings.ENVIRONMENT,
        "version": settings.VERSION,
    }


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    """Agent 对话端点。

    接收用户消息，执行完整的 ReAct 循环（推理 → 工具 → 自省），
    返回最终回复和诊断信息。

    Args:
        request: 包含用户消息、会话 ID 和用户 ID 的请求体。

    Returns:
        ChatResponse: 包含回复、迭代次数、工具列表和意图分类的响应。
    """
    # ── 构建初始状态 ─────────────────────────────────────────
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
        logger.exception("/chat: Agent 执行异常")
        return ChatResponse(
            reply=f"系统异常：{e}",
            iterations=0,
            tools_used=[],
            query_intent="unknown",
        )

    # ── 提取结果 ─────────────────────────────────────────────
    messages = result.get("messages", [])

    # 提取最终 AI 回复（最后一条有实质内容的 AIMessage）
    final_reply = _extract_final_reply(messages)

    # 统计工具使用
    tools_used = _extract_tools_used(messages)

    return ChatResponse(
        reply=final_reply,
        iterations=result.get("iterations", 0),
        tools_used=tools_used,
        query_intent=result.get("query_intent", "unknown"),
    )


@app.post("/chat/stream")
async def chat_stream(request: ChatRequest):
    """Agent 对话流式端点（SSE）。

    按节点级别推送事件：reasoning → tool → critic → done。
    前端可据此展示阶段性进度。

    Args:
        request: 包含用户消息、会话 ID 和用户 ID 的请求体。

    Returns:
        StreamingResponse: SSE 事件流（text/event-stream）。
    """
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

    async def generate():
        try:
            async for event in agent_app.astream(initial_state):
                for node_name, node_output in event.items():
                    if node_name == "reasoning_node":
                        intent = node_output.get("query_intent", "unknown")
                        tool_calls = []
                        if node_output.get("last_tool_calls"):
                            tool_calls = [tc.get("name", "?") for tc in node_output["last_tool_calls"]]
                        yield f"data: {json.dumps({'node': 'reasoning', 'intent': intent, 'tool_calls': tool_calls}, ensure_ascii=False)}\n\n"

                    elif node_name == "tool_node":
                        tools = []
                        if "messages" in node_output:
                            for msg in node_output["messages"]:
                                if isinstance(msg, ToolMessage) and hasattr(msg, "name"):
                                    tools.append(msg.name)
                        yield f"data: {json.dumps({'node': 'tool', 'tools': list(dict.fromkeys(tools))}, ensure_ascii=False)}\n\n"

                    elif node_name == "critic_node":
                        status = node_output.get("critic_status", "PENDING")
                        feedback = node_output.get("critic_feedback", "")
                        yield f"data: {json.dumps({'node': 'critic', 'status': status, 'feedback': feedback[:200]}, ensure_ascii=False)}\n\n"

            yield "data: [DONE]\n\n"

        except Exception as e:
            logger.exception("/chat/stream: Agent 执行异常")
            yield f"data: {json.dumps({'node': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


# ═══════════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════════


def _extract_final_reply(messages: list) -> str:
    """从消息历史中提取最终 AI 回复。

    两级降级策略：
        1. 查找最后一条不含 tool_calls 且有内容的 AIMessage（理想情况）
        2. 退而求其次：任何有内容的 AIMessage（含仍带 tool_calls 的过渡语）
        3. 最终兜底

    Args:
        messages: 完整的消息历史列表。

    Returns:
        最终回复文本。未找到时返回兜底消息。
    """
    # 第一遍：不含 tool_calls 的干净回复
    for m in reversed(messages):
        if isinstance(m, AIMessage) and m.content:
            has_tc = hasattr(m, "tool_calls") and m.tool_calls
            if not has_tc:
                return m.content

    # 第二遍：退而求其次——有内容的 AIMessage（即使还挂着 tool_calls）
    # 说明 Critic 未触发新一轮合成，但至少有部分内容可以展示
    for m in reversed(messages):
        if isinstance(m, AIMessage) and m.content:
            return m.content

    return "抱歉，无法处理您的请求。"


def _extract_tools_used(messages: list) -> list[str]:
    """从消息历史中提取已调用的工具名称列表（去重保序）。

    Args:
        messages: 完整的消息历史列表。

    Returns:
        工具名称列表。
    """
    tools = []
    for m in messages:
        if isinstance(m, ToolMessage) and hasattr(m, "name") and m.name:
            tools.append(m.name)
    return list(dict.fromkeys(tools))
