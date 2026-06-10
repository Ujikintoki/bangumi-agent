"""
LangGraph Agent 节点函数

- reasoning_node: 接入真实 LLM（function-calling），集成意图分类器、消化态隔离
- critic_node: 双模式自省（规则/LLM），评估输出质量
"""

from __future__ import annotations

import logging

from langchain_core.messages import (AIMessage, HumanMessage, SystemMessage,
                                     ToolMessage)

from agent.classifier import classify_intent
from agent.guardrails import (
    TOOL_CALL_XML_RESIDUE,
    check_duplicate_tool_calls,
    is_terminal_response,
    strip_tool_call_xml,
)
from agent.llm import create_llm
from agent.memory import manage_memory
from agent.research.prompts import build_system_prompt
from agent.research.state import _MAX_ITERATIONS, AgentState
from core.config import get_settings
from tools.bgm_tools import get_agent_tools

logger = logging.getLogger("bgm-agent.nodes")

# 不绑定工具的意图（LLM 直接回复）
_NO_TOOL_INTENTS = frozenset({"chitchat", "factual"})


# ═══════════════════════════════════════════════════════════════════
# 推理节点
# ═══════════════════════════════════════════════════════════════════


async def reasoning_node(state: AgentState) -> dict:
    """推理节点：意图分类 + LLM function-calling 决策。

    流程：
        1. 兜底检查（error_flag）
        2. 记忆截断（manage_memory，含单条 ToolMessage 内容截断）
        3. 意图分类（仅第一轮执行）
        4. 构建 System prompt（含 intent 变体 + critic_feedback）
        5. 调用 LLM（全部使用 ainvoke，不阻塞 event loop）

    工具绑定策略：
        - chitchat / factual：不绑工具（节省 token）
        - lookup / discovery / realtime：始终绑定工具，模型自主判断何时
          停止调用。不再在每轮工具执行后强制消化——强制解绑是 XML 泄漏
          的根因。循环保护由 Critic 重复调用检测 + _MAX_ITERATIONS 熔断负责。

    ``_MAX_ITERATIONS`` 和 critic 熔断机制防止死循环。

    Args:
        state: 当前 Agent 全局状态。

    Returns:
        包含 messages、iterations、query_intent 等更新的字典。
    """
    # ── Step 0: 兜底模式 ────────────────────────────────────
    if state.get("error_flag", False):
        logger.warning("reasoning_node: error_flag=True，进入兜底模式")
        return {
            "messages": [AIMessage(content="抱歉，系统当前繁忙，请稍后再试。")],
            "iterations": state.get("iterations", 0),
            "query_intent": state.get("query_intent", "unknown"),
            "critic_feedback": "",
        }

    new_iterations = state.get("iterations", 0) + 1

    # ── Step 0.5: 记忆截断 ───────────────────────────────────
    # 在进入 LLM 推理前检查 Token 预算，超限时滑动窗口截断旧消息。
    # 工具返回数据量最不可控，因此在每轮 reasoning 开头检查。
    messages = state.get("messages", [])
    trimmed_messages = manage_memory(messages)

    # ── Step 1: 意图分类（仅第一轮） ─────────────────────────
    query_intent = state.get("query_intent", "unknown")
    intent_method = "cached"

    # 仅首轮推理时执行意图分类；后续轮次（如 tool 后的消化步）
    # 复用首轮结果，避免 LLM 非确定性导致同一查询被反复重分类为不同意图。
    if state.get("iterations", 0) == 0:
        # 提取用户原始输入
        user_input = _extract_user_input(state)
        if user_input:
            # 使用轻量 LLM 做 fallback 分类（temperature=0, max_tokens=10）
            classifier_llm = create_llm(temperature=0, max_tokens=10, request_timeout=10)
            query_intent, intent_method = await classify_intent(user_input, classifier_llm)
            logger.info(
                "[Intent] query='%s' → intent=%s (method=%s)",
                user_input[:80],
                query_intent,
                intent_method,
            )
        else:
            query_intent = "unknown"
            intent_method = "rule(empty)"

    # ── Step 2: 构建 System Prompt ───────────────────────────
    critic_feedback = state.get("critic_feedback", "")
    system_content = build_system_prompt(
        intent=query_intent,
        critic_feedback=critic_feedback,
    )

    # ── Step 3: 构建消息列表 ─────────────────────────────────
    messages_for_llm = [SystemMessage(content=system_content)]

    # 追加历史消息（使用截断后的消息，跳过原有的 SystemMessage）
    skipped_system = 0
    for m in trimmed_messages:
        if isinstance(m, SystemMessage):
            skipped_system += 1
            continue  # 用新的 SystemMessage 替换
        messages_for_llm.append(m)
    if skipped_system > 0:
        logger.debug("跳过 %d 条旧 SystemMessage，使用新的 SystemPrompt", skipped_system)

    # ── 消息状态日志（每次 reasoning 入口，DEBUG 级别） ────
    _log_message_state(messages_for_llm, new_iterations)

    # ── Step 4: LLM 调用 ─────────────────────────────────────
    llm = create_llm()

    # 消化态日志：记录当前是否在消化工具结果，方便排查多轮行为
    is_digesting = trimmed_messages and isinstance(trimmed_messages[-1], ToolMessage)
    if is_digesting:
        logger.debug("reasoning_node: 消化态 — 最后一条消息为 ToolMessage")

    # chitchat / factual 不绑定工具——节省 token，防止"你好"也调搜索
    # lookup / discovery / realtime 始终绑定工具——模型自主判断何时停止调用，
    # 而非每一轮工具执行后强制消化。强制消化是 XML 泄漏的根因（DeepSeek 等
    # function-calling 微调模型想继续调工具但通道被封 → 溢写到 .content）。
    # 循环保护由 Critic（重复调用检测）+ _MAX_ITERATIONS 熔断负责。
    if query_intent in _NO_TOOL_INTENTS:
        llm_to_use = llm
        logger.debug("reasoning_node: intent=%s → 不绑定工具", query_intent)
    else:
        tools = get_agent_tools()
        llm_to_use = llm.bind_tools(tools)
        if is_digesting:
            logger.debug(
                "reasoning_node: 消化态 → 仍然绑定 %d 个工具，模型自主判断是否需要后续调用",
                len(tools),
            )
        else:
            logger.debug(
                "reasoning_node: intent=%s → 绑定 %d 个工具", query_intent, len(tools)
            )

    # ── 消化态引导指令 ────────────────────────────────────
    # 工具结果回来后，引导模型优先综合数据输出文本回复，同时允许必要时
    # 继续调用工具（如 search → get_detail 的串行依赖）。
    if is_digesting:
        messages_for_llm.append(
            HumanMessage(
                content=(
                    "（系统指令：以上是工具返回的数据。请综合这些信息回答用户的问题。"
                    "如果当前数据足以回答，直接生成文字回复；"
                    "如果确实需要更多数据，可以继续调用必要的工具。）"
                )
            )
        )

    try:
        response: AIMessage = await llm_to_use.ainvoke(messages_for_llm)
    except Exception as e:
        logger.exception("reasoning_node: LLM 调用失败")
        # 保留 state 中已有的 critic_feedback：它来自上一轮 Critic 的评估，
        # 丢弃会让下一轮 REVISE 失去方向，浪费一个修正轮次。
        return {
            "messages": [AIMessage(content=f"抱歉，AI 服务暂时不可用：{e}")],
            "query_intent": query_intent,
            "iterations": new_iterations,
            "critic_feedback": state.get("critic_feedback", ""),
        }

    # ── 消化态 XML 泄漏安全网 ──────────────────────────────
    # 第二道防线：即使注入指令后模型仍然在 content 中输出 XML 工具调用，
    # 检测并剥离这些标签。防止脏数据进入路由器和 Critic。
    if is_digesting and response.content:
        cleaned, was_stripped = strip_tool_call_xml(response.content)
        if was_stripped:
            logger.warning(
                "reasoning_node: 消化态检测到泄露的工具调用 XML，已自动清理"
            )
            if not cleaned:
                # XML 是全部内容 → 替换为兜底回复
                cleaned = (
                    "抱歉，我无法正确处理工具返回的数据。"
                    "请尝试换个方式提问，或提供更具体的信息。"
                )
                logger.warning(
                    "reasoning_node: 消化态 XML 剥离后内容为空，使用兜底回复"
                )
            response = AIMessage(
                content=cleaned,
                response_metadata=getattr(response, "response_metadata", {}),
                id=getattr(response, "id", None),
            )

    # ── Step 5: 提取 tool_calls（仅用于日志） ──────────────
    last_tool_calls = (
        list(response.tool_calls)
        if hasattr(response, "tool_calls") and response.tool_calls
        else []
    )

    logger.info(
        "[Reasoning] intent=%s iterations=%d tool_calls=%s",
        query_intent,
        new_iterations,
        [tc.get("name", "?") for tc in last_tool_calls],
    )

    return {
        "messages": [response],
        "iterations": new_iterations,
        "query_intent": query_intent,
        "critic_feedback": "",  # 已消费
    }


