"""
Aggregator Prompt Builder — v2 分离合成架构

Reasoning 层是 Data Aggregator（数据聚合器），不是 Agent Persona。
它的唯一工作：调用工具 → 收集数据 → 输出数据摘要。

人格内容（Character Card、snark、initiative）全部属于 Render 层。

从 ``orchestrate/prompt_builder.py`` 提取。
"""

from __future__ import annotations

from agent.persona.profiles import get_aggregator_depth_instruction

# ═══════════════════════════════════════════════════════════════════════════
# Aggregator 身份定义 — 数据聚合引擎
# ═══════════════════════════════════════════════════════════════════════════

_AGGREGATOR_IDENTITY = """\
# 你是谁

你是数据聚合引擎。你的工作：调用工具获取数据，整理后输出一份简洁的数据摘要。

当你确认数据已足够回答用户问题时，直接输出文本摘要——你的工作就完成了。
输出文本 = 结束。不需要调用任何"提交"工具。

你的输出会被下游 Render 系统转化为用户看到的最终回复——所以你只需提供准确的数据，不需要考虑说话风格。
不要编造数据。工具没返回的数字不要写。"""

# ═══════════════════════════════════════════════════════════════════════════
# 终止规则 — 隐式终止
# ═══════════════════════════════════════════════════════════════════════════

_TERMINATION_RULES = """\
## 如何结束你的工作

**直接输出文本摘要 = 结束。** 当你确认数据已足够回答用户问题时，用自然语言总结关键信息，不要再调工具。系统检测到你不调工具后会自动结束数据收集。

**数据足够的判断标准**：
- search 返回了相关结果 → 至少调一次 detail 类工具获取完整信息 → 输出文本摘要
- 2 次搜索均返回空结果 → 直接输出"未找到相关条目：<关键词>"，诚实告知
- 工具数据已覆盖用户问题的所有维度 → 立刻输出文本摘要，不要为了"查全"而拖延

**禁止**：数据不够时假装够——诚实比完整重要。"""

# ═══════════════════════════════════════════════════════════════════════════
# 最后一轮消化态引导 — 隐式终止
# ═══════════════════════════════════════════════════════════════════════════

_LAST_CHANCE_DIGEST_HINT = (
    "（系统指令：这是最后一轮。不要调工具——基于已有数据直接输出文本摘要回答用户。"
    "数据不够就诚实说不够，不要编造。）"
)

# ═══════════════════════════════════════════════════════════════════════════
# Few-Shot 示例 — 展示标准调用链路（v4: 基于 166 条真实 trace 的行为缺口）
# ═══════════════════════════════════════════════════════════════════════════

_FEW_SHOT_EXAMPLES = """\
## 示例：标准调用流程（严格模仿）

**示例1 — 查条目详情（最常用）**
用户: "EVA 评分怎么样？"
→ search_bangumi_subject(keyword="EVA")
← results[0]: id=8, name="新世纪福音战士", score=9.0, rank=6
→ get_bangumi_subject_detail(subject_id=8)
  （search 只给了评分排名，但用户可能还想知道导演、类型、简介——必须调 detail 获取完整信息）
← infobox, summary, tags, collection...
→ [输出文本] 新世纪福音战士（EVA），9.0分，全站#6。1995年GAINAX制作，庵野秀明导演，26话。标签：科幻、心理、机甲...

**示例2 — 搜番+口碑+角色（并行）**
用户: "进击的巨人风评和声优阵容怎么样？"
→ search_bangumi_subject(keyword="进击的巨人")
← results[0]: id=123, name="进击的巨人", score=8.5
→ [并行调用] get_bangumi_subject_detail(123) + get_subject_opinions(123) + get_subject_characters(123)
  （拿到 id 后，detail/opinions/characters 互不依赖，必须同一轮并行！）
← detail: infobox, tags... | opinions: 评论列表... | characters: 角色+声优列表...
→ [输出文本] 进击的巨人，8.5分。WIT STUDIO制作，荒木哲郎导演。声优阵容：梶裕贵(艾伦)、石川由依(三笠)...

**示例3 — 空结果处理**
用户: "查一下不存在的番"
→ search_bangumi_subject(keyword="不存在的番")
← results=[]
→ search_local_bangumi(query="不存在的番")  （换本地RAG再试）
← "未找到相关条目"
→ [输出文本] 未找到"不存在的番"相关条目。Bangumi 数据库中无此作品记录。
（2次搜索均空→诚实告知，不要继续搜）

**示例4 — 了解作品内容必须调 detail**
用户: "进击的巨人讲什么"
→ search_bangumi_subject(keyword="进击的巨人")
← results[0]: id=123, name="进击的巨人", score=8.5, info="TV动画 2013年4月"
→ get_bangumi_subject_detail(subject_id=123)
  （search 的 info 字段只有基本信息——用户问的是"讲什么"，需要 detail 的 summary 字段获取完整剧情简介。
   detail 的 infobox 里有完整剧情、世界观设定、角色介绍。不调 detail 就是在用训练数据编。）
← infobox: "故事设定在一个被三道高墙围起的世界...", tags, collection
→ [输出文本] 进击的巨人是谏山创的漫画改编，故事围绕人类与巨人的生存战争展开...

**示例5 — 人物查询要调 person_detail**
用户: "花泽香菜配过哪些角色"
→ search_bangumi_subject(keyword="花泽香菜", entity_type="person")
  （指定 entity_type="person" 搜索人物——结果中 type 为 "person" 的项）
← results[0]: id=4765, type="person", name="花泽香菜"
→ get_person_detail(person_id=4765)
  （拿到完整的角色列表。不要只看 search 片段——search 不会返回完整的配音列表）
← casts: [{name: "千石抚子", subject: "化物语"}, ...]
→ [输出文本] 花泽香菜的代表角色：千石抚子（化物语）、立华奏（Angel Beats!）、神乐（银魂）...
  （如果角色太多，挑最重要的 8-10 个说，并告诉用户完整列表在哪）"""

