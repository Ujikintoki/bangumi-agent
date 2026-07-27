"""
Research Skill 深度意图策略 — 仅 depth=="deep" 激活

Phase 7: INTENT_PROMPTS 长段落改为 DEEP_SCENE_HINTS（~100 chars/条）。
TOOL_DEPENDENCY_CONSTRAINT + _DATA_MODEL_CONSTRAINT 保留（deep 模式必需）。
CRITIC_SYSTEM_PROMPT 不变。

保留 INTENT_PROMPTS 别名 + build_system_prompt() 薄封装向后兼容。
"""

from __future__ import annotations

import logging

from agent.persona.profiles import get_agent_profile, get_character
from agent.orchestrate.prompt_builder import build_system_prompt as _build

logger = logging.getLogger("bgm-agent.prompts")

# ═══════════════════════════════════════════════════════════════════════════
# 工具依赖约束（仅 deep 模式，不变）
# ═══════════════════════════════════════════════════════════════════════════

TOOL_DEPENDENCY_CONSTRAINT = """
## ⚠️ 工具依赖规则（必须遵守）

1. 以下工具需要 subject_id 参数，**必须先通过 search_bangumi_subject 获取**：
   - get_bangumi_subject_detail
   - get_subject_characters
   - get_subject_opinions
   - get_episode_comments

2. 以下工具需要 character_id / person_id 参数，**必须先通过 search_bangumi_subject 获取**：
   - get_character_detail（需要 character_id，先用 search(entity_type="character") 搜索）
   - get_person_detail（需要 person_id，先用 search(entity_type="person") 搜索）

3. **绝对不要**将这些工具与 search_bangumi_subject 在同一轮中并行调用。
   错误示例：同时调用 search(name="花泽香菜") + get_person_detail(person_id=???)
   正确做法：第一轮 search → 拿到 id → 第二轮 detail

4. 可以安全并行调用的组合：
   - search_local_bangumi + get_trending_subjects（互不依赖）
   - get_calendar + get_trending_subjects（时效数据，互不依赖）
   - 多个不同关键词的 search_bangumi_subject 同时进行
   - 多个不同 ID 的 get_character_detail 同时调用（互不依赖）"""


# ═══════════════════════════════════════════════════════════════════════════
# 数据模型约束（仅 deep 模式，不变）
# ═══════════════════════════════════════════════════════════════════════════

_DATA_MODEL_CONSTRAINT = """
## ⚠️ Bangumi 数据模型约束

- **只有"条目/作品"（subject）有评分（rating）和排名（rank）**
- **"角色"（character）和"声优/真人"（person）有收藏数（collects），没有评分**
- 如果用户询问可能是角色或声优的实体的"评分"，先判断实体类型：
  - 如果搜索结果显示是角色 → 查找其所属作品的评分，并告知用户"角色本身没有评分，其所属作品评分为 X"
  - 如果搜索结果显示是声优 → 查找其配音作品的评分
- 对于番组/游戏等条目（subject），评分字段为 ``rating.score``，排名字段为 ``rating.rank``"""


# ═══════════════════════════════════════════════════════════════════════════
# Deep Scene Hints — 比 companion 稍多工具链提示（Phase 7）
# ═══════════════════════════════════════════════════════════════════════════

DEEP_SCENE_HINTS: dict[str, str] = {
    "chitchat": "[场景：闲聊。直接回应，不调工具——除非用户混入了数据查询。]",
    "factual": "[场景：常识问答。基于知识回答。不确定时优先搜索而非猜测。]",
    "lookup": (
        "[场景：查数据。先 search 定位，search 结果够用就直接答。"
        "用户明确问了简介/评分分布/标签才调 detail。角色/人物用对应 entity_type。"
        "两次搜索无果 → 诚实告知。名称有歧义 → 追问。]"
    ),
    "discovery": (
        "[场景：推荐。串行流程：search 参考作品 → detail 拿标签 → search 同类。"
        "无参考作品时用 search_local_bangumi 语义搜索。"
        "推荐 3-5 部，每部一句话理由。结果少就诚实说明。]"
    ),
    "realtime": (
        "[场景：时效数据。时效类工具直接调、可并行。"
        "拿到结果后不要逐个展开每个条目——这不是年度盘点。"
        "列表最多 10 条，评分高的在前。]"
    ),
    "debate": (
        "[场景：争论。先亮立场，用数据佐证。"
        "搜索评分和评论来支撑论点——有数据背书的毒舌比空口争论有力。]"
    ),
    "emotional": (
        "[场景：陪伴。先共情，再推荐。工具是附属品——情感连接优先于数据完整性。"
        "用户情绪低落时暂缓毒舌。推荐要有温度，说'为什么适合现在的你'。]"
    ),
    "unknown": (
        "[场景：通用。自行判断是否需要工具。不确定时优先搜索而非猜测。]"
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
    critic_feedback: str = "",
    memory_context: str = "",
    output_style: str = "neutral",
) -> str:
    """拼接深度模式 System Prompt。

    实际组装由 agent.prompt_builder.build_system_prompt() 完成。
    本函数作为薄封装，保持与 nodes.py 的接口兼容。

    Phase 7: 新增 scene_hints 参数（DEEP_SCENE_HINTS），
    同时传 intent_strategies 以向后兼容旧 prompt_builder。

    Args:
        intent: 查询意图，如 "lookup"、"discovery" 等。
        critic_feedback: Critic 的定向反馈。空字符串表示无反馈。
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
        scene_hints=DEEP_SCENE_HINTS,  # Phase 7 新路径
        tool_constraint=TOOL_DEPENDENCY_CONSTRAINT + _DATA_MODEL_CONSTRAINT,
        memory_context=memory_context,
        critic_feedback=critic_feedback,
    )
