"""
LangGraph Agent 节点函数

Phase 3 Step 2 升级：
- reasoning_node: 接入真实 LLM（function-calling），集成意图分类器
- tool_node / critic_node: 保持占位（Step 3 / Step 4 升级）

State 安全约束:
- last_tool_calls 仅 reasoning_node 写入
- tool_node / critic_node 禁止返回 last_tool_calls
"""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agent.classifier import classify_intent
from agent.llm import create_llm
from agent.memory import manage_memory
from agent.prompts import build_system_prompt
from agent.state import AgentState
from core.config import get_settings
from tools.bgm_tools import get_agent_tools

logger = logging.getLogger("bgm-agent.nodes")

# 不绑定工具的意图（LLM 直接回复）
_NO_TOOL_INTENTS = frozenset({"chitchat", "factual"})


# ═══════════════════════════════════════════════════════════════════
# 推理节点
# ═══════════════════════════════════════════════════════════════════


def reasoning_node(state: AgentState) -> dict:
    """推理节点：意图分类 + LLM function-calling 决策。

    流程：
        1. 兜底检查（error_flag）
        2. 意图分类（仅第一轮执行）
        3. 构建 System prompt（含 intent 变体 + critic_feedback）
        4. 调用 LLM（chitchat/factual 不绑定工具）
        5. 返回 AIMessage + last_tool_calls + query_intent

    Args:
        state: 当前 Agent 全局状态。

    Returns:
        包含 messages、iterations、last_tool_calls、query_intent 等更新的字典。
    """
    # ── Step 0: 兜底模式 ────────────────────────────────────
    if state.get("error_flag", False):
        logger.warning("reasoning_node: error_flag=True，进入兜底模式")
        return {
            "messages": [AIMessage(content="抱歉，系统当前繁忙，请稍后再试。")],
            "last_tool_calls": [],
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

    if query_intent == "unknown" or state.get("iterations", 0) == 0:
        # 提取用户原始输入
        user_input = _extract_user_input(state)
        if user_input:
            # 使用轻量 LLM 做 fallback 分类（temperature=0, max_tokens=10）
            classifier_llm = create_llm(temperature=0, max_tokens=10)
            query_intent, intent_method = classify_intent(user_input, classifier_llm)
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
    for m in trimmed_messages:
        if isinstance(m, SystemMessage):
            continue  # 用新的 SystemMessage 替换
        messages_for_llm.append(m)

    # ── Step 4: LLM 调用 ─────────────────────────────────────
    llm = create_llm()

    # chitchat / factual 不绑定工具——节省 token，防止"你好"也调搜索
    if query_intent in _NO_TOOL_INTENTS:
        llm_to_use = llm
        logger.debug("reasoning_node: intent=%s → 不绑定工具", query_intent)
    else:
        tools = get_agent_tools()
        llm_to_use = llm.bind_tools(tools)
        logger.debug("reasoning_node: intent=%s → 绑定 %d 个工具", query_intent, len(tools))

    try:
        response: AIMessage = llm_to_use.invoke(messages_for_llm)
    except Exception as e:
        logger.exception("reasoning_node: LLM 调用失败")
        return {
            "messages": [AIMessage(content=f"抱歉，AI 服务暂时不可用：{e}")],
            "last_tool_calls": [],
            "query_intent": query_intent,
            "iterations": new_iterations,
            "critic_feedback": "",
        }

    # ── Step 5: 提取 tool_calls ──────────────────────────────
    last_tool_calls = (
        list(response.tool_calls) if hasattr(response, "tool_calls") and response.tool_calls else []
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
        "last_tool_calls": last_tool_calls,
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
# 工具执行节点（Step 3 起由 LangGraph ToolNode 接管）
# ═══════════════════════════════════════════════════════════════════
# 以下手动实现仅作参考，graph.py 已使用 langgraph.prebuilt.ToolNode。
# ToolNode 自动完成：读取 AIMessage.tool_calls → 并发 .ainvoke()
# → 返回 list[ToolMessage]。保留此函数用于理解 ReAct 循环机制。

def tool_node_manual_reference(state: AgentState) -> dict:
    """（参考实现）手动执行工具调用。

    实际 graph.py 使用 LangGraph 内置 ``ToolNode(get_agent_tools())``，
    它提供了并发执行、错误处理、重试等特性。
    """
    from langchain_core.messages import ToolMessage
    from tools.bgm_tools import get_agent_tools

    tools = get_agent_tools()
    tools_by_name = {t.name: t for t in tools}
    last_message = state["messages"][-1]
    tool_messages = []

    for tc in last_message.tool_calls:
        tool = tools_by_name.get(tc["name"])
        if tool:
            try:
                import asyncio
                result = asyncio.run(tool.ainvoke(tc["args"]))
                tool_messages.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))
            except Exception as e:
                tool_messages.append(ToolMessage(content=f"工具执行失败: {e}", tool_call_id=tc["id"]))
        else:
            tool_messages.append(ToolMessage(content=f"未知工具: {tc['name']}", tool_call_id=tc["id"]))

    return {"messages": tool_messages}


