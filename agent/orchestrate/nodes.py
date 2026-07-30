"""
Companion Agent 节点函数 — 纯 ReAct 推理

Phase 6: 合并双 Agent 为单一 reasoning_node。
Phase 9: Critic 暂时屏蔽。三种 depth 共享同一推理逻辑，
仅通过参数（预算/迭代/personality/scene_hints）区分行为。

深度模式差异：
- quick:  深度感知低 (0.3)、被动回答 (0.2)、3 轮上限、6000 tok
- auto:   角色默认 (0.7/0.6)、5 轮上限、10000 tok
- deep:   深度感知高 (0.9)、主动展开 (0.8)、12 轮上限、16000 tok

critic_node 保留在 graph 中但当前不被路由到。
"""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agent.orchestrate.guardrails import (
    TOOL_CALL_XML_RESIDUE,
    check_duplicate_tool_calls,
    is_terminal_response,
)
from agent.llm import create_llm
from agent.memory.short_term import DEFAULT_MAX_TOKENS, DEPTH_TOKEN_BUDGETS, manage_memory
from agent.persona.profiles import get_agent_profile, get_character
from agent.orchestrate.prompt_builder import build_system_prompt
from agent.orchestrate.strategies import COMPANION_INTENT_PROMPTS, COMPANION_SCENE_HINTS
from agent.orchestrate.helpers import (
    build_message_list,
    classify_intent_step,
    extract_user_input,
    guard_xml_leak,
    recall_memory_step,
)
from agent.orchestrate.deep_strategies import (
    CRITIC_SYSTEM_PROMPT,
    DEEP_SCENE_HINTS,
    INTENT_PROMPTS as DEEP_INTENT_PROMPTS,
)
from agent.state import AgentState, get_max_iterations
from core.config import get_settings
from tools.bgm_tools import get_agent_tools

logger = logging.getLogger("bgm-agent.nodes")

# 最后一轮强制回复指令（非 deep 模式用）
_LAST_CHANCE_INSTRUCTION = """## ⚠️ 最后一轮——必须现在回复

你已经没有更多轮次了。**绝对禁止**调用任何工具。
基于已经获取的数据直接回复用户，不要追求"完整"。**回复必须精简——只写最核心的判断，不要展开分析。**

如果之前的工具调用没有获取到任何有效数据——不要编造评分、排名、具体数字。
用你的角色语气诚实表达没找到，并给出替代方向（换关键词、换话题）。
诚实比瞎编更让用户信任你。"""


# ═══════════════════════════════════════════════════════════════════
# 统一推理节点
# ═══════════════════════════════════════════════════════════════════


