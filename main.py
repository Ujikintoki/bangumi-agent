"""
FastAPI 应用启动入口

depth 参数控制深度（fast/deep），
单一 Companion Agent graph 处理所有请求。
"""

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from pydantic import BaseModel, Field

from agent.devtools import RequestTelemetry, set_current_telemetry
from agent.graph import agent_app
from agent.memory.cache import get_session_cache
from agent.persona.profiles import get_agent_profile, get_character
from agent.persona.render import (
    _extract_user_query as _extract_user_query_from_messages,
)
from agent.persona.render import render_fallback_line, render_reply, tool_outcome_line
from agent.state import AgentState
from core.config import get_settings
from database.engine import init_db
from middleware import rate_limit_middleware

try:
    from langgraph.errors import GraphRecursionError
except ImportError:
    GraphRecursionError = Exception  # type: ignore[assignment,misc]

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
    有两种深度模式 fast、deep，分别对应不同的对话深度和预算。
    1. fast（默认）：10000 tok 预算，快速获取核心数据。
    2. deep：16000 tok 预算，更高迭代上限，深度链式调用。
    迭代上限按 intent 细分，见 agent/config.py 的 per-intent 表。

    有三种输出风格，neutral、bangumi、bangumi_kawaii，分别对应不同的输出风格。
    1. neutral：中性输出，适合正式场合。
    2. bangumi：Bangumi娘腹黑吐槽，适合娱乐场合。
    3. bangumi_kawaii：可爱分享者，真诚安利，发现闪光点。
    """

    message: str = Field(..., description="用户消息", min_length=1)
    depth: Literal["fast", "deep"] = Field(
        default="fast",
        description="深度模式：fast（默认，低预算快速获取核心数据）、deep（高预算深度链式调用）。迭代上限按 intent 细分，见 agent/config.py",
    )
    output_style: Literal["neutral", "bangumi", "bangumi_kawaii"] | None = Field(
        default=None,
        description="输出风格。None=走默认值（bangumi），neutral=中性输出，bangumi=Bangumi娘腹黑吐槽，bangumi_kawaii=可爱分享者",
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
    depth: str = Field(default="fast", description="实际使用的深度模式")
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

# CORS: 不使用 cookie 认证（session 通过显式 session_id 参数追踪），因此不设 credentials。
# origin 保持通配符——此 API 为公开服务，接受来自任意前端的请求。
#
# 注意：限流中间件直接返回 429 时不经过外层 CORS（BaseHTTPMiddleware 行为），
# 其 CORS 头由 middleware.py 自补，与注册顺序无关。
app.middleware("http")(rate_limit_middleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """全局兜底：handler 内逃逸的异常统一 JSON 500 + 单点日志。

    注意：middleware 抛出的异常与 SSE 开流后的异常不经过此处，
    分别由 middleware 自身和 generator 内部 try/except 兜底。
    """
    logger.exception(
        "未捕获异常 (path=%s method=%s)", request.url.path, request.method
    )
    return JSONResponse(
        status_code=500,
        content={"error": "internal_error", "message": "服务开小差了，请稍后重试。"},
    )

# ═══════════════════════════════════════════════════════════════════
# 隐式终止 — 统一渲染路径
# ═══════════════════════════════════════════════════════════════════


def _extract_tools_used(messages: list) -> list[str]:
    """从消息历史中提取本轮调用的工具名称列表（去重保序）。"""
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


def _degrade_render_input(text: str) -> str:
    """render 失败时的降级清理：去 emoji、markdown 格式、多余空白。

    用于隐式终止路径：当 render_reply 返回 None 时，
    对 Aggregator 原始文本做基础清理，避免 emoji/markdown table 泄漏到用户端。

    ⚠ 只接受【本来就能给用户看】的文本（Aggregator 的输出）。
    不要把 chat 分支的 render_input 传进来 —— 那是给模型看的指令，
    清 emoji/markdown 不会让它变成可以示人的正文（曾经就是这样泄漏内部提示词的）。

    Args:
        text: Aggregator 输出的原始文本。

    Returns:
        清理后的纯文本。
    """
    import re

    # 去 emoji（Unicode 表情符号区块）
    text = re.sub(
        r"[\U0001F300-\U0001F9FF☀-➿⭐✀-➿️]",
        "",
        text,
    )
    # 去 markdown table 行（以 | 开头和结尾）
    text = re.sub(r"^\|.*\|$", "", text, flags=re.MULTILINE)
    # 去 markdown 标题标记（## 等）
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    # 去 markdown 粗体/斜体
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    # 去 markdown 分隔线
    text = re.sub(r"^[-*_]{3,}$", "", text, flags=re.MULTILINE)
    # 合并多余空行
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


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

    - ``"fast"``（默认）：低预算（10000 tok），快速获取核心数据
    - ``"deep"``：高预算（16000 tok）+ 深度人格参数
    迭代上限按 intent 细分（agent/config.py 的 per-intent 表）。

    Args:
        request: 包含用户消息、深度模式、会话 ID 和用户 ID 的请求体。

    Returns:
        ChatResponse: 包含回复、迭代次数、工具列表、意图分类、深度模式的响应。
    """
    initial_state, session_id, depth, output_style = await _build_initial_state(
        request
    )

    telemetry = None
    if settings.DEV_MODE:
        telemetry = RequestTelemetry()
        set_current_telemetry(telemetry)

    timeout_s = (
        settings.REQUEST_TIMEOUT_FAST
        if depth == "fast"
        else settings.REQUEST_TIMEOUT_DEEP
    )

    async def _graph_and_render():
        """graph 执行 + 统一渲染，两者共享同一请求级超时。

        渲染是独立 LLM 调用，若在 wait_for 之外，挂起的 render 会绕过
        请求超时（审计盲区修复）。
        """
        if telemetry:
            result = await _run_with_telemetry(initial_state, telemetry)
        else:
            result = await agent_app.ainvoke(
                initial_state, config={"recursion_limit": 50}
            )
        messages_for_render = result.get("messages", [])
        user_query = (
            _extract_user_query_from_messages(messages_for_render) or request.message
        )
        query_intent = result.get("query_intent", "fallback")
        messages_for_render, rendered = await _render_final_reply(
            messages=messages_for_render,
            user_query=user_query,
            query_intent=query_intent,
            output_style=output_style,
            depth=depth,
        )
        if rendered:
            result["messages"] = messages_for_render
        return result, query_intent

    try:
        result, query_intent = await asyncio.wait_for(
            _graph_and_render(), timeout=timeout_s
        )
    except asyncio.TimeoutError:
        # TimeoutError 是 Exception 子类，必须放在 except Exception 之前
        logger.warning("/chat: 请求超时 (depth=%s, timeout=%ss)", depth, timeout_s)
        return ChatResponse(
            reply="查询处理超时，请尝试更具体的提问方式。",
            iterations=0,
            tools_used=[],
            query_intent="unknown",
            output_style=output_style,
            depth=depth,
        )
    except GraphRecursionError:
        logger.warning("/chat: recursion_limit 触发 (depth=%s)", depth)
        return ChatResponse(
            reply="查询处理超时，请尝试更具体的提问方式。",
            iterations=0,
            tools_used=[],
            query_intent="unknown",
            output_style=output_style,
            depth=depth,
        )
    except Exception:
        logger.exception("/chat: Agent 执行异常")
        return ChatResponse(
            reply="抱歉，处理请求时遇到了问题，请稍后重试。",
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
    session_cache = get_session_cache()
    max_cached = 30 if depth == "deep" else 20
    await session_cache.store(
        session_id,
        result.get("messages", []),
        max_messages=max_cached,
    )

    # ── L2 记忆写入（fire-and-forget） ──
    asyncio.create_task(
        _remember_session(result, request, depth, session_id=session_id)
    )

    from agent.config import get_max_iterations

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

    Graph 推理层全部使用非流式 ainvoke，不输出中间节点事件。
    SSE 只包含最终 render 事件，保留流式接口形态供未来扩展（如 token 级流式渲染）。

    Args:
        request: 包含用户消息、深度模式、会话 ID 和用户 ID 的请求体。

    Returns:
        StreamingResponse: SSE 事件流（text/event-stream）。
    """
    initial_state, session_id, depth, output_style = await _build_initial_state(
        request
    )

    async def generate():
        session_cache = get_session_cache()  # 单例，恢复/保存 L1 session
        final_state: dict = dict(
            initial_state
        )  # 预初始化，防止 CancelledError 提前触发时 UnboundLocalError
        try:
            # ── Graph 执行 + 统一渲染（共享请求级超时，审计盲区修复）──
            timeout_s = (
                settings.REQUEST_TIMEOUT_FAST
                if depth == "fast"
                else settings.REQUEST_TIMEOUT_DEEP
            )

            async def _graph_and_render():
                state = await agent_app.ainvoke(
                    initial_state, config={"recursion_limit": 50}
                )
                messages_for_render = state.get("messages", [])
                user_query = (
                    _extract_user_query_from_messages(messages_for_render)
                    or request.message
                )
                query_intent = state.get("query_intent", "fallback")
                messages_for_render, reply_to_send = await _render_final_reply(
                    messages=messages_for_render,
                    user_query=user_query,
                    query_intent=query_intent,
                    output_style=output_style,
                    depth=depth,
                )
                if reply_to_send:
                    state["messages"] = messages_for_render
                return state, reply_to_send

            final_state, reply_to_send = await asyncio.wait_for(
                _graph_and_render(), timeout=timeout_s
            )

            # ── 发送 render 事件 + 最终回复 ──
            yield f"data: {json.dumps({'node': 'render', 'reply': reply_to_send}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

            # ── L1 Session 缓存 + L2 记忆写入 ──
            max_cached = 30 if depth == "deep" else 20
            await session_cache.store(
                session_id,
                final_state.get("messages", []),
                max_messages=max_cached,
            )
            asyncio.create_task(
                _remember_session(final_state, request, depth, session_id=session_id)
            )

        except asyncio.TimeoutError:
            # 请求级超时——必须放在 except Exception 之前（TimeoutError 是其子类）
            logger.warning(
                "/chat/stream: 请求超时 (depth=%s, timeout=%ss)", depth, timeout_s
            )
            yield f"data: {json.dumps({'node': 'error', 'message': '查询处理超时，请尝试更具体的提问方式。'}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        except GraphRecursionError:
            logger.warning("/chat/stream: recursion_limit 触发 (depth=%s)", depth)
            yield f"data: {json.dumps({'node': 'error', 'message': '查询处理超时，请尝试更具体的提问方式。'}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        except asyncio.CancelledError:
            # 客户端断开 — CancelledError 是 BaseException 子类，不被 except Exception 捕获。
            # 在此尽力保存 L1 + L2 状态（用 shield 防止被二次取消）。
            logger.warning("/chat/stream: 客户端断开，尝试紧急保存 session 状态")
            try:
                await asyncio.shield(
                    session_cache.store(
                        session_id,
                        final_state.get("messages", []),
                        max_messages=30 if depth == "deep" else 20,
                    )
                )
            except Exception:
                pass
            # 注意：断开时只保存 L1 session 缓存，不写 L2 长期记忆——
            # 中断的对话不完整（可能只执行到一半），写入会污染语义召回
            # 的注入内容（审计盲区修复）。
            # 连接已断开，不发送 SSE 事件，直接退出生成器
        except Exception:
            logger.exception("/chat/stream: Agent 执行异常")
            yield f"data: {json.dumps({'node': 'error', 'message': '内部处理错误，请稍后重试'}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


# ═══════════════════════════════════════════════════════════════════
# DEV_MODE: 节点计时
# ═══════════════════════════════════════════════════════════════════


async def _run_with_telemetry(initial_state: dict, telemetry: RequestTelemetry) -> dict:
    """用 ``astream()`` 跑 graph，记录每个节点的起止时间并累积完整 state。

    和 ``ainvoke()`` 的最终结果一致，但额外在过程中记录 NodeTiming。
    ``stream_mode="updates"`` 每个事件仅是节点 delta，需累积合并为完整 state。
    """
    import time

    prev_time = telemetry.t_start
    final_state: dict = dict(initial_state)
    messages_key = "messages"

    async for event in agent_app.astream(initial_state, config={"recursion_limit": 50}):
        now = time.monotonic()
        for node_name, node_output in event.items():
            if node_output is None:
                continue
            from agent.devtools import NodeTiming

            elapsed = (now - prev_time) * 1000
            telemetry.add_node_timing(
                NodeTiming(node=node_name, elapsed_ms=int(elapsed))
            )
            prev_time = now
            # 累积合并：messages 追加，其余键覆盖
            for key, value in node_output.items():
                if key == messages_key and key in final_state:
                    final_state[messages_key].extend(value)
                else:
                    final_state[key] = value

    return final_state


# ═══════════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════════


async def _build_initial_state(
    request: ChatRequest,
) -> tuple[AgentState, str, str, str]:
    """构造 graph 初始状态（/chat 与 /chat/stream 共用）。

    包含：depth/output_style 解析、L1 session 缓存恢复、种子 SystemMessage。

    Returns:
        (initial_state, session_id, depth, output_style)
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
        "_memory_context": None,
        "output_style": output_style,
        "depth": depth,
    }
    return initial_state, session_id, depth, output_style


