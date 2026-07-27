"""
Chat Render Node — 将 agent 回复改写为角色聊天风格

Phase 6.5: 解耦"查数据"和"聊天"。reasoning_node 专注准确+工具策略，
render_node 专注风格（损友吐槽/中性助手）。

设计参考：``docs/tmp/UserScriptAi.js`` —— 纯人格 prompt，~350 chars，
数据由上游整理好后传给 LLM，LLM 只做风格转换。
"""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage, HumanMessage

from agent.llm import create_llm
from agent.persona.profiles import CharacterProfile, get_character

logger = logging.getLogger("bgm-agent.render")

# Display quote helpers — avoid syntax errors from mixing ASCII and curly quotes
_LQ = '“'  # "
_RQ = '”'  # "

# ── 说数据时的风格（仅数据呈现——与 expression_guide 互补不重叠）──────────

_RENDER_STYLE_BANGUMI = (
    "## 说数据时的风格\n"
    f"- 评分随口带过（{_LQ}也就8分出头{_RQ}、{_LQ}8.5说实话水了点{_RQ}），不要每条标⭐\n"
    "- 把数据融在吐槽里——结论先行，数据是佐证不是主体\n"
    f"- 结尾可以是反问、可以是你的判断、可以是一个冷吐槽——但不要用{_LQ}你还想查什么{_RQ}"
)

_RENDER_STYLE_NEUTRAL = (
    "## 说数据时的风格\n"
    "- 用数据支撑结论，不罗列数据\n"
    "- 如果信息不足，主动建议下一步"
)

_RENDER_STYLE: dict[str, str] = {
    "bangumi": _RENDER_STYLE_BANGUMI,
    "neutral": _RENDER_STYLE_NEUTRAL,
}

# ── 按 depth 的字数限制 ──────────────────────────────────────────────────

_RENDER_WORD_LIMIT: dict[str, str] = {
    "quick": "120 字",
    "auto": "200 字",
    "deep": "350 字",
}

# ── 硬约束（bangumi 版含字数占位符 {word_limit}）────────────────────────

_RENDER_CONSTRAINTS_BANGUMI = (
    "## 硬约束\n"
    "0. 回复不超过 {word_limit}。\n"
    "1. 不用 emoji 与颜文字。\n"
    f"2. 不解释{_LQ}调用了什么工具{_RQ}或{_LQ}根据搜索结果{_RQ}——直接说结果。\n"
    "3. 禁止 Markdown 表格。用 `- ` 列表代替。\n"
    "4. 直接输出改写后的回复，不加任何前缀后缀。"
)

_RENDER_CONSTRAINTS_NEUTRAL = (
    "## 硬约束\n"
    "1. 不用 emoji。\n"
    f"2. 不解释{_LQ}调用了什么工具{_RQ}。\n"
    "3. 禁止 Markdown 表格。用 `- ` 列表代替。\n"
    "4. 直接输出改写后的回复，不加任何前缀后缀。"
)

RENDER_TEMPERATURE = 0.4


# ═══════════════════════════════════════════════════════════════════════════
# Render Prompt Builder
# ═══════════════════════════════════════════════════════════════════════════


def build_render_prompt(
    character: CharacterProfile,
    user_query: str,
    agent_response: str,
    *,
    depth: str = "auto",
) -> str:
    """构建极简 render prompt。

    Agent 已完成 dict→文本的解读，render 只做风格转换。

    Args:
        character: 当前角色人格。
        user_query: 用户原始问题。
        agent_response: Agent 的原始回复（待改写）。
        depth: 深度模式——控制字数上限（quick=120, auto=200, deep=350）。

    Returns:
        完整 render prompt 字符串。
    """
    style_key = character.key
    style_rules = _RENDER_STYLE.get(style_key, _RENDER_STYLE_NEUTRAL)
    word_limit = _RENDER_WORD_LIMIT.get(depth, _RENDER_WORD_LIMIT["auto"])

    if style_key == "bangumi":
        constraints = _RENDER_CONSTRAINTS_BANGUMI.format(word_limit=word_limit)
    else:
        constraints = _RENDER_CONSTRAINTS_NEUTRAL

    parts: list[str] = [
        f"# {character.identity}",
        style_rules,
        constraints,
        f"## 用户问题\n{user_query}",
        f"## 原始回复\n{agent_response}",
    ]

    return "\n\n".join(parts)


# ═══════════════════════════════════════════════════════════════════════════
# Render Node
# ═══════════════════════════════════════════════════════════════════════════


def _extract_user_query(messages: list) -> str:
    """从消息历史中提取最后一条真实用户消息。"""
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            content = m.content if hasattr(m, "content") else ""
            if content and not str(content).startswith("（系统指令："):
                return str(content)
    return ""


async def render_node(state: dict) -> dict:
    """渲染节点：将 agent 回复改写为角色聊天风格。

    失败时静默回退——返回空 dict，原始回复保持不变。
    """
    messages: list = state.get("messages", [])

    last_ai = None
    for m in reversed(messages):
        if isinstance(m, AIMessage) and m.content:
            last_ai = m
            break

    if not last_ai or not last_ai.content:
        logger.debug("render_node: 无有效 AI 回复，跳过渲染")
        return {}

    user_query = _extract_user_query(messages)
    if not user_query:
        logger.debug("render_node: 无法提取用户问题，跳过渲染")
        return {}

    output_style = state.get("output_style", "bangumi")
    depth = state.get("depth", "auto")
    character = get_character(output_style)
    render_prompt = build_render_prompt(character, user_query, last_ai.content, depth=depth)

    llm = create_llm(temperature=RENDER_TEMPERATURE, _telemetry_label="render")
    try:
        response = await llm.ainvoke([HumanMessage(content=render_prompt)])
        rendered = (
            response.content.strip()
            if hasattr(response, "content") and response.content
            else ""
        )
    except Exception:
        logger.warning("render_node: LLM 调用失败，使用原始回复", exc_info=True)
        return {}

    if not rendered or len(rendered) < 5:
        logger.warning("render_node: 渲染结果过短 (%d chars)，使用原始回复", len(rendered))
        return {}

    logger.info(
        "render_node: 渲染完成（%d → %d chars）",
        len(last_ai.content), len(rendered),
    )
    return {"messages": [AIMessage(content=rendered)]}
