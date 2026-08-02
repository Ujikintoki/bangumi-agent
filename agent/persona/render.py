"""
Chat Render Node — v2 分离合成架构

Render 现在是真正的 Agent Persona。它消费两样东西：
1. 代码层确定性拼接的 render_input（结构化 Markdown：查询 + 数据 + 检索概况）
2. 完整的 Character Card + snark/initiative 人格参数

Render 不调工具，不访问数据库——只做风格转换。
"""

from __future__ import annotations

import logging

from langchain_core.messages import HumanMessage, SystemMessage

from agent.llm import create_llm
from agent.persona.profiles import (
    _INITIATIVE_LEVELS,
    _SNARK_LEVELS,
    _pick_level,
    get_character_card,
)

logger = logging.getLogger("bgm-agent.render")

# ── 通用风格规则 + 硬约束 ──────────────────────────────────────────

_STYLE_BASE = """\
## 说话风格
- 评分随口带过（"也就8分出头"），不要每条标⭐
- 结论先行——具体信息是佐证不是主体
- 不提及"数据清单"或"检索概况"的存在——就像你本来就认识这些作品
- 直接说人话。你不是在写报告，你是在聊天"""

_CONSTRAINTS = """\
## 硬约束
1. 回复不超过 {word_limit} 字。
2. 不用 emoji 与颜文字。不用 Markdown 表格。多用 `- ` 列表。
3. 禁止编造评分、排名、集数、收藏数等具体数字。不确定就诚实说没查到。
4. 直接输出改写后的回复，不加任何前缀后缀。"""

# ── Per-personality voice hints（保留兼容，v2 中 Character Card 是主力）──

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

# ── 参数感知的风格微调（5 档阈值）──

def _style_modifiers(snark: float, depth_taste: float, initiative: float) -> str:
    """按人格参数选择风格微调规则。每次只注入 0-3 条。"""
    rules: list[str] = []

    if snark >= 0.8:
        rules.append("- 可以 diss 数据和用户观点——要有论据，不是乱喷")
    elif snark < 0.4:
        rules.append("- 吐槽温和，多表达共鸣，少直接批评")

    if depth_taste >= 0.8:
        rules.append("- 可以自然地融入导演谱系、制作背景——有货就带一笔，不展开")
    elif depth_taste < 0.4:
        rules.append("- 用简单直接的语言。不引用动画史、导演谱系或制作技法")

    if initiative >= 0.8:
        rules.append("- 可以留话头、主动 offer 更多角度——但不要用反问填充结尾")
    elif initiative < 0.4:
        rules.append("- 说完就停。不要用'你还想查什么'、'你觉得呢'收尾")
    else:
        rules.append("- 结尾可以是判断或冷吐槽，说完就停。不要用反问填充")

    return "\n".join(rules)


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
# Render Prompt Builder — v2: 完整 Character Card + 代码生成的 render_input
# ═══════════════════════════════════════════════════════════════════════════