async def _render_final_reply(
    messages: list,
    user_query: str,
    query_intent: str,
    output_style: str,
    depth: str,
) -> tuple[list, str | None]:
    """统一渲染路径（/chat 与 /chat/stream 共用，隐式终止后处理）。

    - chat 意图：纯闲聊，无工具数据，直接用人格回复。
    - 其余：隐式终止——取 Aggregator 文本摘要交给 render。
    - 模型没吐文本（熔断掐断）：转 ``_render_no_text_followup``，那里必定给出回复。
    - render 失败时：有可示人的原文就清理后降级，没有（chat）就回落兜底话术。

    render_input 与 fallback 是两件事，**不要合并**：
      render_input = 喂给模型的输入（chat 分支里全是脚手架，不可示人）
      fallback     = 本来就能给用户看的原文（只有非 chat 分支的 Aggregator 摘要有）

    Returns:
        (更新后的 messages, 最终回复文本)。末行的 None 是防御性兜底，
        正常路径不会再走到（非 chat 分支要么有摘要、要么已由上面那条转走）。
    """
    if query_intent == "chat":
        render_input = (
            f"用户对你说：{user_query}\n\n"
            "这是一段闲聊。自然地用你的角色性格回复。"
            "不要列数据、不要提搜索、就像朋友聊天一样。"
        )
        force_render = True
        fallback = None
    else:
        # 隐式终止：直接使用 Aggregator 文本摘要
        last_ai = _get_last_ai_message(messages)
        if last_ai and last_ai.content:
            render_input = last_ai.content
            fallback = render_input
            force_render = True
            logger.info(
                "render: 隐式终止 — 使用 Aggregator 文本摘要 (%d chars)",
                len(render_input),
            )
        else:
            # 模型一句话都没说。见 _render_no_text_followup —— 这里不能塞个
            # "（无数据）"就撒手：render 会因输入过短跳过它，用户拿到的是
            # 「工具执行完成但未能生成文本回复」。
            return await _render_no_text_followup(
                messages, user_query, output_style, depth
            )

    character = get_character(output_style)
    rendered = await render_reply(
        render_input=render_input,
        user_query=user_query,
        character=character,
        depth=depth,
        force=force_render,
    )
    if rendered:
        return _replace_last_ai_content(messages, rendered), rendered
    if fallback is not None:
        # 降级：render 失败时清理原始文本，避免 emoji/markdown 泄漏。
        # 判断条件是"有没有可示人的原文"，不是"有没有数据"——后者放过 chat 分支。
        cleaned = _degrade_render_input(fallback)
        logger.warning(
            "Render 失败，降级为清理后的原始文本 (%d → %d chars)",
            len(fallback),
            len(cleaned),
        )
        return _replace_last_ai_content(messages, cleaned), cleaned
    if query_intent == "chat":
        # chat 分支：render_input 是给模型看的指令，一个字都不能吐给用户；
        # 也不能返回 None —— messages 里还躺着 L1 缓存中上一轮的 AIMessage
        # （chat 直通 END，本轮不产生新 AIMessage），端点的 _extract_final_reply
        # 会往上翻把它当成这一轮的回答，用户看到的是复读。
        line = render_fallback_line(character)
        logger.warning("Render 失败，chat 分支回落通用话术 (%d chars)", len(line))
        return _replace_last_ai_content(messages, line), line
    return messages, None


