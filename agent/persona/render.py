"""
Chat Render Node — 将 agent 回复改写为角色聊天风格

Phase 7: Render 始终运行（不再仅 tool calls 后触发）。
Agent 管策略，Render 管风格——职责彻底分离。

新增：
- 参数感知：snark/depth_taste/initiative 影响风格规则
- 快速跳过：无工具调用 + 回复 < 60 字 → 跳过渲染
- _RENDER_STYLE 按参数微调

设计参考：``docs/tmp/UserScriptAi.js`` —— 纯人格 prompt，~350 chars，
数据由上游整理好后传给 LLM，LLM 只做风格转换。
"""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent.llm import create_llm
from agent.persona.profiles import CharacterProfile, _render_tone, get_character

logger = logging.getLogger("bgm-agent.render")

# Display quote helpers — avoid syntax errors from mixing ASCII and curly quotes
_LQ = '“'  # "
_RQ = '”'  # "

# ── 说数据时的风格（参数感知）────────────────────────────────────────

_RENDER_STYLE_BASE = (
    "## 说数据时的风格\n"
    f"- 评分随口带过（{_LQ}也就8分出头{_RQ}、{_LQ}8.5说实话水了点{_RQ}），不要每条标⭐\n"
    "- 把数据融在吐槽里——结论先行，数据是佐证不是主体\n"
    f"- 结尾可以是你的判断、一个冷吐槽、或者说完就停。但不要用{_LQ}你还想查什么{_RQ}、"
    f"{_LQ}你觉得呢{_RQ}来收尾——你的话本身有分量，不需要递话筒\n"
)

# snark 微调
_RENDER_STYLE_SNARK_HIGH = (
    f"- 可以 diss 数据和用户观点（有论据地）——{_LQ}这分白瞎了{_RQ}比{_LQ}评分偏低{_RQ}有性格\n"
)
_RENDER_STYLE_SNARK_LOW = (
    "- 吐槽温和，点到为止。多表达共鸣，少直接批评\n"
)

# depth_taste 微调
_RENDER_STYLE_DEPTH_HIGH = (
    "- 可以自然地融入导演谱系、制作背景、跨媒介比较——有货就带一笔，不展开\n"
)
_RENDER_STYLE_DEPTH_LOW = (
    "- 用简单直接的语言。不要引用动画史、导演谱系或制作技法\n"
)

# initiative 微调
_RENDER_STYLE_INITIATIVE_HIGH = (
    "- 今天你愿意多聊。可以留话头、主动 offer 更多角度——但不要用反问来填充结尾\n"
)
_RENDER_STYLE_INITIATIVE_LOW = (
    "- 说完就停。你的话本身有分量，不需要用问句递话筒\n"
)

_RENDER_STYLE_NEUTRAL = (
    "## 说数据时的风格\n"
    "- 用数据支撑结论，不罗列数据\n"
    "- 如果信息不足，主动建议下一步"
)

# ── 按 depth 的字数限制 ──────────────────────────────────────────────────

_RENDER_WORD_LIMIT: dict[str, str] = {
    "quick": "120",
    "auto": "200",
    "deep": "350",
}

# ── 硬约束（bangumi 版含字数占位符 {word_limit}）────────────────────────

