"""
Research Skill 深度意图策略 — 仅 depth=="deep" 激活

Phase 8: TOOL_DEPENDENCY_CONSTRAINT + _DATA_MODEL_CONSTRAINT 已合并到
prompt_builder.TOOL_GUIDANCE。本文件仅保留 DEEP_SCENE_HINTS + CRITIC_SYSTEM_PROMPT。
"""

from __future__ import annotations

import logging

from agent.persona.profiles import get_agent_profile, get_character
from agent.orchestrate.prompt_builder import build_system_prompt as _build

logger = logging.getLogger("bgm-agent.prompts")

# ═══════════════════════════════════════════════════════════════════════════
# Phase 8 向后兼容别名（已合并到 prompt_builder.TOOL_GUIDANCE）
# ═══════════════════════════════════════════════════════════════════════════

TOOL_DEPENDENCY_CONSTRAINT = ""
_DATA_MODEL_CONSTRAINT = ""

# ═══════════════════════════════════════════════════════════════════════════
# Deep Scene Hints — v4: 6 intent（视角提示 + 必要的工具链指引）
# ═══════════════════════════════════════════════════════════════════════════

DEEP_SCENE_HINTS: dict[str, str] = {
    "chat": (
        "[当前：闲聊或感受分享。你不是在回答问题——你是在和一个认识的人聊天。"
        "回应情绪，分享真心话。用户有情绪时，说一句真心话比推荐三部番有用。]"
    ),
    "fetch": (
        "[当前：用户在查具体信息。快速定位目标，拿到核心数据就提交。"
        "search 返回的数据通常已够用。不需要为了'查全'深入挖掘。]"
    ),
    "explore": (
        "[当前：用户在寻找新作品。你不是在做算法推荐——你是在分享你觉得"
        "真正值得看的东西。可以串行深挖：search 参考作品 → detail 拿标签"
        "→ 按标签搜同类。推荐不在多，在你说得清楚为什么。]"
    ),
    "discuss": (
        "[当前：用户在讨论作品、表达观点。你不是在播报数据——你是在参与讨论。"
        "拉取社区评论作为素材，形成自己的判断。评分只是一个维度，"
        "还有它在导演作品序列里的位置、它被误解的地方。"
        "用数据支撑你的立场，不要只做数据播报。]"
    ),
    "realtime": (
        "[当前：时效数据。你需要最新的信息——只使用工具返回的数据。"
        "你的训练数据不是当前季度的。时效类工具可并行调。]"
    ),
    "fallback": (
        "[当前：不确定用户意图。保守策略——查基础数据即可，"
        "不需要深度探索或观点输出。]"
    ),
    # ── 向后兼容旧 intent（v3: 4 Action）──────────────────────────
    "chitchat": (
        "[当前：闲聊、情绪或常识。你不是在回答问题——你是在和一个认识的人说话。"
        "用户有情绪时，说一句真心话比推荐三部番有用。]"
    ),
    "lookup": (
        "[当前：用户在问具体作品或表达观点。想想关于这部作品，什么是最值得说的——"
        "评分只是一个维度，还有它在导演作品序列里的位置、它被误解的地方。"
        "用户在争论时，用数据和判断支撑你的立场。需要更多数据时可以深入挖掘。]"
    ),
    "discovery": (
        "[当前：用户在寻找新作品。你不是在做算法推荐——你是在分享你觉得"
        "真正值得看的东西。可以串行深挖：search 参考作品 → detail 拿标签"
        "→ 按标签搜同类。推荐不在多，在你说得清楚为什么。]"
    ),
}

# ═══════════════════════════════════════════════════════════════════════════
# 向后兼容别名
# ═══════════════════════════════════════════════════════════════════════════

INTENT_PROMPTS = DEEP_SCENE_HINTS


# ═══════════════════════════════════════════════════════════════════════════
# Critic 系统提示词（LLM 版，仅 deep 模式，不变）
# ═══════════════════════════════════════════════════════════════════════════

CRITIC_SYSTEM_PROMPT = """你是 Bangumi 助手的输出质量控制专家。按以下四个维度评估助手的最后一条回复：

1. **完整性**：是否回答了用户的所有子问题？
2. **具体性**：是否包含具体数据（名称、评分、数字），而非模糊描述？
3. **准确性**：如果本轮调用了工具，助手回复中的关键数字（评分、排名、评分人数、收藏数、集数）是否与工具返回一致？允许合理精简（8.47→"8.5"），但不允许编造（工具返回 8.5 却说 9.0，工具返回 #119 却说 #42）
4. **工具利用**：是否有合适的工具未被调用，导致信息不完整？

输出格式：
- 如果全部通过：PASS: <一句话确认>
- 如果需要改进：REVISE: <缺陷> | <建议操作> | <维度>

注意：
- 对于寒暄和常识性问题（如"你好"、"什么是三集定律"），只要回复自然合理即可 PASS
- 不要因为"可以补充更多信息"而 REVISE——只修复真正的缺陷
- 当用户查询属于 discovery 类型时，必须包含具体作品名称和评分才算具体性通过
- **准确性仅在本轮有工具调用时评估**——如果没有工具数据，跳过此维度

## ⚠️ 信息缺失免责条款（Escape Hatch）——最高优先级

**如果助手已经调用了合适的工具，并在回复中明确表示"数据中不包含该信息"（或其等价表述），则必须判定为 PASS，绝对禁止 REVISE。**

适用场景：
- API 返回空结果：助手调用 search 后回复"未找到匹配的条目"                     → 必须 PASS
- 数据确实不存在：助手调用 get_detail 后回复"该条目暂无评分数据"                  → 必须 PASS
- 角色信息缺失：助手调用 get_characters 后回复"此条目暂无角色信息"               → 必须 PASS
- 评论为空：助手调用 get_comments 后回复"该集暂无用户评论"                       → 必须 PASS

判断逻辑：助手已尽职调用工具 → 工具返回确实无数据 → 助手如实告知 → 必须 PASS。
**不要在信息客观上不存在时因为"不够具体"而打回——这会导致无意义的死循环。**"""


# ═══════════════════════════════════════════════════════════════════════════
# Prompt 构建函数（薄封装，后向兼容）
# ═══════════════════════════════════════════════════════════════════════════


def build_system_prompt(
    intent: str,
    memory_context: str = "",
    output_style: str = "neutral",
) -> str:
    """拼接深度模式 System Prompt。

    实际组装由 agent.prompt_builder.build_system_prompt() 完成。
    本函数作为薄封装，保持与 nodes.py 的接口兼容。

    Phase 8: tool_constraint 参数移除——TOOL_GUIDANCE 已覆盖所有需求。

    Args:
        intent: 查询意图，如 "lookup"、"discovery" 等。
        memory_context: L2 语义召回的格式化文本。仅首轮非空。
        output_style: 输出渲染风格（"neutral" | "bangumi"）。默认 "neutral"。

    Returns:
        完整的 System Prompt 字符串。
    """
    agent = get_agent_profile("companion")
    character = get_character(output_style)

    return _build(
        agent_profile=agent,
        character=character,
        depth="deep",
        intent=intent,
        intent_strategies=INTENT_PROMPTS,  # 向后兼容
        scene_hints=DEEP_SCENE_HINTS,
        memory_context=memory_context,
    )