# ═══════════════════════════════════════════════════════════════════
# 模型未收尾时的降级（方案 C：人格层交代 + 兜底话术）
# ═══════════════════════════════════════════════════════════════════

# 喂给 render 的"事实"——把实际发生的结局摊开讲，让角色自己组织语言。
# render prompt 会把它塞进 <system_retrieved_facts> 当作数据看待。
#
# ⚠ 那三行"不许"是被实测逼出来的，不是以防万一：
#   1. 不禁"印象里很高"这类软话，模型就会用"几季都在前列"填答案——它没说数字，
#      但仍然是没有任何证据支撑的断言，正是本项目的忠实性判官要判 unsupported 的形态。
#   2. 不禁"接口/系统"，render 会照着能力自述里的"API 查询"说"接口挂了"——
#      住在站里的角色不该知道"接口"是什么。
# 改这段等于改降级时的人格表达，改完要重跑 probe_degrade_wording 看有没有回潮。
_NO_DATA_RENDER_INPUT: dict[str, str] = {
    "blocked": (
        "（情况：你刚去站里翻数据，没连上，一条都没取到。"
        "你只需要把“这次没查到、过会儿再来”告诉用户。"
        "不要补充任何作品信息、评分、排名、日期或推荐——"
        "连“印象里很高”“应该挺强”这种也不许说。"
        "也别提接口、系统、工具这些词，就当自己跑了一趟没跑成。）"
    ),
    "empty": (
        "（情况：你刚去站里翻过了，没有找到匹配的内容。"
        "你只需要把“没找着、换个说法”告诉用户。"
        "不要补充任何作品信息、评分、排名、日期或推荐——"
        "连“印象里好像有”这种也不许说。"
        "也别提接口、系统、工具这些词，就当自己翻了一圈没翻着。）"
    ),
}

