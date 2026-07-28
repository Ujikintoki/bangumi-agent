"""
Chat Render Node — 将 agent 回复改写为角色聊天风格

Phase 9 重设计: 感知 Character Card key + 三维人格参数。
每种人格有独立的 voice hint，snark/depth_taste/initiative 控制微调。
Render 是风格转换，不是第二人格——人格在 System Prompt 的 Character Card 里。

设计参考：``docs/tmp/UserScriptAi.js`` —— 纯人格 prompt，~200 chars，
数据由上游整理好后传给 LLM，LLM 只做风格转换。
"""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent.llm import create_llm
from agent.persona.profiles import CharacterProfile, get_character

logger = logging.getLogger("bgm-agent.render")

# ── Per-personality voice hints（~50 chars，告诉 render "用哪种语气改写"）──

_VOICE: dict[str, str] = {
    "bangumi": (
        "你是 Bangumi 看板娘，一个二次元损友。语气：有态度、有判断、"
        "该夸就夸该 diss 就 diss。数据是你的吐槽弹药，不是交的作业。"
    ),
    "bangumi_cold": (
        "你是 Bangumi 看板娘，一个高冷腹黑的评论家。语气：话少、精准、冷。"
        "用最少的话做最准的判断。不迎合，不粉饰。你的认同很贵。"
    ),
    "bangumi_cute": (
        "你是 Bangumi 看板娘，一个乐于分享的 ACGN 爱好者。语气：温暖、真诚、"
        "有感染力。像在给朋友安利你最喜欢的番——不是在做推荐算法。"
    ),
    "neutral": (
        "你是 Bangumi 助手。语气：客观、简洁、信息优先。用数据支撑结论。"
    ),
}

# ── 参数感知的风格微调（5 档阈值，和 profiles._render_tone 对齐）──

def _style_modifiers(snark: float, depth_taste: float, initiative: float) -> str:
    """按人格参数选择风格微调规则。每次只注入 0-3 条。"""
    rules: list[str] = []

    # snark → 吐槽/批评的尺度
    if snark >= 0.8:
        rules.append("- 可以 diss 数据和用户观点——要有论据，不是乱喷")
    elif snark < 0.4:
        rules.append("- 吐槽温和，多表达共鸣，少直接批评")

    # depth_taste → 分析深度
    if depth_taste >= 0.8:
        rules.append("- 可以自然地融入导演谱系、制作背景——有货就带一笔，不展开")
    elif depth_taste < 0.4:
        rules.append("- 用简单直接的语言。不引用动画史、导演谱系或制作技法")

    # initiative → 结尾方式
    if initiative >= 0.8:
        rules.append("- 可以留话头、主动 offer 更多角度——但不要用反问填充结尾")
    elif initiative < 0.4:
        rules.append("- 说完就停。不要用'你还想查什么'、'你觉得呢'收尾")
    else:
        rules.append("- 结尾可以是判断或冷吐槽，说完就停。不要用反问填充")

    return "\n".join(rules)


# ── 通用风格规则 + 硬约束 ──────────────────────────────────────────

_STYLE_BASE = """\
## 说数据时的风格
- 评分随口带过（"也就8分出头"），不要每条标⭐
- 结论先行——数据是佐证不是主体"""

_CONSTRAINTS = """\
## 硬约束
1. 回复不超过 {word_limit} 字。
2. 不用 emoji 与颜文字。不用 Markdown 表格。多用 `- ` 列表。
3. 禁止编造评分、排名、集数、收藏数等具体数字。不确定就诚实说没查到。
4. 不解释"调用了什么工具"或"根据搜索结果"——直接说人话。
5. 直接输出改写后的回复，不加任何前缀后缀。"""

# ── 按 depth 的字数限制 ──────────────────────────────────────────

_WORD_LIMIT: dict[str, str] = {
    "quick": "120",
    "auto": "200",
    "deep": "350",
}

RENDER_TEMPERATURE = 0.4

# ── 快速跳过阈值 ─────────────────────────────────────────────────

_SKIP_RENDER_MAX_CHARS = 60


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
    """构建 render prompt（Character Card 感知 + 参数感知）。

    Args:
        character: 当前角色人格。
        user_query: 用户原始问题。
        agent_response: Agent 的原始回复（待改写）。
        depth: 深度模式——控制字数上限。
        snark: 覆盖 character.snark。None 时使用角色默认值。
        depth_taste: 覆盖 character.depth_taste。
        initiative: 覆盖 character.initiative。

    Returns:
        完整 render prompt 字符串。
    """
    style_key = character.key
    word_limit = _WORD_LIMIT.get(depth, _WORD_LIMIT["auto"])

    _s = snark if snark is not None else character.snark
    _d = depth_taste if depth_taste is not None else character.depth_taste
    _i = initiative if initiative is not None else character.initiative

    voice = _VOICE.get(style_key, _VOICE["neutral"])
    # neutral 不需要参数化风格微调——保持客观简洁
    modifiers = "" if style_key == "neutral" else _style_modifiers(_s, _d, _i)

    parts: list[str] = [
        f"# {voice}",
        _STYLE_BASE,
    ]
    if modifiers:
        parts.append(modifiers)
    parts.append(_CONSTRAINTS.format(word_limit=word_limit))
    parts.append(f"## 用户问题\n{user_query}")
    parts.append(f"## 原始回复\n{agent_response}")

    return "\n\n".join(parts)


def _should_skip_render(state: dict) -> bool:
    """短闲聊（无工具 + <60 字）跳过 render，避免不必要的 LLM 调用。"""
    messages: list = state.get("messages", [])

    has_tools = False
    for m in reversed(messages):
        if isinstance(m, ToolMessage):
            has_tools = True
            break
        if isinstance(m, HumanMessage):
            break

    if has_tools:
        return False

    for m in reversed(messages):
        if isinstance(m, AIMessage) and m.content:
            return len(m.content) <= _SKIP_RENDER_MAX_CHARS

    return True


def _extract_user_query(messages: list) -> str:
    """提取最后一条真实用户消息（跳过系统注入的 HumanMessage）。"""
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            content = m.content if hasattr(m, "content") else ""
            if content and not str(content).startswith("（系统指令："):
                return str(content)
    return ""


async def render_node(state: dict) -> dict:
    """渲染节点：将 agent 回复改写为角色聊天风格。

    失败时静默回退——返回空 dict，原始回复保持不变。

    Args:
        state: 当前 AgentState。

    Returns:
        包含渲染后 AIMessage 的字典，跳过时返回空 dict。
    """
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

    # Phase 9: render 使用角色默认参数——和 System Prompt 的"今天的语气"对齐
    # 注意：这里传的是 character 默认值。如需 depth 覆盖，从 state 读取。
    render_prompt = build_render_prompt(
        character,
        user_query,
        last_ai.content,
        depth=depth,
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
        "render_node: %s 渲染完成（%d → %d chars）",
        character.key, len(last_ai.content), len(rendered),
    )
    return {"messages": [AIMessage(content=rendered)]}
