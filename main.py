"""
FastAPI 应用启动入口

初始化 FastAPI 实例，配置 CORS 中间件与生命周期事件，
提供健康检查、Agent 对话和流式输出端点。
"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from pydantic import BaseModel, Field

from agent.dialogue.graph import dialogue_app
from agent.dialogue.prompts import DIALOGUE_SYSTEM_PROMPT
from agent.dialogue.state import DialogueState
from agent.research.graph import agent_app
from agent.research.prompts import BASE_SYSTEM_PROMPT
from agent.research.state import AgentState
from agent.session_cache import get_session_cache
from core.config import get_settings
from database.engine import init_db

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
    agent_type: Literal["dialogue", "research"] = Field(
        default="dialogue",
        description="Agent 类型：dialogue（快速对话/Bangumi娘）或 research（深度搜索/中性助手）",
    )
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
    init_db()
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

    通过 ``agent_type`` 选择 Agent：

    - ``"dialogue"``（默认）：Bangumi娘人格，2 节点拓扑，无 Critic，
      回复 30-150 字，<2s 延迟。适合日常闲聊和快速查询。
    - ``"research"``：中性助手，3 节点拓扑，Critic 质量自省，
      深度链式工具调用。适合需要完整数据的复杂查询。

    Args:
        request: 包含用户消息、Agent 类型、会话 ID 和用户 ID 的请求体。

    Returns:
        ChatResponse: 包含回复、迭代次数、工具列表和意图分类的响应。
    """
    if request.agent_type == "research":
        return await _chat_research(request)
    return await _chat_dialogue(request)


async def _chat_dialogue(request: ChatRequest) -> ChatResponse:
    """Dialogue Agent 内部处理。"""
    # ── L1 Session 缓存：恢复同 session 前序消息 ──
    session_cache = get_session_cache()
    cached = await session_cache.load(request.session_id)

    initial_state: DialogueState = {
        "messages": [
            SystemMessage(content=DIALOGUE_SYSTEM_PROMPT),
            *cached,  # 前序对话（不含 SystemMessage）
            HumanMessage(content=request.message),
        ],
        "iterations": 0,
        "query_intent": "unknown",
        "session_id": request.session_id,
        "user_id": request.user_id,
        "_memory_context": "",
    }

    try:
        result = await dialogue_app.ainvoke(initial_state)
    except Exception as e:
        logger.exception("/chat (dialogue): Agent 执行异常")
        return ChatResponse(
            reply=f"啧，出错了：{e}",
            iterations=0,
            tools_used=[],
            query_intent="unknown",
        )

    # ── L1 Session 缓存：保存本轮消息（Dialogue 最多 20 条） ──
    await session_cache.store(
        request.session_id,
        result.get("messages", []),
        max_messages=20,
    )

    # ── L2 记忆写入（fire-and-forget，不阻塞响应） ──
    asyncio.create_task(_remember_session(result, request))

    messages = result.get("messages", [])
    return ChatResponse(
        reply=_extract_final_reply(
            messages,
            iterations=result.get("iterations", 0),
            max_iterations=4,
        ),
        iterations=result.get("iterations", 0),
        tools_used=_extract_tools_used(messages),
        query_intent=result.get("query_intent", "unknown"),
    )


async def _chat_research(request: ChatRequest) -> ChatResponse:
    """Research Agent 内部处理。"""
    # ── L1 Session 缓存：恢复同 session 前序消息 ──
    session_cache = get_session_cache()
    cached = await session_cache.load(request.session_id)

    initial_state: AgentState = {
        "messages": [
            SystemMessage(content=BASE_SYSTEM_PROMPT),
            *cached,  # 前序对话（不含 SystemMessage）
            HumanMessage(content=request.message),
        ],
        "iterations": 0,
        "critic_status": "PENDING",
        "critic_feedback": "",
        "query_intent": "unknown",
        "session_id": request.session_id,
        "user_id": request.user_id,
        "error_flag": False,
        "_memory_context": "",
    }

    try:
        result = await agent_app.ainvoke(initial_state)
    except Exception as e:
        logger.exception("/chat (research): Agent 执行异常")
        return ChatResponse(
            reply=f"系统异常：{e}",
            iterations=0,
            tools_used=[],
            query_intent="unknown",
        )

    # ── L1 Session 缓存：保存本轮消息（Research 最多 30 条） ──
    await session_cache.store(
        request.session_id,
        result.get("messages", []),
        max_messages=30,
    )

    # ── L2 记忆写入（fire-and-forget，不阻塞响应） ──
    asyncio.create_task(_remember_session(result, request))

    messages = result.get("messages", [])
    return ChatResponse(
        reply=_extract_final_reply(
            messages,
            error_flag=result.get("error_flag", False),
            iterations=result.get("iterations", 0),
            max_iterations=12,
        ),
        iterations=result.get("iterations", 0),
        tools_used=_extract_tools_used(messages),
        query_intent=result.get("query_intent", "unknown"),
    )


