"""
Bangumi Agent 节点函数 — v2 纯 ReAct

fast / deep 两种深度模式共享同一推理逻辑，差异在参数：
- fast: 5 轮上限、10000 tok、角色默认 depth_taste
- deep: 12 轮上限、16000 tok、depth_taste=0.90、无强制终止
"""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agent.llm import create_llm
from agent.memory.short_term import (
    DEFAULT_MAX_TOKENS,
    DEPTH_TOKEN_BUDGETS,
    manage_memory,
)
from agent.orchestrate.deep_strategies import CRITIC_SYSTEM_PROMPT, DEEP_SCENE_HINTS
from agent.orchestrate.guardrails import TOOL_CALL_XML_RESIDUE
from agent.orchestrate.helpers import (
    build_message_list,
    extract_user_input,
    guard_xml_leak,
    recall_memory_step,
)
from agent.orchestrate.prompt_builder import (
    TOOLS_BY_INTENT,
    build_aggregator_prompt,
    get_tool_choice,
)
from agent.orchestrate.strategies import COMPANION_INTENT_PROMPTS, COMPANION_SCENE_HINTS
from agent.state import AgentState, get_max_iterations
from core.config import get_settings
from tools.bgm_tools import get_agent_tools

logger = logging.getLogger("bgm-agent.nodes")


# ═══════════════════════════════════════════════════════════════════
# 分类节点（v4: intent routing gate）
# ═══════════════════════════════════════════════════════════════════


async def classify_node(state: AgentState) -> dict:
    """分类节点：LLM function calling 分类 → 置信度路由。独立 LLM 调用。

    输出写入 state 的 ``query_intent`` 和 ``classifier_confidence`` 字段，
    后续 reasoning_node 读取这些字段决定行为。

    Args:
        state: 当前 Agent 全局状态。

    Returns:
        包含 query_intent、classifier_confidence、iterations 的字典。
    """
    from agent.llm import create_classifier_llm
    from agent.orchestrate.classifier import classify_intent, route_by_classification

    user_input = extract_user_input(state)

    classifier_llm = create_classifier_llm()
    intent, confidence = await classify_intent(user_input, classifier_llm)

    # 置信度路由
    routed_intent = route_by_classification(intent, confidence)

    logger.info(
        "[Classify] query='%s' → intent=%s (raw=%s, conf=%.2f)",
        user_input[:80], routed_intent, intent, confidence,
    )

    return {
        "query_intent": routed_intent,
        "classifier_confidence": confidence,
        "iterations": 1,
        "_memory_context": None,
    }


# ═══════════════════════════════════════════════════════════════════
# 统一推理节点（ReAct 路径）— v4: 纯 Aggregator
# ═══════════════════════════════════════════════════════════════════