_RENDER_CONSTRAINTS_BANGUMI = (
    "## 硬约束\n"
    "0. 回复不超过 {word_limit} 字。\n"
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

# ── 快速跳过阈值 ─────────────────────────────────────────────────────

_SKIP_RENDER_MAX_CHARS = 60
"""无工具调用时，agent 回复短于此值的直接跳过 render。"""


# ═══════════════════════════════════════════════════════════════════════════
# Render Prompt Builder
# ═══════════════════════════════════════════════════════════════════════════


def build_render_prompt(
    character: CharacterProfile,
    user_query: str,
    agent_response: str,
    *,
    depth: str = "auto",
    snark: float | None = None,
    depth_taste: float | None = None,
    initiative: float | None = None,
) -> str:
    """构建 render prompt（参数感知）。

    Agent 已完成 dict→文本的解读，render 只做风格转换。

    Args:
        character: 当前角色人格。
        user_query: 用户原始问题。
        agent_response: Agent 的原始回复（待改写）。
        depth: 深度模式——控制字数上限（quick=120, auto=200, deep=350）。
        snark: 覆盖 character.snark。
        depth_taste: 覆盖 character.depth_taste。
        initiative: 覆盖 character.initiative。

    Returns:
        完整 render prompt 字符串。
    """
    style_key = character.key
    word_limit = _RENDER_WORD_LIMIT.get(depth, _RENDER_WORD_LIMIT["auto"])

    if style_key == "neutral":
        style_rules = _RENDER_STYLE_NEUTRAL
        constraints = _RENDER_CONSTRAINTS_NEUTRAL
    else:
        # ── 参数感知的风格微调 ──
        _s = snark if snark is not None else character.snark
        _d = depth_taste if depth_taste is not None else character.depth_taste
        _i = initiative if initiative is not None else character.initiative

        style_rules = _RENDER_STYLE_BASE
        if _s >= 0.65:
            style_rules += _RENDER_STYLE_SNARK_HIGH
        elif _s < 0.4:
            style_rules += _RENDER_STYLE_SNARK_LOW
        if _d >= 0.65:
            style_rules += _RENDER_STYLE_DEPTH_HIGH
        elif _d < 0.4:
            style_rules += _RENDER_STYLE_DEPTH_LOW
        if _i >= 0.65:
            style_rules += _RENDER_STYLE_INITIATIVE_HIGH
        elif _i < 0.4:
            style_rules += _RENDER_STYLE_INITIATIVE_LOW

        constraints = _RENDER_CONSTRAINTS_BANGUMI.format(word_limit=word_limit)

    parts: list[str] = [
        f"# {character.identity}",
        style_rules,
        constraints,
        f"## 用户问题\n{user_query}",
        f"## 原始回复\n{agent_response}",
    ]

    return "\n\n".join(parts)


# ═══════════════════════════════════════════════════════════════════════════
# 快速跳过判断
# ═══════════════════════════════════════════════════════════════════════════


def _should_skip_render(state: dict) -> bool:
    """判断是否可以跳过 render：无工具调用 + agent 回复足够短。

    短闲聊（如"那就别看烧脑的了"）已经够自然，不需要二次风格化。
    跳过避免了不必要的 LLM 调用和延迟。

    Args:
        state: 当前 AgentState。

    Returns:
        True 如果可以安全跳过 render。
    """
    messages: list = state.get("messages", [])

    # 检查当前轮是否有工具调用
    has_tools = False
    for m in reversed(messages):
        if isinstance(m, ToolMessage):
            has_tools = True
            break
        if isinstance(m, HumanMessage):
            # 到了当前轮的用户消息，停止回溯
            break

    if has_tools:
        return False  # 有工具调用 → 必须 render

    # 无工具调用 → 检查回复长度
    for m in reversed(messages):
        if isinstance(m, AIMessage) and m.content:
            return len(m.content) <= _SKIP_RENDER_MAX_CHARS

    return True  # 无 AI 回复也跳过


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

    Phase 7: 始终运行（不再仅 tool calls 后触发）。
    短闲聊自动跳过（_should_skip_render），避免不必要的 LLM 调用。

    失败时静默回退——返回空 dict，原始回复保持不变。

    Args:
        state: 当前 AgentState。

    Returns:
        包含渲染后 AIMessage 的字典，跳过时返回空 dict。
    """
    # ── 快速跳过 ────────────────────────────────────────────
    if _should_skip_render(state):
        logger.debug("render_node: 短闲聊无工具调用 → 跳过渲染")
        return {}

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

    # ── 参数感知 ────────────────────────────────────────────
    render_prompt = build_render_prompt(
        character,
        user_query,
        last_ai.content,
        depth=depth,
        snark=character.snark,
        depth_taste=character.depth_taste,
        initiative=character.initiative,
    )

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
