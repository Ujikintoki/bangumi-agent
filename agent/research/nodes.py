"""
LangGraph Agent 节点函数

- reasoning_node: 接入真实 LLM（function-calling），集成意图分类器
- tool_node / critic_node: 保持占位?

State 安全约束:
- last_tool_calls 仅 reasoning_node 写入
- tool_node / critic_node 禁止返回 last_tool_calls
"""

from __future__ import annotations

import logging
import re

from langchain_core.messages import (AIMessage, HumanMessage, SystemMessage,
                                     ToolMessage)

from agent.classifier import classify_intent
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
            # 这里会不会有更好的处理方式？
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

    # 仅首轮推理时执行意图分类；后续轮次（如 tool 后的消化步）
    # 复用首轮结果，避免 LLM 非确定性导致同一查询被反复重分类为不同意图。
    if state.get("iterations", 0) == 0:
        # 提取用户原始输入
        user_input = _extract_user_input(state)
        if user_input:
            # maxtokens的限时是否可以调整？
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
        logger.debug(
            "reasoning_node: intent=%s → 绑定 %d 个工具", query_intent, len(tools)
        )

    try:
        response: AIMessage = llm_to_use.invoke(messages_for_llm)
    except Exception as e:
        logger.exception("reasoning_node: LLM 调用失败")
        # 保留 state 中已有的 critic_feedback：它来自上一轮 Critic 的评估，
        # 丢弃会让下一轮 REVISE 失去方向，浪费一个修正轮次。
        return {
            "messages": [AIMessage(content=f"抱歉，AI 服务暂时不可用：{e}")],
            "last_tool_calls": [],
            "query_intent": query_intent,
            "iterations": new_iterations,
            "critic_feedback": state.get("critic_feedback", ""),
        }

    # ── Step 5: 提取 tool_calls ──────────────────────────────
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
                tool_messages.append(
                    ToolMessage(content=str(result), tool_call_id=tc["id"])
                )
            except Exception as e:
                tool_messages.append(
                    ToolMessage(content=f"工具执行失败: {e}", tool_call_id=tc["id"])
                )
        else:
            tool_messages.append(
                ToolMessage(content=f"未知工具: {tc['name']}", tool_call_id=tc["id"])
            )

    return {"messages": tool_messages}


# ═══════════════════════════════════════════════════════════════════
# 自省节点（Phase 3 Step 4：定向反馈）
# ═══════════════════════════════════════════════════════════════════


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


# critic策略可能需要更具具体事使用情况修改
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

    # ── 检查 1.5: 逃逸舱 — 追问/澄清/诚实告知不存在 ────
    # 当 LLM 向用户追问、诚实地告知数据不存在、或说明领域约束（如"角色没有评分"）
    # 时，即使回复较短也属于合法终端状态，不应被字数阈值误伤。
    if last_ai and _is_terminal_response(last_ai.content):
        logger.debug("critic(rule): 终端回复（追问/澄清/诚实告知）→ PASS")
        return {
            "critic_status": "PASS",
            "critic_feedback": "回复为追问、澄清或诚实告知，属于合法终端状态。",
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

# 是否应该将LLM的critic作为默认？


def _critic_node_llm(state: AgentState) -> dict:
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
        response = llm.invoke(eval_messages)
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

# 最后一条是否可以作为critc的评估依据？


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


# ── 终端回复识别模式 ──────────────────────────────────────────
# 当 AI 回复匹配以下任一模式时，视为合法终端状态（追问、澄清、
# 诚实告知数据不存在、说明领域约束），即使字数较少也不应被
# Critic 判定为 REVISE。

_TERMINAL_RESPONSE_PATTERNS = [
    # 追问澄清
    r"您(是指|说的|想查|要找).{1,30}(吗|\?|？)",
    r"请问.{1,30}(吗|\?|？)",
    r"(需要|请).{1,20}(确认|指定|明确|说明)",
    # 诚实告知不存在
    r"(未|没有|无法)(找到|检索到|搜索到|匹配|收录|发现)",
    r"暂无.{1,20}(数据|信息|结果|记录|评分|评论)",
    r"(数据库|站内|系统|本地|Bangumi).{0,10}(不含|没有|不存在|未收录)",
    r"(暂无|没有|无)(收录|相关|匹配).{0,10}(条目|信息|数据)",
    # 建议用户下一步操作
    r"(建议|推荐|您可以|请尝试|不妨).{1,30}(搜索|查找|确认|尝试|访问)",
    # 角色/人物无评分说明
    r"(角色|人物|声优|真人).{0,5}(没有|无|不含|不提供).{0,5}(评分|rating)",
    r"(只有|仅有).{1,10}(条目|作品|subject).{1,10}(评分|rating)",
    # 多候选让用户选
    r"(可能|也许).{1,10}(是|指).{1,30}(还是|或者|哪一个)",
    r"以下.{1,20}(候选|可能|结果)",
]


def _is_terminal_response(content: str) -> bool:
    """判断 AI 回复是否为合法的终端状态。

    当 LLM 在执行以下操作时，说明它已经完成了"尽职"的部分，
    不需要 Critic 要求它继续搜索或展开：
    - 向用户追问以澄清意图
    - 诚实告知数据客观不存在
    - 建议用户换一种方式搜索
    - 说明 Bangumi 数据模型的边界（如角色没有评分）

    Args:
        content: AI 回复的文本内容。

    Returns:
        True 如果该回复应被视为合法终端状态。
    """
    return any(re.search(pattern, content) for pattern in _TERMINAL_RESPONSE_PATTERNS)