async def reasoning_node(state: AgentState) -> dict:
    """推理节点：意图分类 + LLM function-calling 决策。纯 ReAct。

    三种 depth 共享同一逻辑，差异在参数：
    - quick:  depth_taste=0.3 initiative=0.2 3轮上限 last_chance强制回复
    - auto:   角色默认值 5轮上限 last_chance强制回复
    - deep:   depth_taste=0.9 initiative=0.8 12轮上限 无last_chance

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
    depth = state.get("depth", "auto")
    max_iterations = get_max_iterations(depth)
    is_deep = depth == "deep"

    new_iterations = state.get("iterations", 0) + 1
    messages = state.get("messages", [])

    # ── Step 1: 意图分类（仅首轮） ─────────────────────────
    query_intent, intent_method, did_classify = await classify_intent_step(state)
    if did_classify:
        user_input = extract_user_input(state)
        logger.info(
            "[Intent] depth=%s query='%s' → intent=%s (method=%s)",
            depth, user_input[:80], query_intent, intent_method,
        )

    # ── Step 1.5: 记忆召回（depth 分支）──────────────────
    if is_deep:
        memory_context = await recall_memory_step(
            state, max_tokens=get_settings().MEMORY_MAX_INJECT_TOKENS,
        )
    else:
        memory_context = await recall_memory_step(
            state,
            max_tokens=get_settings().MEMORY_DIALOGUE_MAX_INJECT_TOKENS,
            recall_threshold=get_settings().MEMORY_DIALOGUE_RECALL_THRESHOLD,
        )

    # ── Step 2: 构建 System Prompt（depth 分支） ───────────
    output_style = state.get("output_style", "bangumi")
    character = get_character(output_style)
    agent_profile = get_agent_profile("companion")

    # Phase 9: 按 depth 拧 personality 旋钮（5 档离散）
    # quick → 简单直接 L2、问什么答什么 L1
    # auto  → 角色默认 L4/L3
    # deep  → 动画史视角 L5、话痨 L5
    # snark 不传，三个 depth 共用角色默认 snark=0.65 (L4: 标准很高)
    if depth == "quick":
        tone_kwargs = {"depth_taste": 0.35, "initiative": 0.15}
    elif depth == "deep":
        tone_kwargs = {"depth_taste": 0.90, "initiative": 0.85}
    else:
        tone_kwargs = {}  # auto: 使用角色默认值 (0.65, 0.70, 0.60)

    if is_deep:
        system_content = build_system_prompt(
            agent_profile=agent_profile,
            character=character,
            depth="deep",
            intent=query_intent,
            intent_strategies=DEEP_INTENT_PROMPTS,
            scene_hints=DEEP_SCENE_HINTS,
            memory_context=memory_context,
            **tone_kwargs,
        )
    else:
        system_content = build_system_prompt(
            agent_profile=agent_profile,
            character=character,
            depth=depth,
            intent=query_intent,
            intent_strategies=COMPANION_INTENT_PROMPTS,
            scene_hints=COMPANION_SCENE_HINTS,
            memory_context=memory_context,
            **tone_kwargs,
        )

    # ── Step 3: 构建消息列表 ───────────────────────────────
    messages_for_llm = build_message_list(messages, system_content)

    # ── Step 3.5: 最后一轮强制回复（仅非 deep 模式） ─────
    is_last_chance = (not is_deep) and (new_iterations >= max_iterations - 1)
    if is_last_chance:
        logger.info(
            "reasoning_node: 最后一轮 (depth=%s iter=%d/%d) → 注入强制回复指令",
            depth, new_iterations, max_iterations,
        )
        system_content += "\n\n" + _LAST_CHANCE_INSTRUCTION
        # 重新构建消息列表以包含更新后的 system_content
        messages_for_llm = build_message_list(messages, system_content)

    # ── Step 4: LLM 调用 ────────────────────────────────────
    llm = create_llm(_telemetry_label=f"reasoning#{state['iterations'] + 1}")

    is_digesting = messages and isinstance(messages[-1], ToolMessage)
    if is_digesting:
        logger.debug("reasoning_node: 消化态 — 最后一条消息为 ToolMessage")

    # 工具绑定：非 deep 最后一轮解绑，其余始终绑定
    if is_last_chance:
        llm_to_use = llm
        logger.info("reasoning_node: 最后一轮 → 强制解绑工具")
    else:
        tools = get_agent_tools()
        llm_to_use = llm.bind_tools(tools)
        logger.debug(
            "reasoning_node: depth=%s intent=%s → 绑定 %d 个工具%s",
            depth, query_intent, len(tools),
            " (消化态)" if is_digesting else "",
        )

    # ── 消化态引导指令 ────────────────────────────────────
    if is_digesting:
        if is_deep:
            digest_hint = (
                "（系统指令：工具数据已返回。精简回复——只写最核心的判断，不要展开。"
                "如果已拿到完整信息直接基于数据回答，不需要逐个搜索每部作品。"
                "数据确实不足时才继续调用工具。）"
            )
        else:
            digest_hint = (
                "（系统指令：工具数据已返回。精简回复——只写最核心的判断。"
                "数据充分则直接回复；不足可继续调用工具。）"
            )
        messages_for_llm.append(HumanMessage(content=digest_hint))

    # ── 重复工具调用检测 ───────────────────────────────────
    dup_feedback = check_duplicate_tool_calls(messages)
    if dup_feedback:
        logger.info("reasoning_node: 检测到重复工具调用 → 注入引导指令")
        messages_for_llm.append(
            HumanMessage(
                content=(
                    f"（系统指令：{dup_feedback}。"
                    "如果数据确实不存在，直接告诉用户并给出建议，不要继续搜索。）"
                )
            )
        )

    # ── Step 4.5: 记忆截断（Phase 8: 按 depth 选预算） ──────
    token_budget = DEPTH_TOKEN_BUDGETS.get(depth, DEFAULT_MAX_TOKENS)
    messages_for_llm = manage_memory(messages_for_llm, max_tokens=token_budget)

    # ── 消息状态日志 ────────────────────────────────────────
    _log_message_state(messages_for_llm, new_iterations)

    try:
        response: AIMessage = await llm_to_use.ainvoke(messages_for_llm)
    except Exception as e:
        logger.exception("reasoning_node: LLM 调用失败")
        fallback = (
            f"抱歉，AI 服务暂时不可用：{e}"
            if is_deep
            else f"啧，脑子短路了。{e}"
        )
        result: dict = {
            "messages": [AIMessage(content=fallback)],
            "query_intent": query_intent,
            "iterations": new_iterations,
            "_memory_context": memory_context,
        }
        return result

    # ── 终端回复逃逸舱（非 deep 模式） ────────────────────
    if (not is_deep) and is_digesting and response.content and is_terminal_response(response.content):
        logger.info("reasoning_node: 终端回复（逃逸舱）→ 强制结束")
        new_iterations = max_iterations  # 让路由函数熔断到 END

    # ── XML 泄漏防护 ────────────────────────────────────────
    fallback = (
        "抱歉，我无法正确处理工具返回的数据。请尝试换个方式提问，或提供更具体的信息。"
        if is_deep
        else "啧，脑子有点乱，你再说一遍？"
    )
    response = guard_xml_leak(
        response, is_digesting=is_digesting, fallback_text=fallback, log=logger,
    )

    # ── Step 5: 日志 ────────────────────────────────────────
    tool_calls = (
        list(response.tool_calls)
        if hasattr(response, "tool_calls") and response.tool_calls
        else []
    )
    logger.info(
        "[Reasoning] depth=%s intent=%s iterations=%d tool_calls=%s",
        depth, query_intent, new_iterations,
        [tc.get("name", "?") for tc in tool_calls],
    )

    result = {
        "messages": [response],
        "iterations": new_iterations,
        "query_intent": query_intent,
        "_memory_context": memory_context,
    }
    return result


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
        any(isinstance(m, ToolMessage) for m in messages[_last_tc_idx + 1:])
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
                i, mtype, name, tc_id,
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


def _get_last_ai_response(messages: list) -> "AIMessage | None":
    """提取最后一条有实质内容的 AI 回复。"""
    for m in reversed(messages):
        if isinstance(m, AIMessage) and m.content:
            return m
    return None