# ═══════════════════════════════════════════════════════════════════


def _extract_user_input(state: AgentState) -> str:
    """从消息历史中提取用户原始输入。

    查找最后一条 HumanMessage，跳过 SystemMessage 和 AI 消息。
    用于意图分类器——只需要用户的原始问题，不需要对话上下文。

    Args:
        state: 当前 Agent 全局状态。

    Returns:
        用户原始输入文本。未找到时返回空字符串。
    """
    messages = state.get("messages", [])
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            return m.content if hasattr(m, "content") else str(m)
    return ""


# ═══════════════════════════════════════════════════════════════════
# 自省节点（Phase 3 Step 4：定向反馈）
# ═══════════════════════════════════════════════════════════════════


async def critic_node(state: AgentState) -> dict:
    """自省节点：评估 LLM 输出质量，输出定向反馈。

    支持双模式（通过 config.CRITIC_MODE 切换）：
    - ``"rule"``：零 Token 规则评估，检查工具利用和回复质量
    - ``"llm"``：LLM 三元维度评估 + 逃逸舱 + 定向反馈

    Args:
        state: 当前 Agent 全局状态。

    Returns:
        包含 ``critic_status``、``critic_feedback`` 和可能的
        ``error_flag`` 更新的字典。
    """
    settings = get_settings()
    if settings.CRITIC_MODE == "llm":
        return await _critic_node_llm(state)
    return _critic_node_rule(state)


