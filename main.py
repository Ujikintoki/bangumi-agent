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
from agent.orchestrate.nodes import _count_consecutive_empty_searches
from agent.persona.profiles import get_agent_profile, get_character
from agent.persona.render import render_reply
from agent.persona.render import _extract_user_query as _extract_user_query_from_messages
from agent.state import AgentState
from core.config import get_settings
from database.engine import init_db

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
# v2 分离合成 — 代码中转层
# ═══════════════════════════════════════════════════════════════════


def _find_submit_facts(messages: list) -> dict | None:
    """从消息历史中找到 submit_facts_to_render 的 ToolMessage 并解析内容。

    LangChain ToolNode 可能将 dict return 转为 Python repr（单引号）或 JSON 字符串，
    两种格式都需要处理。

    Args:
        messages: graph 返回的消息列表。

    Returns:
        解析后的 facts dict，未找到时返回 None。
    """
    import ast

    for m in reversed(messages):
        if isinstance(m, ToolMessage) and getattr(m, "name", "") == "submit_facts_to_render":
            content = m.content if hasattr(m, "content") else ""
            if isinstance(content, dict):
                return content
            if isinstance(content, str) and content:
                # 尝试 JSON 解析
                try:
                    return json.loads(content)
                except json.JSONDecodeError:
                    pass
                # 尝试 Python literal 解析（ToolNode 可能输出 repr 格式）
                try:
                    parsed = ast.literal_eval(content)
                    if isinstance(parsed, dict):
                        return parsed
                except (ValueError, SyntaxError):
                    pass
                logger.warning("_find_submit_facts: 无法解析 ToolMessage content: %.100s", content)
            break
    return None


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


def _data_level_label(depth: str) -> str:
    """depth 模式 → 数据等级标签。"""
    return {"quick": "L1（摘要）", "auto": "L1-L2（摘要+详情）", "deep": "L1-L3（完整）"}.get(
        depth, "L1-L2"
    )