async def reasoning_node(state: AgentState) -> dict:
    """推理节点：意图分类 + LLM function-calling 决策。纯 ReAct。

    两种 depth 共享同一逻辑，差异在参数：
    - fast: 角色默认值 5轮上限 last_chance强制回复
    - deep: depth_taste=0.90 12轮上限 无last_chance

    流程：
        1. 意图分类（仅首轮）
        2. L2 记忆召回
        3. 构建 System Prompt（按 depth 传不同的 personality 参数和 scene hints）
        4. LLM 调用（始终绑定工具，除非 last_chance）
        5. XML 泄漏防护

    Args:
        state: 当前 Agent 全局状态。

    Returns:
        包含 messages、iterations、query_intent 等更新的字典。
    """
    depth = state.get("depth", "fast")
    query_intent = state.get("query_intent", "fallback")
    max_iterations = get_max_iterations(depth, query_intent)
    is_deep = depth == "deep"

    new_iterations = state.get("iterations", 0) + 1
    messages = state.get("messages", [])

    # ── Step 1: 记忆召回（仅首轮） ─────────────────────────
    if new_iterations == 1:
        if is_deep:
            memory_context = await recall_memory_step(
                state,
                max_tokens=get_settings().MEMORY_MAX_INJECT_TOKENS,
            )
        else:
            memory_context = await recall_memory_step(
                state,
                max_tokens=get_settings().MEMORY_DIALOGUE_MAX_INJECT_TOKENS,
                recall_threshold=get_settings().MEMORY_DIALOGUE_RECALL_THRESHOLD,
            )
    else:
        memory_context = state.get("_memory_context", "") or ""

    # ── Step 2: 构建 Aggregator System Prompt（首轮） ──────
    if new_iterations == 1:
        tone_kwargs = {"depth_taste": 0.90} if depth == "deep" else {}
        scene_hints = DEEP_SCENE_HINTS if is_deep else COMPANION_SCENE_HINTS
        system_content = build_aggregator_prompt(
            depth="deep" if is_deep else depth,
            intent=query_intent,
            scene_hints=scene_hints,
            memory_context=memory_context,
            **tone_kwargs,
        )
    else:
        system_content = None

    # ── Step 3: 构建消息列表 ───────────────────────────────
    messages_for_llm = build_message_list(messages, system_content)

    # ── Step 4: Dynamic Tool Binding + Forced Tool Choice ────
    tool_names = TOOLS_BY_INTENT.get(query_intent, TOOLS_BY_INTENT["fallback"])
    all_tools = get_agent_tools()
    intent_tools = [t for t in all_tools if t.name in tool_names]

    tool_choice = get_tool_choice(
        intent=query_intent,
        iterations=new_iterations,
        max_iterations=max_iterations,
    )

    llm = create_llm(
        _telemetry_label=f"reasoning#{new_iterations}",
        extra_body={"thinking": {"type": "disabled"}},
    )
    llm_to_use = llm.bind_tools(intent_tools, tool_choice=tool_choice)

    is_digesting = messages and isinstance(messages[-1], ToolMessage)
    logger.debug(
        "reasoning_node: intent=%s iter=%d/%d tools=%d tool_choice=%s%s",
        query_intent, new_iterations, max_iterations,
        len(intent_tools), _tc_label(tool_choice),
        " (digesting)" if is_digesting else "",
    )

    # ── 消化态软引导（唯一保留的 20% 部分） ────────────────
    if is_digesting:
        digest_hint = (
            "（系统指令：工具数据已返回。判断是否够回答用户问题："
            "够→submit_facts，不够→继续查。）"
        )
        messages_for_llm.append(HumanMessage(content=digest_hint))

    # ── L1 记忆截断 ─────────────────────────────────────────
    token_budget = DEPTH_TOKEN_BUDGETS.get(depth, DEFAULT_MAX_TOKENS)
    messages_for_llm = manage_memory(messages_for_llm, max_tokens=token_budget)

    # ── 消息状态日志 ────────────────────────────────────────
    _log_message_state(messages_for_llm, new_iterations)

    # ── LLM 调用 ────────────────────────────────────────────
    try:
        response: AIMessage = await llm_to_use.ainvoke(messages_for_llm)
    except Exception as e:
        logger.exception("reasoning_node: LLM 调用失败")
        fallback = (
            f"抱歉，AI 服务暂时不可用：{e}" if is_deep else f"啧，脑子短路了。{e}"
        )
        return {
            "messages": [AIMessage(content=fallback)],
            "query_intent": query_intent,
            "iterations": new_iterations,
            "_memory_context": memory_context or "",
        }

    # ── XML 泄漏防护 ────────────────────────────────────────
    fallback_text = (
        "抱歉，我无法正确处理工具返回的数据。请尝试换个方式提问，或提供更具体的信息。"
        if is_deep
        else "啧，脑子有点乱，你再说一遍？"
    )
    response = guard_xml_leak(
        response, is_digesting=is_digesting,
        fallback_text=fallback_text, log=logger,
    )

    # ── Step 5: 日志 ────────────────────────────────────────
    tool_calls = (
        list(response.tool_calls)
        if hasattr(response, "tool_calls") and response.tool_calls
        else []
    )
    logger.info(
        "[Reasoning] depth=%s intent=%s iter=%d/%d tool_calls=%s",
        depth, query_intent, new_iterations, max_iterations,
        [tc.get("name", "?") for tc in tool_calls],
    )

    return {
        "messages": [response],
        "iterations": new_iterations,
        "query_intent": query_intent,
        "_memory_context": memory_context or "",
    }


# 兼容别名
_extract_user_input = extract_user_input


# ═══════════════════════════════════════════════════════════════════
# [DEPRECATED Phase 10] 自省节点（已从图谱中移除）
#
# critic_node、_critic_node_rule、_critic_node_llm 及其辅助函数
# 保留在代码中以备未来恢复。当前纯 ReAct 拓扑不再路由到这些函数。
# 如需恢复 Critic，在 graph.py 中重新注册节点并添加条件边即可。
# ═══════════════════════════════════════════════════════════════════


