"""
FastAPI 应用启动入口

Phase 6: depth 参数控制深度（auto/quick/deep），单一 Companion Agent graph 处理所有请求。
单一 Companion Agent graph 处理所有请求。
"""

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from pydantic import BaseModel, Field

from agent.devtools import RequestTelemetry, set_current_telemetry
from agent.graph import agent_app
from agent.memory.cache import get_session_cache
from agent.persona.profiles import get_agent_profile
from agent.state import AgentState
from core.config import get_settings
from database.engine import init_db

settings = get_settings()


def _setup_logging() -> None:
    """初始化 bgm-agent 命名空间下的所有 logger。"""
    level_name = __import__("os").environ.get("BGM_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger("bgm-agent")
    root.setLevel(level)
    root.propagate = False

    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setLevel(level)
        fmt = logging.Formatter(
            "[%(asctime)s] %(levelname)-5s %(name)s | %(message)s",
            datefmt="%m-%d %H:%M:%S",
        )
        handler.setFormatter(fmt)
        root.addHandler(handler)

    if level_name != "DEBUG":
        logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


_setup_logging()
logger = logging.getLogger("bgm-agent")


# ═══════════════════════════════════════════════════════════════════
# 请求/响应模型
# ═══════════════════════════════════════════════════════════════════


class ChatRequest(BaseModel):
    """对话请求。
    发起对话请求
    有三个深度参数 quick、deep、auto，分别对应不同的对话深度和预算。
    1. quick：快速模式，适合简单问题，预算较低，通常在 1-3 轮内完成对话。
    2. deep：深度模式，适合复杂问题，预算较高，通常在 12 轮内完成对话。
    3. auto：自动模式，在5轮内完成对话

    有四种输出风格，neutral、bangumi、bangumi_cold、bangumi_cute，分别对应不同的输出风格。
    1. neutral：中性输出，适合正式场合。
    2. bangumi：Bangumi娘腹黑吐槽，适合娱乐场合。
    3. bangumi_cold：高冷腹黑，适合冷幽默场合。
    4. bangumi_cute：可爱安利，适合可爱风格场合。
    """

    message: str = Field(..., description="用户消息", min_length=1)
    depth: Literal["auto", "quick", "deep"] = Field(
        default="auto",
        description="    1. quick：快速模式，适合简单问题，预算较低，通常在 1-3 轮内完成对话, 2. deep：深度模式，适合复杂问题，预算较高，通常在 12 轮内完成对话, 3. auto：自动模式，在5轮内完成对话",
    )
    output_style: (
        Literal["neutral", "bangumi", "bangumi_cold", "bangumi_cute"] | None
    ) = Field(
        default=None,
        description="输出风格。None=走默认值（bangumi），neutral=中性输出，bangumi=Bangumi娘腹黑吐槽，bangumi_cold=高冷腹黑，bangumi_cute=可爱安利",
    )
    session_id: str = Field(
        default="",
        description="会话 ID，用于 L1 多轮上下文。留空时自动生成随机 UUID",
    )
    user_id: str = Field(default="anonymous", description="用户 ID（L2 跨会话记忆）")


class ChatResponse(BaseModel):
    """对话响应。"""

    reply: str = Field(..., description="Agent 的最终回复")
    iterations: int = Field(..., description="ReAct 循环轮数")
    tools_used: list[str] = Field(
        default_factory=list, description="本轮调用的工具名称"
    )
    query_intent: str = Field(default="unknown", description="查询意图分类结果")
    output_style: str = Field(default="bangumi", description="实际使用的输出渲染风格")
    depth: str = Field(default="auto", description="实际使用的深度模式")
    telemetry: dict | None = Field(
        default=None, description="开发者可观测性数据（仅 DEV_MODE=true 时返回）"
    )


def _resolve_output_style(request: ChatRequest) -> str:
    """确定实际使用的输出风格。

    优先级：用户显式传值 > 默认值 bangumi。

    Args:
        request: 用户请求。

    Returns:
        实际使用的风格 key（"neutral" | "bangumi"）。
    """
    if request.output_style is not None:
        return request.output_style
    return "bangumi"  # Companion Agent 统一默认 Bangumi娘


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

    通过 ``depth`` 控制深度模式：

    - ``"auto"``（默认）：LLM 自行判断，轻量 ReAct ≤5 轮，无 Critic
    - ``"quick"``：强制浅层 1-3 轮，速度快
    - ``"deep"``：高预算（16000 tok）+ 深度人格参数，12 轮迭代上限

    通过 ``depth`` 参数控制：auto（默认 5 轮）、quick（3 轮）、deep（12 轮）。

    Args:
        request: 包含用户消息、深度模式、会话 ID 和用户 ID 的请求体。

    Returns:
        ChatResponse: 包含回复、迭代次数、工具列表、意图分类、深度模式的响应。
    """
    depth = request.depth
    session_id = request.session_id or uuid.uuid4().hex
    output_style = _resolve_output_style(request)

    # ── L1 Session 缓存：恢复同 session 前序消息 ──
    session_cache = get_session_cache()
    cached = await session_cache.load(session_id)

    # 种子 SystemMessage——将在 reasoning_node 中被替换为完整 prompt
    _seed = get_agent_profile("companion").capabilities
    initial_state: AgentState = {
        "messages": [
            SystemMessage(content=_seed),
            *cached,
            HumanMessage(content=request.message),
        ],
        "iterations": 0,
        "query_intent": "unknown",
        "session_id": session_id,
        "user_id": request.user_id,
        "error_flag": False,
        "_memory_context": "",
        "output_style": output_style,
        "depth": depth,
    }

    telemetry = None
    if settings.DEV_MODE:
        telemetry = RequestTelemetry()
        set_current_telemetry(telemetry)

    try:
        if telemetry:
            result = await _run_with_telemetry(initial_state, telemetry)
        else:
            result = await agent_app.ainvoke(initial_state)
    except Exception as e:
        logger.exception("/chat: Agent 执行异常")
        return ChatResponse(
            reply=f"啧，出错了：{e}",
            iterations=0,
            tools_used=[],
            query_intent="unknown",
            output_style=output_style,
            depth=depth,
        )
    finally:
        if telemetry:
            set_current_telemetry(None)

    # ── L1 Session 缓存：保存本轮消息 ──
    max_cached = 30 if depth == "deep" else 20
    await session_cache.store(
        session_id,
        result.get("messages", []),
        max_messages=max_cached,
    )

    # ── L2 记忆写入（fire-and-forget） ──
    asyncio.create_task(_remember_session(result, request, depth))

    from agent.state import get_max_iterations

    max_iterations = get_max_iterations(depth)
    messages = result.get("messages", [])
    return ChatResponse(
        reply=_extract_final_reply(
            messages,
            error_flag=result.get("error_flag", False),
            iterations=result.get("iterations", 0),
            max_iterations=max_iterations,
        ),
        iterations=result.get("iterations", 0),
        tools_used=_extract_tools_used(messages),
        query_intent=result.get("query_intent", "unknown"),
        output_style=output_style,
        depth=depth,
        telemetry=telemetry.to_dict() if telemetry else None,
    )


@app.post("/chat/stream")
async def chat_stream(request: ChatRequest):
    """Agent 对话流式端点（SSE）。

    按节点级别推送事件：reasoning → tool → (critic，仅 deep) → done。

    Args:
        request: 包含用户消息、深度模式、会话 ID 和用户 ID 的请求体。

    Returns:
        StreamingResponse: SSE 事件流（text/event-stream）。
    """
    depth = request.depth
    output_style = _resolve_output_style(request)

    _seed = get_agent_profile("companion").capabilities
    initial_state: AgentState = {
        "messages": [
            SystemMessage(content=_seed),
            HumanMessage(content=request.message),
        ],
        "iterations": 0,
        "query_intent": "unknown",
        "session_id": request.session_id,
        "user_id": request.user_id,
        "error_flag": False,
        "output_style": output_style,
        "depth": depth,
    }

    async def generate():
        try:
            async for event in agent_app.astream(initial_state):
                for node_name, node_output in event.items():
                    if node_name == "reasoning_node":
                        intent = node_output.get("query_intent", "unknown")
                        tool_calls = []
                        for msg in node_output.get("messages", []):
                            if (
                                isinstance(msg, AIMessage)
                                and hasattr(msg, "tool_calls")
                                and msg.tool_calls
                            ):
                                tool_calls = [
                                    tc.get("name", "?") for tc in msg.tool_calls
                                ]
                                break
                        yield f"data: {json.dumps({'node': 'reasoning', 'intent': intent, 'tool_calls': tool_calls}, ensure_ascii=False)}\n\n"

                    elif node_name == "tool_node":
                        tools = []
                        if "messages" in node_output:
                            for msg in node_output["messages"]:
                                if isinstance(msg, ToolMessage) and hasattr(
                                    msg, "name"
                                ):
                                    tools.append(msg.name)
                        yield f"data: {json.dumps({'node': 'tool', 'tools': list(dict.fromkeys(tools))}, ensure_ascii=False)}\n\n"

                    # [DEPRECATED Phase 10] Critic 已从图谱中移除，此分支不再执行。
                    # 保留以备未来恢复 Critic 时重新激活。
                    # elif node_name == "critic_node":
                    #     status = node_output.get("critic_status", "PENDING")
                    #     feedback = node_output.get("critic_feedback", "")
                    #     yield f"data: {json.dumps({'node': 'critic', 'status': status, 'feedback': feedback[:200]}, ensure_ascii=False)}\n\n"

                    elif node_name == "render_node":
                        yield f"data: {json.dumps({'node': 'render'}, ensure_ascii=False)}\n\n"

            yield "data: [DONE]\n\n"

        except Exception as e:
            logger.exception("/chat/stream: Agent 执行异常")
            yield f"data: {json.dumps({'node': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


# ═══════════════════════════════════════════════════════════════════
# DEV_MODE: 节点计时
# ═══════════════════════════════════════════════════════════════════


async def _run_with_telemetry(initial_state: dict, telemetry: RequestTelemetry) -> dict:
    """用 ``astream()`` 跑 graph，记录每个节点的起止时间。

    和 ``ainvoke()`` 的最终结果一致，但额外在过程中记录 NodeTiming。
    """
    import time

    prev_time = telemetry.t_start
    final_state = initial_state

    async for event in agent_app.astream(initial_state):
        now = time.monotonic()
        for node_name, node_output in event.items():
            from agent.devtools import NodeTiming

            elapsed = (now - prev_time) * 1000
            telemetry.add_node_timing(
                NodeTiming(node=node_name, elapsed_ms=int(elapsed))
            )
            prev_time = now
            final_state = node_output

    return final_state


# ═══════════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════════


def _extract_final_reply(
    messages: list,
    error_flag: bool = False,
    iterations: int = 0,
    max_iterations: int = 5,
) -> str:
    """从消息历史中提取最终 AI 回复。

    Args:
        messages: 完整的消息历史列表。
        error_flag: 是否触发了错误降级。
        iterations: 当前迭代次数。
        max_iterations: 最大迭代次数上限。

    Returns:
        最终回复文本。未找到时返回兜底消息。
    """
    for m in reversed(messages):
        if isinstance(m, AIMessage) and m.content:
            return m.content

    if error_flag:
        return "系统处理超时，请简化查询后重试。"

    if iterations >= max_iterations:
        return "查询达到最大处理轮次，请尝试更具体的提问方式。"

    has_tool_results = any(isinstance(m, ToolMessage) for m in messages)
    if has_tool_results:
        return "工具执行完成但未能生成文本回复，请重试或换个方式提问。"

    return "抱歉，无法处理您的请求。"


def _extract_tools_used(messages: list) -> list[str]:
    """从消息历史中提取本轮调用的工具名称列表（去重保序）。

    Args:
        messages: 完整的消息历史列表。

    Returns:
        本轮工具名称列表。
    """
    start_idx = 0
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], HumanMessage):
            start_idx = i
            break

    tools = []
    for m in messages[start_idx:]:
        if isinstance(m, ToolMessage) and hasattr(m, "name") and m.name:
            tools.append(m.name)
    return list(dict.fromkeys(tools))


# ═══════════════════════════════════════════════════════════════════
# L2 记忆写入（fire-and-forget）
# ═══════════════════════════════════════════════════════════════════


async def _remember_session(
    result: dict,
    request: ChatRequest,
    depth: str = "auto",
) -> None:
    """Fire-and-forget: 写入 L2 session 摘要。"""
    try:
        from agent.memory.long_term import get_memory_manager
        from agent.state import get_max_iterations

        mm = get_memory_manager()
        messages: list = result.get("messages", [])
        if not messages:
            return

        max_iterations = get_max_iterations(depth)
        final_reply = _extract_final_reply(
            messages,
            error_flag=result.get("error_flag", False),
            iterations=result.get("iterations", 0),
            max_iterations=max_iterations,
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
            "[Memory] remember_session fire-and-forget 异常 (user=%s, session=%s)",
            request.user_id,
            request.session_id,
            exc_info=True,
        )