_ERROR_KEY_SUFFIX = "_error"


def _tool_outcome(messages: list) -> str:
    """倒推工具这一轮到底给了什么 —— 决定降级时该说哪种"没有"。

    能走到这里，前提是模型一句话都没吐：熔断（连续空搜索 / 重复调用 / 迭代上限）
    在 tool_node 之后直接掐断了图，最后一条消息是 ToolMessage。所以要靠工具结果
    本身判断该跟用户交代什么。

    Returns:
        ``"no_tools"`` 压根没查过（模型空手而归，不是查了没有）
        ``"has_data"`` 至少一条结果有可用内容（问题是模型没接住，不是没查到）
        ``"blocked"``  结果全是错误（够不着 / 连不上）
        ``"empty"``    有结果但都空（翻过了，没有）
    """
    kinds = [
        _tool_payload_kind(m.content)
        for m in messages
        if isinstance(m, ToolMessage)
    ]
    if not kinds:
        # 一次都没查过却说"翻了一圈没找着"是撒谎 —— 跟"有数据说没查到"一样，
        # 都属于把实际发生的事说错。两者都该走"我没接住"。
        return "no_tools"
    if "data" in kinds:
        return "has_data"
    if "error" in kinds:
        return "blocked"
    return "empty"


def _tool_payload_kind(content) -> str:
    """把一条 ToolMessage 的 content 归成 error / data / empty。"""
    text = content if isinstance(content, str) else str(content)
    try:
        obj = json.loads(text)
    except (TypeError, ValueError):
        # 不是 JSON：有字就算有内容（工具偶尔返回纯文本），空串就是空。
        return "data" if text.strip() else "empty"

    if isinstance(obj, dict):
        # 错误形状有两层：约定内的顶层 _error，以及 get_user_profile 那类
        # {key}_error 子键（如 comments_error）—— 后者是部分失败，同样没数据。
        if "_error" in obj or any(k.endswith(_ERROR_KEY_SUFFIX) for k in obj):
            return "error"
        containers = [v for v in obj.values() if isinstance(v, (list, dict))]
        # 只有标量（如 {"total": 0}）不算内容：没有可供用户看的实体。
        return "data" if any(containers) else "empty"
    if isinstance(obj, list):
        return "data" if obj else "empty"
    return "empty"