def build_render_prompt(
    character_key: str,
    user_query: str,
    render_input: str,
    *,
    depth: str = "auto",
    snark: float = 0.65,
    initiative: float = 0.60,
) -> str:
    """构建 Render System Prompt — v2 分离合成架构。

    Render 接收：
    - 完整 Character Card（530 字审美体系，从 _CHARACTER_CARDS 取）
    - snark/initiative 语气参数
    - 代码层确定性拼接的 render_input（查询 + 数据 + 检索概况）
    - 硬约束（字数限制等）

    Args:
        character_key: 人格 key（"bangumi" | "bangumi_cold" | "bangumi_cute" | "neutral"）。
        user_query: 用户原始问题。
        render_input: 代码层从 Aggregator 输出 + AgentState 拼接的 Markdown。
        depth: 深度模式——控制字数上限。
        snark: 毒舌度 0.0-1.0。
        initiative: 主动性 0.0-1.0。

    Returns:
        完整 Render System Prompt 字符串。
    """
    word_limit = _WORD_LIMIT.get(depth, _WORD_LIMIT["auto"])

    # ── Character Card（v2: 完整 530 字，从 reasoning 移过来）──
    card = get_character_card(character_key)
    if not card:
        card = _VOICE.get(character_key, _VOICE["neutral"])

    # ── 语气参数（v2: snark + initiative，来自 _SNARK_LEVELS / _INITIATIVE_LEVELS）──
    snark_tone = _pick_level(snark, _SNARK_LEVELS)
    initiative_tone = _pick_level(initiative, _INITIATIVE_LEVELS)

    parts: list[str] = [
        f"# 你是谁\n\n{card}",
        f"## 今天的语气\n{snark_tone}",
        f"## 回复节奏\n{initiative_tone}",
        _STYLE_BASE,
        f"## 用户问题\n{user_query}",
        f"## 系统数据\n请基于以下 <system_retrieved_facts> 标签中的数据来回复。不要提及数据标签的存在。\n\n<system_retrieved_facts>\n{render_input}\n</system_retrieved_facts>",
        _CONSTRAINTS.format(word_limit=word_limit),
    ]

    return "\n\n".join(parts)


# ═══════════════════════════════════════════════════════════════════════════
# Render 调用入口
# ═══════════════════════════════════════════════════════════════════════════


def _should_skip_render(render_input: str) -> bool:
    """判断是否跳过 render：render_input 极短时跳过。"""
    return len(render_input) <= _SKIP_RENDER_MAX_CHARS


def _extract_user_query(messages: list) -> str:
    """提取最后一条真实用户消息（跳过系统注入的 HumanMessage）。"""
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            content = m.content if hasattr(m, "content") else ""
            if content and not str(content).startswith("（系统指令："):
                return str(content)
    return ""


async def render_reply(
    render_input: str,
    user_query: str,
    output_style: str = "bangumi",
    depth: str = "auto",
    *,
    snark: float = 0.65,
    initiative: float = 0.60,
    force: bool = False,
) -> str | None:
    """对 Aggregator 的数据清单做人格化改写。v2 分离合成架构。

    Args:
        render_input: 代码层拼接的 Markdown（查询 + 数据清单 + 检索概况）。
        user_query: 用户原始问题。
        output_style: 人格 key。
        depth: 深度模式，控制字数上限。
        snark: 毒舌度 0.0-1.0。
        initiative: 主动性 0.0-1.0。
        force: 强制渲染，跳过长度检查。submit_facts 路径必须设为 True。

    Returns:
        渲染后的自然语言回复。跳过或失败时返回 ``None``，调用方使用原始输入。
    """
    # Step 1: 跳过判断（force=True 时强制渲染）
    if not force and _should_skip_render(render_input):
        logger.debug("render_reply: 输入过短 → 跳过渲染")
        return None

    # Step 2: 构建 Render Prompt（v2: 完整 Character Card 在此注入）
    render_prompt = build_render_prompt(
        character_key=output_style,
        user_query=user_query,
        render_input=render_input,
        depth=depth,
        snark=snark,
        initiative=initiative,
    )

    # Step 3: LLM 调用
    llm = create_llm(temperature=RENDER_TEMPERATURE, _telemetry_label="render")
    try:
        response = await llm.ainvoke([SystemMessage(content=render_prompt)])
        rendered = (
            response.content.strip()
            if hasattr(response, "content") and response.content
            else ""
        )
    except Exception:
        logger.warning("render_reply: LLM 调用失败，使用原始输入", exc_info=True)
        return None

    if not rendered or len(rendered) < 5:
        logger.warning("render_reply: 渲染结果过短 (%d chars)，使用原始输入", len(rendered))
        return None

    logger.info(
        "render_reply: %s 渲染完成（%d → %d chars）",
        output_style, len(render_input), len(rendered),
    )
    return rendered
