"""
查询意图分类器 — LLM 单阶段分类

设计：全部查询通过轻量 LLM prompt（temperature=0, max_tokens=10）分类，
无关键词表、无正则规则。分类结果仅用于选择 System Prompt 策略变体和路由决策，
不影响工具可用性（LLM 始终看到全部工具）。

==== 意图总览（8 个）====

| intent     | 说明 | strategy |
|------------|------|----------|
| discovery  | 推荐、探索、类似作品 | research/prompts.py INTENT_PROMPTS |
| realtime   | 时效数据：热门、排期 | research/prompts.py INTENT_PROMPTS |
| debate     | 争论、质疑、观点表达 | research/prompts.py INTENT_PROMPTS |
| emotional  | 情绪表达：开心/难过/无聊 | research/prompts.py INTENT_PROMPTS |
| lookup     | 精确查找：评分、角色、声优 | research/prompts.py INTENT_PROMPTS |
| factual    | 常识问答 | research/prompts.py INTENT_PROMPTS |
| chitchat   | 寒暄、感谢、纯社交 | research/prompts.py INTENT_PROMPTS |
| unknown    | 无法明确分类的兜底 | research/prompts.py INTENT_PROMPTS |

==== 新增/修改意图步骤 ====

1. 在 INTENT_CLASSIFIER_PROMPT 加分类描述（本文件）
2. 在 _VALID_INTENTS 加 intent key（本文件）
3. 在 INTENT_PROMPTS 加策略变体（agent/research/prompts.py）

==== 设计原则 ====

- LLM 单阶段：一次轻量 LLM 调用完成分类，一致性优于关键词+LLM 两阶段
- 分类失败安全：非预期输出 → fallback "unknown"（工具始终绑定，不影响功能）
- 短文本处理：单字/短作品名（"EVA"、"86"）由 LLM 根据上下文判断
"""

from __future__ import annotations

import logging
from typing import Optional

from langchain_openai import ChatOpenAI

logger = logging.getLogger("bgm-agent.classifier")

# ═══════════════════════════════════════════════════════════════════
# LLM 分类 Prompt
# ═══════════════════════════════════════════════════════════════════

INTENT_CLASSIFIER_PROMPT = """将用户消息分类为以下类别之一，只回复类别名称（一个单词）：

- chitchat: 纯寒暄、问候、感谢，不涉及任何 Bangumi 内容查询
- factual: 领域常识问题（"什么是三集定律"），不需要查询实时数据
- lookup: 精确查找特定条目、评分、声优、评论，追问上文已提及作品的详情（"具体说说"、"第一部讲什么"）。注意：单独出现的作品名（"进击的巨人"、"EVA"）也应归为 lookup——用户想了解该作品
- discovery: 模糊推荐、探索发现、"类似XX的番"、找新内容
- realtime: 询问当前热门、放送排期、最新动态等时效性信息
- debate: 用户想争论、质疑或表达强烈观点（"EVA被高估了"、"巨人的结尾真的很烂"）
- emotional: 用户有明显的情绪表达（"失恋了"、"太开心了"、"好无聊"、"心情不好"）
- unknown: 无法明确分类的输入（如纯标点、无意义字符串、无法判断意图的极短文本）

注意：
- 包含"第一/二/三部"、"最早"、"最后那个"、"具体说说"等追问信号的消息，归为 lookup
- 单字或短文本（如 "EVA"、"86"、"K"）可能是作品名缩写——如果能判断是作品名，归为 lookup；确实无法判断意图时归为 unknown
- 用户可能在同一句话中混合寒暄和数据查询（"你好，EVA评分怎么样？"），此时归为 lookup 或对应的数据查询意图，而非 chitchat

用户消息: {user_message}

类别:"""

# ═══════════════════════════════════════════════════════════════════
# 公开接口
# ═══════════════════════════════════════════════════════════════════

_VALID_INTENTS = frozenset(
    {"chitchat", "factual", "lookup", "discovery", "realtime", "debate", "emotional", "unknown"}
)


async def classify_intent_llm(user_message: str, llm: ChatOpenAI) -> str:
    """LLM 意图分类。

    用轻量 prompt 让 LLM 判断意图。temperature=0, max_tokens=10
    确保输出稳定且低成本。

    Args:
        user_message: 用户原始输入。
        llm: ChatOpenAI 实例（应已配置为低 temperature、低 max_tokens）。

    Returns:
        intent 字符串，非预期值时 fallback 为 "unknown"。
    """
    try:
        # 转义花括号：用户输入含 {name} 等字面量时，
        # str.format() 会把它们当成占位符抛出 KeyError
        safe_message = user_message.replace("{", "{{").replace("}", "}}")
        response = await llm.ainvoke(
            INTENT_CLASSIFIER_PROMPT.format(user_message=safe_message)
        )
        raw = (
            response.content.strip().lower()
            if hasattr(response, "content")
            else str(response).strip().lower()
        )
        # 提取第一个有效单词
        intent = raw.split()[0] if raw else "unknown"
        if intent not in _VALID_INTENTS:
            logger.warning(
                "classify_intent_llm: 非预期输出 '%s'，fallback 为 unknown", raw
            )
            return "unknown"
        return intent
    except Exception as e:
        logger.warning("classify_intent_llm: LLM 调用失败 (%s)，fallback 为 unknown", e)
        return "unknown"


async def classify_intent(
    user_message: str,
    llm: ChatOpenAI | None = None,
) -> tuple[str, str]:
    """LLM 单阶段意图分类。

    Args:
        user_message: 用户原始输入。
        llm: ChatOpenAI 实例。None 时直接返回 "unknown"（调用方应始终传入）。

    Returns:
        (intent, method) 元组：
        - intent: 分类结果
        - method: 始终为 "llm"
    """
    # 空消息
    if not user_message or not user_message.strip():
        return ("chitchat", "llm")

    # 无 LLM 实例 → 兜底
    if llm is None:
        return ("unknown", "llm")

    intent = await classify_intent_llm(user_message, llm)
    return (intent, "llm")