async def _render_no_text_followup(
    messages: list,
    user_query: str,
    output_style: str,
    depth: str,
) -> tuple[list, str]:
    """模型没吐出任何文本时的收尾（方案 C）。

    A：把实际结局当事实交给 render，让角色用人格说出来。
    B：render 也失败 → 回落不经 LLM 的结局话术，绝不把技术黑话丢给用户。

    ⚠ 一律【追加】AIMessage，不用 ``_replace_last_ai_content``：此刻最后一条
    AIMessage 是空 content + tool_calls，替换会把它的 ToolMessage 变成孤儿
    （配对丢失）。追加得到的转录仍然合法：
    ``AIMessage(tool_calls) → ToolMessage → AIMessage(text)``。

    Returns:
        (更新后的 messages, 回复文本)。这里永远给得出文本，不会返回 None。
    """
    character = get_character(output_style)
    outcome = _tool_outcome(messages)

    if outcome in ("has_data", "no_tools"):
        # 有数据却没接住（或压根没查成）—— 该说的是"我走神了"，不是"我没查到"。
        # 这两个结局没有"数据"要转述，喂 render 只会逼它去编，所以根本不调。
        line = render_fallback_line(character)
        logger.warning("render: 模型未收尾（%s）→ 走神话术 (%d chars)", outcome, len(line))
        return messages + [AIMessage(content=line)], line

    logger.warning("render: 模型未收尾（%s），交给人格层交代", outcome)
    try:
        rendered = await render_reply(
            render_input=_NO_DATA_RENDER_INPUT[outcome],
            user_query=user_query,
            character=character,
            depth=depth,
            force=True,
        )
    except Exception:
        # render_reply 自己吞异常，但它在进 try 之前先调了 build_render_prompt
        # （人格卡/格式串坏了就会炸）。这里是最后一层防线，兜底话术必须说到做到。
        logger.exception("render: 结局交代渲染异常")
        rendered = None
    if rendered:
        return messages + [AIMessage(content=rendered)], rendered

    line = tool_outcome_line(character, blocked=(outcome == "blocked"))
    logger.warning("render: 结局交代渲染失败 → 回落 %s 兜底话术", outcome)
    return messages + [AIMessage(content=line)], line