# ═══════════════════════════════════════════════════════════════════
# 规则版 Critic（零 Token，默认）
# ═══════════════════════════════════════════════════════════════════


def _critic_node_rule(state: AgentState) -> dict:
    """规则版 Critic：快速结构化检查，零 Token 消耗。

    拓扑保证：critic_node 入口处 LLM 已消化工具结果并生成了回复。
    新拓扑下 tool → reasoning（消化）→ critic，critic 评估的永远是
    LLM 看到工具数据后的输出，而非纯 tool_call 消息。

    检查维度：
        1. 熔断防御：iterations >= _MAX_ITERATIONS → 强制 PASS
        2. 回复缺失：LLM 消化工具结果后未生成有效回复 → REVISE
        3. 逃逸舱：追问/澄清/诚实告知不存在 → PASS（语义终端识别）
        4. 回复过短：有工具数据但回复 < 20 字且非终端 → REVISE
        5. 首轮直接回复：无工具调用 → PASS（闲聊/常识）
    """
    iterations = state.get("iterations", 0)

    # ── 熔断防御 ──────────────────────────────────────────
    if iterations >= _MAX_ITERATIONS:
        logger.warning("critic(rule): iterations=%d 已达上限，强制 PASS", iterations)
        return {
            "critic_status": "PASS",
            "critic_feedback": "达到最大迭代次数，强制终止。",
            "error_flag": True,
        }

    messages = state.get("messages", [])
    # 精确定位当前轮次的 ToolMessages：找到最后一条带 tool_calls 的 AIMessage，
    # 其后出现的 ToolMessage 即为本轮工具调用。用 tool_calls 语义耦合替代
    # 脆弱的 [-2] 列表索引——重试/降级产生额外 AIMessage 时不会误判。
    _last_tc_idx = -1
    for i, m in enumerate(messages):
        if isinstance(m, AIMessage) and hasattr(m, "tool_calls") and m.tool_calls:
            _last_tc_idx = i
    has_tool_msgs = any(
        isinstance(m, ToolMessage) for m in messages[_last_tc_idx + 1:]
    ) if _last_tc_idx >= 0 else False

    # 找到最后一条有实质内容的 AI 回复（排除纯 tool_call 的 AIMessage）
    last_ai = _get_last_ai_response(messages)

    # ── 检查 0: 重复调用同一工具（参数相同） ──────────────────
    # 当 LLM 连续两轮调用相同工具且参数一致时，说明工具可能返回了错误
    # 或空结果，LLM 陷入无效重试。此时应强制切换到不同策略。
    _dup_feedback = check_duplicate_tool_calls(messages)
    if _dup_feedback:
        logger.info("critic(rule): 检测到重复工具调用 → REVISE")
        return {"critic_status": "REVISE", "critic_feedback": _dup_feedback}

    # ── 检查 0.5: XML 工具调用泄漏 ──────────────────────────
    # DeepSeek 等模型在消化态解绑工具后可能在 .content 中输出原始
    # <function_calls> XML。reasoning_node 有第一/二道防线（注入指令 +
    # 剥离），此处作为第三道防线确保无漏网之鱼。
    if last_ai and TOOL_CALL_XML_RESIDUE.search(last_ai.content):
        logger.warning("critic(rule): 检测到回复中包含工具调用 XML 残骸 → REVISE")
        return {
            "critic_status": "REVISE",
            "critic_feedback": (
                "回复中包含工具调用 XML 标签，应输出纯文本回复 | "
                "请基于工具数据直接生成自然语言回答，不要输出 XML 标签或 function_calls 标记 | "
                "格式错误"
            ),
        }

    # ── 检查 1: 工具已返回数据但没有 AI 回复 ──────────────
    if has_tool_msgs and last_ai is None:
        logger.debug("critic(rule): 工具已返回但 LLM 未生成回复 → REVISE")
        return {
            "critic_status": "REVISE",
            "critic_feedback": (
                "工具已返回数据但未生成有效回复 | "
                "请基于工具返回的内容组织自然语言回答 | "
                "回复缺失"
            ),
        }

    # ── 检查 1.5: 逃逸舱 — 追问/澄清/诚实告知不存在 ────
    # 当 LLM 向用户追问、诚实地告知数据不存在、或说明领域约束（如"角色没有评分"）
    # 时，即使回复较短也属于合法终端状态，不应被字数阈值误伤。
    if last_ai and is_terminal_response(last_ai.content):
        logger.debug("critic(rule): 终端回复（追问/澄清/诚实告知）→ PASS")
        return {
            "critic_status": "PASS",
            "critic_feedback": "回复为追问、澄清或诚实告知，属于合法终端状态。",
        }

    # ── 检查 2: 有工具数据但回复过短，可能未充分利用 ──────
    if has_tool_msgs and last_ai and len(last_ai.content) < 10:
        logger.debug("critic(rule): 回复过短 (%d 字) → REVISE", len(last_ai.content))
        return {
            "critic_status": "REVISE",
            "critic_feedback": (
                f"回复过短（仅 {len(last_ai.content)} 字），可能未充分利用工具数据 | "
                "请展开详细回答，包含名称、评分等具体信息 | "
                "不够具体"
            ),
        }

    # ── 检查 3: 第一轮且无工具调用 → 可能是闲聊 → PASS ───
    if iterations == 1 and not has_tool_msgs:
        logger.debug("critic(rule): 第一轮无工具调用 → PASS")
        return {
            "critic_status": "PASS",
            "critic_feedback": "直接回复，未使用工具——对于闲聊和常识问题这是合理的。",
        }

    # ── 默认：通过 ─────────────────────────────────────────
    logger.debug("critic(rule): iterations=%d → PASS", iterations)
    return {
        "critic_status": "PASS",
        "critic_feedback": "回复通过质量检查（规则评估）。",
    }


