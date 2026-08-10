"""
[DEPRECATED Phase 10] 自省节点 — 已从图谱中移除

critic_node、_critic_node_rule、_critic_node_llm 及其辅助函数
保留在代码中以备未来恢复。当前纯 ReAct 拓扑不再路由到这些函数。
如需恢复 Critic，在 graph.py 中重新注册节点并添加条件边即可。
"""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agent.config import get_max_iterations
from agent.guardrails import TOOL_CALL_XML_RESIDUE, check_duplicate_tool_calls, is_terminal_response
from agent.llm import create_llm
from agent.state import AgentState
from core.config import get_settings

logger = logging.getLogger("bgm-agent.nodes")


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
    from agent.prompts.scene_hints_deep import CRITIC_SYSTEM_PROMPT

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
# Critic 辅助
# ═══════════════════════════════════════════════════════════════════


def _get_last_ai_response(messages: list) -> AIMessage | None:
    """提取最后一条有实质内容的 AI 回复。"""
    for m in reversed(messages):
        if isinstance(m, AIMessage) and m.content:
            return m
    return None
