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

# ── 按 depth 的字数限制 ──────────────────────────────────────────

_WORD_LIMIT: dict[str, str] = {
    "fast": "200",
    "deep": "350",
}

# [PHASE5-A] 硬截断上限（字符数）。prompt 建议 + 硬截断双保险。
# 如果 LLM-judge 评测中截断导致回复质量下降，将此值调高或设为 None 禁用。
# grep: HARD_CUTOFF_MAX_CHARS
_HARD_CUTOFF_MAX_CHARS: dict[str, int | None] = {
    "fast": 280,   # prompt 建议 200，硬截断留 40% buffer
    "deep": 480,   # prompt 建议 350，硬截断留 37% buffer
}

RENDER_TEMPERATURE = 0.4

# ── 快速跳过阈值 ─────────────────────────────────────────────────

_SKIP_RENDER_MAX_CHARS = 60


# ── render 完全失败时的兜底话术 ──────────────────────────────────
# 唯一一条不经 LLM 的人格可见输出。触发条件见 main.py:_render_final_reply：
# render 返回 None，且该分支【没有本来就能给用户看的原文】可降级
# （chat 分支的 render_input 是给模型看的指令，原样吐出去就是泄漏内部提示词）。
#
# 不要用"系统开小差了"这类中性 IT 话术：它和三个角色都对不上，且会把
# "AI 没答上来"说成"系统故障"。写成角色自己走神/卡壳，既诚实又不跳出人设。
# 改这里等于改人格表达 —— 与 profiles.py 的 Character Card 一起看（CLAUDE.md 规则 3）。
_RENDER_FALLBACK_LINES: dict[str, str] = {
    "bangumi": "……啧，刚才那句我没接住。你再说一遍？",
    "bangumi_kawaii": "诶，我刚刚卡了一下——你再说一次好不好？",
    "neutral": "抱歉，我这边出了点问题，请再说一遍。",
}


def render_fallback_line(character) -> str:
    """render 失败且无原文可降级时的兜底话术（不经 LLM）。

    Args:
        character: CharacterProfile 对象，按其 key 选话术。

    Returns:
        该角色的兜底话术；未知 key 回落到 neutral 的那句。
    """
    return _RENDER_FALLBACK_LINES.get(
        getattr(character, "key", ""), _RENDER_FALLBACK_LINES["neutral"]
    )


# ═══════════════════════════════════════════════════════════════════════════
# Render Prompt Builder — v2: 完整 Character Card + 代码生成的 render_input
# ═══════════════════════════════════════════════════════════════════════════


def build_render_prompt(
    character,          # CharacterProfile 对象
    user_query: str,
    render_input: str,
    *,
    depth: str = "fast",
) -> str:
    """构建 Render System Prompt — v2 分离合成架构。

    Render 接收：
    - CharacterProfile 对象（Card + style_guide + snark + initiative 动态拼接为一段）
    - 硬约束（character.guardrails，按 depth 格式化 word_limit）
    - 代码层确定性拼接的 render_input（查询 + 数据 + 检索概况）

    Args:
        character: CharacterProfile 对象。
        user_query: 用户原始问题。
        render_input: 代码层从 Aggregator 输出 + AgentState 拼接的 Markdown。
        depth: 深度模式——控制字数上限。

    Returns:
        完整 Render System Prompt 字符串。
    """
    word_limit = _WORD_LIMIT.get(depth, _WORD_LIMIT["fast"])
    if character.word_limit_override:
        word_limit = character.word_limit_override.get(depth, word_limit)

    # ── §1 人格自述：Card + style_guide + snark + initiative 动态拼接为一段 ──
    card = get_character_card(character.key)
    if not card:
        card = "你是 Bangumi 助手。"

    snark_text = _pick_level(character.snark, _SNARK_LEVELS)
    initiative_text = _pick_level(character.initiative, _INITIATIVE_LEVELS)

    persona_block = f"{card}\n\n{character.style_guide}\n\n今天的状态：{snark_text} {initiative_text}"

    parts: list[str] = [
        f"# 你是谁 + 你怎么说话\n{persona_block}",
        f"## 必须遵守\n{character.guardrails.format(word_limit=word_limit)}",
        f"## 用户问题\n{user_query}",
        f"## 系统数据\n请基于以下 <system_retrieved_facts> 标签中的数据来回复。不要提及数据标签的存在。\n\n<system_retrieved_facts>\n{render_input}\n</system_retrieved_facts>",
        f"## 字数限制\n回复严格不超过 {word_limit} 字。超过会被系统强制截断。",
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
    character,          # CharacterProfile 对象
    depth: str = "fast",
    *,
    force: bool = False,
) -> str | None:
    """对 Aggregator 的数据清单做人格化改写。v2 分离合成架构。

    Args:
        render_input: 代码层拼接的 Markdown（查询 + 数据清单 + 检索概况）。
        user_query: 用户原始问题。
        character: CharacterProfile 对象，提供 Card + style_guide + snark + initiative + guardrails。
        depth: 深度模式，控制字数上限。
        force: 强制渲染，跳过长度检查。非 chat 路径必须设为 True。

    Returns:
        渲染后的自然语言回复。跳过或失败时返回 ``None``，调用方使用原始输入。
    """
    # Step 1: 跳过判断（force=True 时强制渲染）
    if not force and _should_skip_render(render_input):
        logger.debug("render_reply: 输入过短 → 跳过渲染")
        return None

    # Step 2: 构建 Render Prompt
    render_prompt = build_render_prompt(
        character=character,
        user_query=user_query,
        render_input=render_input,
        depth=depth,
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

    # [PHASE5-A] 硬截断：双保险——prompt 已建议字数上限，此处硬截
    max_chars = _HARD_CUTOFF_MAX_CHARS.get(depth) if depth in _HARD_CUTOFF_MAX_CHARS else _HARD_CUTOFF_MAX_CHARS.get("fast", 280)
    if max_chars and len(rendered) > max_chars:
        original_len = len(rendered)
        # 尝试在句号处截断，避免截在词中间
        cutoff = rendered.rfind("。", 0, max_chars)
        if cutoff > max_chars * 0.7:
            rendered = rendered[:cutoff + 1]
        else:
            # 无合适句号 → 尝试换行处截断（保护列表/多段输出）
            cutoff = rendered.rfind("\n", 0, max_chars)
            if cutoff > max_chars * 0.5:
                rendered = rendered[:cutoff]
            else:
                rendered = rendered[:max_chars]
        logger.warning(
            "render_reply: 硬截断 %s (%d → %d chars, limit=%d)",
            character.key, original_len, len(rendered), max_chars,
        )

    logger.info(
        "render_reply: %s 渲染完成（%d → %d chars）",
        character.key, len(render_input), len(rendered),
    )
    return rendered