# ═══════════════════════════════════════════════════════════════════
# LLM 版 Critic（三元维度 + 逃逸舱 + 定向反馈）
# ═══════════════════════════════════════════════════════════════════

async def _critic_node_llm(state: AgentState) -> dict:
    """LLM 版 Critic：三元维度评估 + 逃逸舱 + 定向反馈。

    评估维度：完整性、具体性、工具利用。
    逃逸舱：助手已调工具并如实告知数据不存在 → 强制 PASS。
    """
    from agent.research.prompts import CRITIC_SYSTEM_PROMPT

    iterations = state.get("iterations", 0)

    # ── 熔断防御 ──────────────────────────────────────────
    if iterations >= _MAX_ITERATIONS:
        logger.warning("critic(llm): iterations=%d 已达上限，强制 PASS", iterations)
        return {
            "critic_status": "PASS",
            "critic_feedback": "达到最大迭代次数，强制终止。",
            "error_flag": True,
        }

    messages = state.get("messages", [])

    # 提取用户原始问题
    user_query = ""
    for m in messages:
        if isinstance(m, HumanMessage):
            user_query = m.content if hasattr(m, "content") else str(m)
            break

    # 提取最后一条有实质内容的 AI 回复
    last_ai = _get_last_ai_response(messages)
    if last_ai is None:
        return {
            "critic_status": "REVISE",
            "critic_feedback": ("未找到有效的 AI 回复 | 请生成自然语言回复 | 回复缺失"),
        }

    # ── LLM 评估 ──────────────────────────────────────────
    settings = get_settings()
    critic_model = settings.LLM_CRITIC_MODEL or settings.LLM_MODEL
    llm = create_llm(model=critic_model, temperature=0)

    eval_messages = [
        SystemMessage(content=CRITIC_SYSTEM_PROMPT),
        HumanMessage(
            content=f"""用户问题: {user_query}

助手回复: {last_ai.content}

请按三维度评估并给出结论："""
        ),
    ]

    try:
        response = await llm.ainvoke(eval_messages)
        verdict = (
            response.content.strip()
            if hasattr(response, "content")
            else str(response).strip()
        )
    except Exception as e:
        logger.warning("critic(llm): LLM 评估失败 (%s)，默认 PASS", e)
        return {
            "critic_status": "PASS",
            "critic_feedback": f"LLM 评估异常（{e}），默认通过。",
        }

    # ── 解析 verdict ───────────────────────────────────────
    verdict_upper = verdict.upper()
    if verdict_upper.startswith("PASS"):
        logger.debug("critic(llm): PASS — %s", verdict[:80])
        return {"critic_status": "PASS", "critic_feedback": verdict}
    elif verdict_upper.startswith("REVISE"):
        logger.info("critic(llm): REVISE — %s", verdict[:80])
        return {"critic_status": "REVISE", "critic_feedback": verdict}
    else:
        # 非预期输出 → 默认 PASS（安全侧）
        logger.warning("critic(llm): 非预期输出 '%s'，默认 PASS", verdict[:80])
        return {
            "critic_status": "PASS",
            "critic_feedback": "非预期评估输出，默认通过。",
        }