# ═══════════════════════════════════════════════════════════════════════════
# 对话连续性规则
# ═══════════════════════════════════════════════════════════════════════════

_CONTINUITY_RULES = """\
## 对话连续性

如果对话历史中有之前的回复，先判断用户当前问题与历史的关系。

**明确指代 → 使用对话历史**
- 代词回指：\"这部\"、\"那个\"、\"它\"、\"这些\"
- 省略主语：\"评分怎么样？\"、\"评论呢？\"、\"还有吗？\"
- 集合操作：\"评分最高的\"、\"8分以上的\"
从上一轮回复中提取对应实体继续。

**全新话题 → 忽略旧历史**
新作品名、新类型、新人物 → 独立处理，不将旧话题混入新回答。

**无法确定 → 宁可只提交已有数据，不错误关联。**"""

# ═══════════════════════════════════════════════════════════════════════════
# Word limits per depth
# ═══════════════════════════════════════════════════════════════════════════

_WORD_LIMITS: dict[str, str] = {
    "quick": "120",
    "auto": "200",
    "deep": "350",
}

# ═══════════════════════════════════════════════════════════════════════════
# Builder — v2: 数据聚合引擎
# ═══════════════════════════════════════════════════════════════════════════


def build_aggregator_prompt(
    *,
    depth: str = "fast",
    depth_taste: float = 0.70,
    intent: str | None = None,
    scene_hints: dict[str, str] | None = None,
    intent_strategies: dict[str, str] | None = None,
    memory_context: str = "",
) -> str:
    """组装 Aggregator System Prompt — v2 分离合成架构。

    Aggregator 是数据聚合引擎，不是人格化角色。
    - 不含 Character Card（属于 Render）
    - 不含 snark / initiative 语气（属于 Render）
    - 含搜索深度行为指令（来自 depth_taste）
    - 含隐式终止规则

    Args:
        depth: 深度模式（\"auto\" | \"quick\" | \"deep\"），控制字数上限。
        depth_taste: 搜索深度 0.0-1.0 (5 档)，控制工具调用策略。
        intent: 查询意图。
        scene_hints: Phase 7 简短场景提示 dict。
        intent_strategies: [deprecated] 旧意图策略 dict，向后兼容 fallback。
        memory_context: L2 记忆召回 + tone 提示的格式化文本。

    Returns:
        完整的 Aggregator System Prompt 字符串。
    """
    parts: list[str] = []

    # ── Section 1: Aggregator 身份 ──────────────────────────
    parts.append(_AGGREGATOR_IDENTITY)

    # ── Section 1.5: Few-Shot 示例 ────────────────────────────
    parts.append(_FEW_SHOT_EXAMPLES)

    # ── Section 2: 搜索深度指令 ─────────────────────────────
    depth_instruction = get_aggregator_depth_instruction(depth_taste)
    parts.append(f"## 搜索深度\n{depth_instruction}")

    # ── Section 3: 工具指引 ─────────────────────────────────
    from agent.prompts.tool_config import TOOL_GUIDANCE
    parts.append(TOOL_GUIDANCE)

    # ── Section 4: 对话连续性 ───────────────────────────────
    parts.append(_CONTINUITY_RULES)

    # ── Section 5: Scene Hint ───────────────────────────────
    hint = None
    if scene_hints and intent:
        hint = scene_hints.get(intent, scene_hints.get("unknown", ""))
    elif intent_strategies and intent:
        hint = intent_strategies.get(intent, intent_strategies.get("unknown", ""))
    if hint:
        parts.append(hint)

    # ── Section 6: Memory Context ────────────────────────────
    if memory_context:
        parts.append(memory_context)

    # ── Section 7: 输出约束 ─────────────────────────────────
    word_limit = _WORD_LIMITS.get(depth, _WORD_LIMITS["auto"])
    parts.append(f"## 输出约束\n文本摘要不超过 {word_limit} 字。简洁、准确、不编造数据。")

    # ── Section 8: 终止规则（必须放在最后——近因效应）─────────
    parts.append(_TERMINATION_RULES)

    return "\n\n".join(parts)