def _build_render_input(
    facts_json: dict | None,
    state: dict,
    user_query: str,
    depth: str,
) -> str:
    """确定性拼接 Render 的输入 Markdown（v2 代码中转层）。

    不从 LLM 拿文本——从 submit_facts JSON（Aggregator 的 ToolMessage）
    和 AgentState（系统数据）确定性地构建。

    Args:
        facts_json: submit_facts_to_render 返回的 dict，None 则用空数据。
        state: graph 返回的完整 state dict。
        user_query: 用户原始消息。
        depth: 深度模式。

    Returns:
        供 Render LLM 消费的 Markdown 字符串。
    """
    if facts_json is None:
        facts_json = {"facts": [], "intent": "unknown", "missing": "数据收集未完成"}

    facts = facts_json.get("facts", [])
    intent = facts_json.get("intent", "未知")
    missing = facts_json.get("missing", "")

    # ── 系统状态（代码提取，不靠 LLM 自述）──
    messages = state.get("messages", [])
    iterations = state.get("iterations", 0)
    tools_used = _extract_tools_used(messages)
    empty_count = _count_consecutive_empty_searches(messages)
    data_level = _data_level_label(depth)

    # ── 拼接 ──
    lines = [
        "## 数据清单",
        f"查询意图: {intent}",
        "",
    ]

    if facts:
        for i, f in enumerate(facts, 1):
            if isinstance(f, dict):
                name = f.get("name", "?")
                score = f.get("score")
                score_str = f"{score}分" if score is not None else "暂无评分"
                rank = f.get("rank")
                rank_str = f"#{rank}" if rank else ""
                summary = f.get("summary", "")
                tags = f.get("tags", "")
                source = f.get("source", "")

                line = f"{i}. {name} | {score_str}"
                if rank_str:
                    line += f" | {rank_str}"
                if summary:
                    line += f"\n   {summary[:300]}"
                if tags:
                    line += f"\n   标签: {tags}"
                if source:
                    line += f"  [{source}]"
                lines.append(line)
            else:
                lines.append(f"{i}. {f}")
        lines.append("")

    lines.extend([
        "## 检索概况（系统记录）",
        f"搜索深度: {data_level} | 共 {iterations} 轮",
    ])
    if tools_used:
        data_tools = [t for t in tools_used if t != "submit_facts_to_render"]
        if data_tools:
            lines.append(f"调用工具: {', '.join(data_tools)}")
    if empty_count >= 2:
        lines.append(f"⚠ 连续 {empty_count} 次搜索返回空结果——相关关键词可能不存在于数据库。")
    if missing:
        lines.append(f"数据缺失: {missing}")

    return "\n".join(lines)


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
        "_memory_context": None,
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
            result = await agent_app.ainvoke(
                initial_state, config={"recursion_limit": 50}
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

    # ── 后处理：v2 代码中转 + Render 风格改写 ──
    messages_for_render = result.get("messages", [])
    user_query = _extract_user_query_from_messages(messages_for_render) or request.message

    # v2: 从 submit_facts_to_render ToolMessage 提取结构化数据
    facts_json = _find_submit_facts(messages_for_render)
    if facts_json is not None:
        # 正常路径: Aggregator 通过 submit_facts 提交了数据
        render_input = _build_render_input(facts_json, result, user_query, depth)
        force_render = True  # submit_facts 路径绝不跳过 Render
    else:
        # Fallback: Aggregator 未调 submit_facts（chitchat / 错误）
        last_ai = _get_last_ai_message(messages_for_render)
        render_input = last_ai.content if last_ai and last_ai.content else "（无数据）"
        force_render = False
        logger.info("render fallback: Aggregator 未调 submit_facts，使用 AIMessage content")

    # 获取角色人格参数（snark/initiative 来自角色定义，不被 depth 覆盖）
    character = get_character(output_style)

    rendered = await render_reply(
        render_input=render_input,
        user_query=user_query,
        output_style=output_style,
        depth=depth,
        snark=character.snark,
        initiative=character.initiative,
        force=force_render,
    )
    if rendered:
        result["messages"] = _replace_last_ai_content(messages_for_render, rendered)
    elif facts_json is not None:
        # submit_facts 路径 + Render 失败 → 降级为确定性 Markdown
        # 绝不 fallback 到 Aggregator 的 AIMessage.content——那是内部草稿本
        result["messages"] = _replace_last_ai_content(messages_for_render, render_input)
        logger.warning("Render 失败 (submit_facts 路径)，降级为确定性 Markdown (%d chars)", len(render_input))

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

    按节点级别推送事件：reasoning → tool → render → done。
    Render 已从图节点降级为后处理，由 generate() 在 graph 完成后调用。

    Args:
        request: 包含用户消息、深度模式、会话 ID 和用户 ID 的请求体。

    Returns:
        StreamingResponse: SSE 事件流（text/event-stream）。
    """
    depth = request.depth
    session_id = request.session_id or uuid.uuid4().hex
    output_style = _resolve_output_style(request)

    # ── L1 Session 缓存：恢复前序消息 ──
    session_cache = get_session_cache()
    cached = await session_cache.load(session_id)

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

    async def generate():
        final_state: dict = dict(initial_state)
        try:
            async for event in agent_app.astream(initial_state, config={"recursion_limit": 50}):
                for node_name, node_output in event.items():
                    # 累积完整 state
                    for key, value in node_output.items():
                        if key == "messages" and key in final_state:
                            final_state["messages"].extend(value)
                        else:
                            final_state[key] = value

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

            # ── 后处理：v2 代码中转 + Render 风格改写 ──
            messages_for_render = final_state.get("messages", [])
            user_query = _extract_user_query_from_messages(messages_for_render) or request.message

            # v2: 从 submit_facts_to_render ToolMessage 提取结构化数据
            facts_json = _find_submit_facts(messages_for_render)
            if facts_json is not None:
                render_input = _build_render_input(facts_json, final_state, user_query, depth)
                force_render = True
            else:
                last_ai = _get_last_ai_message(messages_for_render)
                render_input = last_ai.content if last_ai and last_ai.content else "（无数据）"
                force_render = False

            character = get_character(output_style)
            rendered_reply = await render_reply(
                render_input=render_input,
                user_query=user_query,
                output_style=output_style,
                depth=depth,
                snark=character.snark,
                initiative=character.initiative,
                force=force_render,
            )
            if rendered_reply:
                final_state["messages"] = _replace_last_ai_content(
                    messages_for_render, rendered_reply
                )
            elif facts_json is not None:
                # submit_facts 路径 + Render 失败 → 降级为确定性 Markdown
                final_state["messages"] = _replace_last_ai_content(
                    messages_for_render, render_input
                )

            # ── 发送 render 事件 + 最终回复 ──
            yield f"data: {json.dumps({'node': 'render', 'reply': rendered_reply}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

            # ── L1 Session 缓存 + L2 记忆写入 ──
            max_cached = 30 if depth == "deep" else 20
            await session_cache.store(
                session_id,
                final_state.get("messages", []),
                max_messages=max_cached,
            )
            asyncio.create_task(_remember_session(final_state, request, depth))

        except Exception as e:
            logger.exception("/chat/stream: Agent 执行异常")
            yield f"data: {json.dumps({'node': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"
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

    async for event in agent_app.astream(
        initial_state, config={"recursion_limit": 50}
    ):
        now = time.monotonic()
        for node_name, node_output in event.items():
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