# ═══════════════════════════════════════════════════════════════════
# Critic 辅助函数
# ═══════════════════════════════════════════════════════════════════


def _log_message_state(messages: list, iteration: int) -> None:
    """记录消息列表结构（DEBUG 级别），便于排查状态机行为。

    对每条消息输出：类型、内容预览（前 200 字），ToolMessage 额外输出全量内容。
    """
    if not logger.isEnabledFor(logging.DEBUG):
        return

    logger.debug("── 消息状态 (iter=%d, 共 %d 条) ──", iteration, len(messages))
    for i, m in enumerate(messages):
        mtype = type(m).__name__
        content = m.content if hasattr(m, "content") else str(m)
        if isinstance(content, str):
            preview = content[:200].replace("\n", "\\n")
        else:
            preview = str(content)[:200]

        if isinstance(m, ToolMessage):
            # ToolMessage: 完整内容 + tool_call_id
            tc_id = getattr(m, "tool_call_id", "?")
            name = getattr(m, "name", "?")
            logger.debug(
                "  [%d] %s name=%s tc_id=%s content=%s",
                i, mtype, name, tc_id, content if isinstance(content, str) else str(content),
            )
        elif isinstance(m, AIMessage):
            tcs = getattr(m, "tool_calls", []) or []
            tc_names = [tc.get("name", "?") for tc in tcs]
            logger.debug("  [%d] %s tool_calls=%s preview=%s", i, mtype, tc_names, preview)
        else:
            logger.debug("  [%d] %s preview=%s", i, mtype, preview)


def _get_last_ai_response(messages: list) -> "AIMessage | None":
    """提取最后一条有实质内容的 AI 回复。

    返回任意有 content 的 AIMessage（含附带 tool_calls 的混合回复）。
    LLM 可能同时输出文字 + 工具调用（"我先介绍已知信息，同时帮你查最新数据"），
    这种情况应视为有效回复，不能被 critic 误判为"无回复"。
    """
    for m in reversed(messages):
        if isinstance(m, AIMessage) and m.content:
            return m
    return None