async def critic_node(state: AgentState) -> dict:
    """[DEPRECATED Phase 10] 自省节点。已从图谱中移除，保留以备恢复。

    原功能：评估 LLM 输出质量，输出定向反馈。支持双模式（rule/llm）。
    """
    settings = get_settings()
    if settings.CRITIC_MODE == "llm":
        return await _critic_node_llm(state)
    return _critic_node_rule(state)


# ═══════════════════════════════════════════════════════════════════
# 规则版 Critic（零 Token，默认）
# ═══════════════════════════════════════════════════════════════════


def _critic_node_rule(state: AgentState) -> dict:
    """规则版 Critic：快速结构化检查，零 Token 消耗。"""
    max_iterations = get_max_iterations(state.get("depth", "deep"))
    iterations = state.get("iterations", 0)

    if iterations >= max_iterations:
        logger.warning("critic(rule): iterations=%d 已达上限，强制 PASS", iterations)
        return {
            "critic_status": "PASS",
            "critic_feedback": "达到最大迭代次数，强制终止。",
            "error_flag": True,
        }

    messages = state.get("messages", [])

    # 定位本轮 ToolMessages
    _last_tc_idx = -1
    for i, m in enumerate(messages):
        if isinstance(m, AIMessage) and hasattr(m, "tool_calls") and m.tool_calls:
            _last_tc_idx = i
    has_tool_msgs = (
        any(isinstance(m, ToolMessage) for m in messages[_last_tc_idx + 1 :])
        if _last_tc_idx >= 0
        else False
    )

    last_ai = _get_last_ai_response(messages)

    # 重复调用检测
    _dup_feedback = check_duplicate_tool_calls(messages)
    if _dup_feedback:
        logger.info("critic(rule): 检测到重复工具调用 → REVISE")
        return {"critic_status": "REVISE", "critic_feedback": _dup_feedback}

    # XML 泄漏检测
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

    # 工具返回但无回复
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

    # 逃逸舱
    if last_ai and is_terminal_response(last_ai.content):
        logger.debug("critic(rule): 终端回复 → PASS")
        return {
            "critic_status": "PASS",
            "critic_feedback": "回复为追问、澄清或诚实告知，属于合法终端状态。",
        }

    # 回复过短
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

    # 首轮无工具 → PASS
    if iterations == 1 and not has_tool_msgs:
        logger.debug("critic(rule): 第一轮无工具调用 → PASS")
        return {
            "critic_status": "PASS",
            "critic_feedback": "直接回复，未使用工具——对于闲聊和常识问题这是合理的。",
        }

    logger.debug("critic(rule): iterations=%d → PASS", iterations)
    return {
        "critic_status": "PASS",
        "critic_feedback": "回复通过质量检查（规则评估）。",
    }


# ═══════════════════════════════════════════════════════════════════
# LLM 版 Critic（四维度 + 逃逸舱 + 定向反馈）
# ═══════════════════════════════════════════════════════════════════

_MAX_TOOL_DATA_CHARS = 800


def _extract_tool_data_for_critic(messages: list) -> str:
    """从消息历史中提取本轮工具返回的结构化数据。"""
    from langchain_core.messages import ToolMessage as TM

    start_idx = 0
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], HumanMessage):
            start_idx = i
            break

    parts: list[str] = []
    total_chars = 0
    for m in messages[start_idx:]:
        if isinstance(m, TM):
            name = getattr(m, "name", "?") or "?"
            content = getattr(m, "content", "") or ""
            if not content:
                continue
            if len(content) > _MAX_TOOL_DATA_CHARS:
                content = content[:_MAX_TOOL_DATA_CHARS] + "…"
            parts.append(f"[{name}]\n{content}")
            total_chars += len(content)
            if total_chars > _MAX_TOOL_DATA_CHARS * 2:
                parts.append("…[后续工具数据已截断]")
                break

    return "\n\n".join(parts)