def _get_last_ai_message(messages: list):
    """从后往前取最后一条有 content 的 AIMessage。"""
    for m in reversed(messages):
        if isinstance(m, AIMessage) and m.content:
            return m
    return None


def _replace_last_ai_content(messages: list, new_content: str) -> list:
    """返回新列表，最后一条 AIMessage 被替换为只有 new_content 的 AIMessage。

    v2: aggregator 最后一条 AIMessage 可能只有 tool_calls 而 content 为空。
    此函数匹配任意 AIMessage（含空 content 的），替换为纯文本 AIMessage。
    找不到时追加一条新的。
    """
    result = list(messages)
    for i in range(len(result) - 1, -1, -1):
        if isinstance(result[i], AIMessage):
            result[i] = AIMessage(
                content=new_content,
                response_metadata=getattr(result[i], "response_metadata", {}),
                id=getattr(result[i], "id", None),
            )
            return result
    # 没有找到任何 AIMessage → 追加
    result.append(AIMessage(content=new_content))
    return result


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


# ═══════════════════════════════════════════════════════════════════
# L2 记忆写入（fire-and-forget）
# ═══════════════════════════════════════════════════════════════════


async def _remember_session(
    result: dict,
    request: ChatRequest,
    depth: str = "fast",
    session_id: str | None = None,
) -> None:
    """Fire-and-forget: 写入 L2 session 摘要。

    Args:
        session_id: 实际使用的 session ID（优先于 request.session_id，避免匿名 session 塌缩）。
    """
    try:
        from agent.config import get_max_iterations
        from agent.memory.long_term import get_memory_manager

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

        effective_session_id = session_id or request.session_id
        query_intent = result.get("query_intent", "unknown")

        await asyncio.wait_for(
            mm.remember_session(
                session_id=effective_session_id,
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
            effective_session_id,
        )
    except Exception:
        logger.warning(
            "[Memory] remember_session fire-and-forget 异常 (user=%s, session=%s)",
            request.user_id,
            effective_session_id,
            exc_info=True,
        )