@app.post("/chat/stream")
async def chat_stream(request: ChatRequest):
    """Agent 对话流式端点（SSE）。

    通过 ``agent_type`` 选择 Agent。按节点级别推送事件。
    Research: reasoning → tool → critic → done。
    Dialogue: reasoning → tool → done（无 critic 节点）。

    Args:
        request: 包含用户消息、Agent 类型、会话 ID 和用户 ID 的请求体。

    Returns:
        StreamingResponse: SSE 事件流（text/event-stream）。
    """
    if request.agent_type == "dialogue":
        initial_state: DialogueState = {
            "messages": [
                SystemMessage(content=DIALOGUE_SYSTEM_PROMPT),
                HumanMessage(content=request.message),
            ],
            "iterations": 0,
            "query_intent": "unknown",
            "session_id": request.session_id,
            "user_id": request.user_id,
        }
        graph_app = dialogue_app
    else:
        initial_state: AgentState = {
            "messages": [
                SystemMessage(content=BASE_SYSTEM_PROMPT),
                HumanMessage(content=request.message),
            ],
            "iterations": 0,
            "critic_status": "PENDING",
            "critic_feedback": "",
            "query_intent": "unknown",
            "session_id": request.session_id,
            "user_id": request.user_id,
            "error_flag": False,
        }
        graph_app = agent_app

    async def generate():
        try:
            async for event in graph_app.astream(initial_state):
                for node_name, node_output in event.items():
                    if node_name in ("reasoning_node", "dialogue_reasoning_node"):
                        intent = node_output.get("query_intent", "unknown")
                        tool_calls = []
                        for msg in node_output.get("messages", []):
                            if isinstance(msg, AIMessage) and hasattr(msg, "tool_calls") and msg.tool_calls:
                                tool_calls = [tc.get("name", "?") for tc in msg.tool_calls]
                                break
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


def _extract_final_reply(
    messages: list,
    error_flag: bool = False,
    iterations: int = 0,
    max_iterations: int = 10,
) -> str:
    """从消息历史中提取最终 AI 回复。

    查找最后一条有实质内容的 AIMessage。与 Critic 的 ``_get_last_ai_response``
    标准一致：有 content 即视为有效回复，不因附带 tool_calls 而拒绝。

    兜底消息根据失败原因提供区分度更高的提示。

    Args:
        messages: 完整的消息历史列表。
        error_flag: 是否触发了错误降级。
        iterations: 当前迭代次数（用于超限提示）。
        max_iterations: 最大迭代次数上限。

    Returns:
        最终回复文本。未找到时返回兜底消息。
    """
    for m in reversed(messages):
        if isinstance(m, AIMessage) and m.content:
            return m.content

    # ── 区分化的兜底消息 ────────────────────────────────────
    if error_flag:
        return "系统处理超时，请简化查询后重试。"

    if iterations >= max_iterations:
        return "查询达到最大处理轮次，请尝试更具体的提问方式。"

    # 检查是否有工具执行但无文本回复
    has_tool_results = any(
        isinstance(m, ToolMessage) for m in messages
    )
    if has_tool_results:
        return "工具执行完成但未能生成文本回复，请重试或换个方式提问。"

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


# ═══════════════════════════════════════════════════════════════════
# L2 记忆写入（fire-and-forget）
# ═══════════════════════════════════════════════════════════════════


async def _remember_session(
    result: dict,
    request: ChatRequest,
) -> None:
    """Fire-and-forget: 写入 L2 session 摘要 + 增量更新用户画像。

    Agent 返回结果后，在后台异步执行 LLM 摘要 → embedding →
    INSERT session_memories + UPSERT user_profiles。不阻塞
    HTTP 响应——用户感知延迟为零。

    整个 remember_session 链路设有 15 秒硬超时。正常链路
    （摘要 LLM 10s + embedding 0.1s + DB <0.1s）约 10-11 秒，
    15 秒提供 ~50% 余量，允许轻微响应波动通过。

    注意：流式端点 ``POST /chat/stream`` 未接入记忆写入。
    ``astream()`` 逐个 yield 节点事件，无干净的"最终结果点"，
    强行接入会在 ``[DONE]`` 前卡 ~1s。后续可考虑收集完整事件后
    在生成器末尾调度。

    Args:
        result: agent_graph.ainvoke() 的完整返回 dict。
        request: 原始 ChatRequest（含 session_id, user_id）。
    """
    try:
        from agent.memory_manager import get_memory_manager

        mm = get_memory_manager()
        messages: list = result.get("messages", [])
        if not messages:
            return

        final_reply = _extract_final_reply(
            messages,
            error_flag=result.get("error_flag", False),
            iterations=result.get("iterations", 0),
            max_iterations=10,
        )

        query_intent = result.get("query_intent", "unknown")

        await asyncio.wait_for(
            mm.remember_session(
                session_id=request.session_id,
                user_id=request.user_id,
                messages=messages,
                final_reply=final_reply,
                query_intent=query_intent,
            ),
            timeout=15.0,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "[Memory] remember_session 超时 (user=%s, session=%s, timeout=15s)",
            request.user_id,
            request.session_id,
        )
    except Exception:
        logger.warning(
            "[Memory] remember_session fire-and-forget 异常 "
            "(user=%s, session=%s)",
            request.user_id,
            request.session_id,
            exc_info=True,
        )