# ═══════════════════════════════════════════════════════════════════
# 自省节点（Phase 3 Step 4：定向反馈）
# ═══════════════════════════════════════════════════════════════════

_MAX_ITERATIONS = 5


def critic_node(state: AgentState) -> dict:
    """自省节点：评估 LLM 输出质量，输出定向反馈。

    支持双模式（通过 config.CRITIC_MODE 切换）：
    - ``"rule"``：零 Token 规则评估，检查工具利用和回复质量
    - ``"llm"``：LLM 三元维度评估 + 逃逸舱 + 定向反馈

    ⚠️ State 安全约束：不触碰 ``last_tool_calls``。

    Args:
        state: 当前 Agent 全局状态。

    Returns:
        包含 ``critic_status``、``critic_feedback`` 和可能的
        ``error_flag`` 更新的字典。
    """
    settings = get_settings()
    if settings.CRITIC_MODE == "llm":
        return _critic_node_llm(state)
    return _critic_node_rule(state)


# ═══════════════════════════════════════════════════════════════════
# 规则版 Critic（零 Token，默认）
# ═══════════════════════════════════════════════════════════════════


def _critic_node_rule(state: AgentState) -> dict:
    """规则版 Critic：快速结构化检查，零 Token 消耗。

    检查维度：
        1. 熔断防御：iterations >= 3 → 强制 PASS
        2. 回复缺失：工具返回了数据但 LLM 未生成有效回复 → REVISE
        3. 回复过短：有工具数据但回复 < 20 字 → REVISE
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
    has_tool_msgs = any(isinstance(m, ToolMessage) for m in messages)

    # 找到最后一条有实质内容的 AI 回复（排除纯 tool_call 的 AIMessage）
    last_ai = _get_last_ai_response(messages)

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

    # ── 检查 2: 有工具数据但回复过短，可能未充分利用 ──────
    if has_tool_msgs and last_ai and len(last_ai.content) < 20:
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


def _critic_node_llm(state: AgentState) -> dict:
    """LLM 版 Critic：三元维度评估 + 逃逸舱 + 定向反馈。

    评估维度：完整性、具体性、工具利用。
    逃逸舱：助手已调工具并如实告知数据不存在 → 强制 PASS。
    """
    from agent.prompts import CRITIC_SYSTEM_PROMPT

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
            "critic_feedback": (
                "未找到有效的 AI 回复 | 请生成自然语言回复 | 回复缺失"
            ),
        }

    # ── LLM 评估 ──────────────────────────────────────────
    settings = get_settings()
    critic_model = settings.LLM_CRITIC_MODEL or settings.LLM_MODEL
    llm = create_llm(model=critic_model, temperature=0)

    eval_messages = [
        SystemMessage(content=CRITIC_SYSTEM_PROMPT),
        HumanMessage(content=f"""用户问题: {user_query}

助手回复: {last_ai.content}

请按三维度评估并给出结论："""),
    ]

    try:
        response = llm.invoke(eval_messages)
        verdict = response.content.strip() if hasattr(response, "content") else str(response).strip()
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
        return {"critic_status": "PASS", "critic_feedback": "非预期评估输出，默认通过。"}


# ═══════════════════════════════════════════════════════════════════
# Critic 辅助函数
# ═══════════════════════════════════════════════════════════════════


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