async def _critic_node_llm(state: AgentState) -> dict:
    """LLM 版 Critic：四维度评估 + 逃逸舱 + 定向反馈。"""
    max_iterations = get_max_iterations(state.get("depth", "deep"))
    iterations = state.get("iterations", 0)

    if iterations >= max_iterations:
        logger.warning("critic(llm): iterations=%d 已达上限，强制 PASS", iterations)
        return {
            "critic_status": "PASS",
            "critic_feedback": "达到最大迭代次数，强制终止。",
            "error_flag": True,
        }

    messages = state.get("messages", [])

    user_query = ""
    for m in messages:
        if isinstance(m, HumanMessage):
            user_query = m.content if hasattr(m, "content") else str(m)
            break

    last_ai = _get_last_ai_response(messages)
    if last_ai is None:
        return {
            "critic_status": "REVISE",
            "critic_feedback": "未找到有效的 AI 回复 | 请生成自然语言回复 | 回复缺失",
        }

    tool_data = _extract_tool_data_for_critic(messages)

    settings = get_settings()
    critic_model = settings.LLM_CRITIC_MODEL or settings.LLM_MODEL
    llm = create_llm(model=critic_model, temperature=0, _telemetry_label="critic")

    eval_context = f"""用户问题: {user_query}

助手回复: {last_ai.content}"""

    if tool_data:
        eval_context += f"""

本轮工具返回（用于校验助手回复中的数字是否准确）:
{tool_data}"""

    eval_context += "\n\n请按四维度评估并给出结论："

    eval_messages = [
        SystemMessage(content=CRITIC_SYSTEM_PROMPT),
        HumanMessage(content=eval_context),
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

    verdict_upper = verdict.upper()
    if verdict_upper.startswith("PASS"):
        logger.debug("critic(llm): PASS — %s", verdict[:80])
        return {"critic_status": "PASS", "critic_feedback": verdict}
    elif verdict_upper.startswith("REVISE"):
        logger.info("critic(llm): REVISE — %s", verdict[:80])
        return {"critic_status": "REVISE", "critic_feedback": verdict}
    else:
        logger.warning("critic(llm): 非预期输出 '%s'，默认 PASS", verdict[:80])
        return {
            "critic_status": "PASS",
            "critic_feedback": "非预期评估输出，默认通过。",
        }


# ═══════════════════════════════════════════════════════════════════
# Critic 辅助函数
# ═══════════════════════════════════════════════════════════════════


def _log_message_state(messages: list, iteration: int) -> None:
    """记录消息列表结构（DEBUG 级别）。"""
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
            tc_id = getattr(m, "tool_call_id", "?")
            name = getattr(m, "name", "?")
            logger.debug(
                "  [%d] %s name=%s tc_id=%s content=%s",
                i,
                mtype,
                name,
                tc_id,
                content if isinstance(content, str) else str(content),
            )
        elif isinstance(m, AIMessage):
            tcs = getattr(m, "tool_calls", []) or []
            tc_names = [tc.get("name", "?") for tc in tcs]
            logger.debug(
                "  [%d] %s tool_calls=%s preview=%s", i, mtype, tc_names, preview
            )
        else:
            logger.debug("  [%d] %s preview=%s", i, mtype, preview)


def _get_last_ai_response(messages: list) -> AIMessage | None:
    """提取最后一条有实质内容的 AI 回复。"""
    for m in reversed(messages):
        if isinstance(m, AIMessage) and m.content:
            return m
    return None


def _tc_label(tool_choice) -> str:
    """tool_choice → 可读标签。"""
    if isinstance(tool_choice, dict):
        fn = tool_choice.get("function", {}).get("name", "?")
        return f"force:{fn}"
    return str(tool_choice)


# ═══════════════════════════════════════════════════════════════════
# 空结果检测
# ═══════════════════════════════════════════════════════════════════


def _count_consecutive_empty_searches(messages: list) -> int:
    """检测最近连续多少次 ``search_bangumi_subject`` 返回空结果。

    从最近的 ToolMessage 往前数，检查 JSON content 中的
    ``"results": []``、``"total": 0`` 或 ``"_error"`` 信号。
    遇到 AIMessage（含 tool_calls）时说明开始新一轮 → 计数器重置。

    Args:
        messages: 消息历史列表。

    Returns:
        连续空结果次数。非 search 工具的 ToolMessage 不计数但也不打断。
    """
    count = 0
    for m in reversed(messages):
        if isinstance(m, AIMessage) and hasattr(m, "tool_calls") and m.tool_calls:
            # 新一轮搜索 → 重置
            break
        if isinstance(m, ToolMessage):
            name = getattr(m, "name", "") or ""
            if name != "search_bangumi_subject":
                continue
            content = getattr(m, "content", "") or ""
            # 检测空结果信号
            if not content:
                continue
            if (
                '"results":[]' in content.replace(" ", "")
                or '"total":0' in content.replace(" ", "")
                or '"_error"' in content
            ):
                count += 1
            else:
                # 非空结果 → 不是"连续"空
                break
    return count
