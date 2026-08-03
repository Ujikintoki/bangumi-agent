"""
查询 Intent 分类器 — v4: Function Calling 结构化输出（7 intent）

v4 将 4 Action 扩展为 7 Intent，使用 function calling 替代单 token 输出：
- LLM 调用 bind_tools 后输出 structured intent + confidence
- 置信度路由：低置信度 → 安全回退；高置信度 → 直接使用
- 分类结果直接决定图路由：chat → 直通 Render, profile → 用户画像分析, 其余 → ReAct

设计：一次 LLM 调用（temperature=0, max_tokens=200），用 function schema 约束输出格式。

==== 历史教训 ====

v1: 正则分类器（213行关键词+正则 → LLM fallback），系统性失败：关键词劫持、歧义词、短文本不匹配
v2: LLM 单 token 分类（temperature=0, max_tokens=10），小马拉大车——极简输出决定下游所有行为
v3: 4 Action（chitchat/lookup/discovery/realtime），分类结果只选 scene hint，不控制代码路径
v4: 7 Intent function calling + 置信度路由，结构化输出 + 硬闸门
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from langchain_openai import ChatOpenAI

from langchain_core.messages import HumanMessage

logger = logging.getLogger("bgm-agent.classifier")

# ═══════════════════════════════════════════════════════════════════════════
# v4: 7 Intent — 判定边界
# ═══════════════════════════════════════════════════════════════════════════

CLASSIFIER_PROMPT = """你是 Bangumi 助手的查询路由器。分析用户输入，调用 classify_intent 输出分类。

意图判定：
- chat: 纯社交/感受/常识，不需要查站内数据。"你好""好累""EVA真好看（感叹）""什么是三集定律（常识）"
- fetch: 查单个确定实体的属性。裸标题默认=查信息。"EVA""EVA评分""杉田智和配过什么""进击的巨人讲什么"
- explore: 多实体/无确定目标的探索。"推荐治愈番""2024最佳动画""类似日常的作品""EVA和巨人哪个好"
- discuss: 观点驱动讨论，需要站内社区内容。"EVA被高估了""分析芙莉莲为什么火""以瓶子君口吻吐槽高达"
- profile: 查询某位 Bangumi 用户的看番品味、评分习惯、追番动态。通常包含 @用户名 或"分析XX的品味"等表述
- realtime: 时效信息查询。"今天星期几""这周新番""现在什么番最火""本季排期"
- fallback: 真的无法判断时才用

硬原则：
1. 识别到作品名/人物名 → 默认不是 chat（除非明显只是感叹）
2. chat 需要高把握，不确定时宁可走 fetch/fallback 给用户查数据
3. 裸短标题("EVA""86""K") → fetch，这是站内用户默认行为
4. "最近"看语境归类 explore，"今天/本周/当前在播" → realtime
5. "2024年"已过去 → explore，不是 realtime
6. @用户名 或"分析XX的看番品味/评分习惯" → profile"""

# ═══════════════════════════════════════════════════════════════════════════
# Function Calling Schema（2 字段）
# ═══════════════════════════════════════════════════════════════════════════

CLASSIFY_INTENT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "classify_intent",
        "description": "分析用户查询，输出意图分类和置信度",
        "parameters": {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "enum": [
                        "chat", "fetch", "explore", "discuss",
                        "profile", "realtime", "fallback",
                    ],
                    "description": "用户的查询意图",
                },
                "confidence": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "description": "置信度。>0.8=确定，0.5-0.8=基本确定，<0.5=猜测",
                },
            },
            "required": ["intent", "confidence"],
        },
    },
}

# ═══════════════════════════════════════════════════════════════════════════
# 公开接口
# ═══════════════════════════════════════════════════════════════════════════

_VALID_INTENTS = frozenset({
    "chat", "fetch", "explore", "discuss", "profile", "realtime", "fallback",
})

# 向后兼容：旧 Action 名映射到 v4 intent
_INTENT_ALIASES: dict[str, str] = {
    "chitchat": "chat",
    "lookup": "fetch",
    "discovery": "explore",
    "debate": "discuss",
    "emotional": "chat",
    "factual": "fetch",
    "unknown": "fallback",
}


def _resolve_alias(raw: str) -> str:
    """旧 intent 名 → v4 intent 名。"""
    return _INTENT_ALIASES.get(raw, raw)


async def classify_intent_llm(
    user_message: str,
    llm: ChatOpenAI,
) -> tuple[str, float]:
    """LLM Intent 分类。v4: function calling 结构化输出。

    用 function schema 让 LLM 输出结构化分类结果（intent + confidence）。
    temperature=0, max_tokens=200 确保输出稳定。

    Args:
        user_message: 用户原始输入。
        llm: ChatOpenAI 实例（应已配置 temperature=0, max_tokens=200）。

    Returns:
        ``(intent, confidence)`` 元组。失败时返回 ``("fallback", 0.0)``。
    """
    try:
        safe_message = user_message.replace("{", "{{").replace("}", "}}")
        llm_with_tool = llm.bind_tools([CLASSIFY_INTENT_SCHEMA])
        response = await llm_with_tool.ainvoke(
            [HumanMessage(content=CLASSIFIER_PROMPT + "\n\n用户消息: " + safe_message)]
        )

        # 解析 tool_calls
        if hasattr(response, "tool_calls") and response.tool_calls:
            tc = response.tool_calls[0]
            args = json.loads(tc["args"]) if isinstance(tc["args"], str) else tc["args"]
            raw_intent = str(args.get("intent", "fallback")).strip().lower()
            confidence = float(args.get("confidence", 0.0))

            # 别名解析 + 校验
            intent = _resolve_alias(raw_intent)
            if intent not in _VALID_INTENTS:
                logger.warning(
                    "classify_intent_llm: 非预期输出 '%s' → 'fallback'", raw_intent
                )
                return ("fallback", 0.0)

            # 钳制 confidence
            confidence = max(0.0, min(1.0, confidence))
            return (intent, confidence)

        # 无 tool_calls → fallback
        content = str(response.content).strip() if hasattr(response, "content") else ""
        logger.warning(
            "classify_intent_llm: 无 tool_calls, content='%s' → fallback", content[:80]
        )
        return ("fallback", 0.0)

    except Exception as e:
        logger.warning("classify_intent_llm: LLM 调用失败 (%s)，fallback", e)
        return ("fallback", 0.0)


async def classify_intent(
    user_message: str,
    llm: ChatOpenAI | None = None,
) -> tuple[str, float]:
    """Intent 分类入口。v4: function calling + 置信度。

    Args:
        user_message: 用户原始输入。
        llm: ChatOpenAI 实例。None 时返回 ``("fallback", 0.0)``。

    Returns:
        ``(intent, confidence)`` 元组。
    """
    if not user_message or not user_message.strip():
        return ("fallback", 0.0)

    if llm is None:
        return ("fallback", 0.0)

    return await classify_intent_llm(user_message, llm)


def route_by_classification(
    intent: str,
    confidence: float,
    has_entities: bool = False,
) -> str:
    """置信度路由：根据分类结果决定实际使用的 intent。

    策略：
    - ≥0.8: 直接使用分类结果
    - 0.5-0.8: 保守降级（chat→fetch/fallback, discuss→explore）
    - <0.5: 全部 fallback

    chat 是最严格的 intent——不确定时宁可查数据也不要剥夺用户的信息获取。

    Args:
        intent: LLM 分类的原始 intent。
        confidence: 置信度（0.0-1.0）。
        has_entities: 用户消息中是否检测到作品名/人物名/用户名。

    Returns:
        路由后的 intent 字符串。
    """
    if confidence >= 0.8:
        return intent

    if confidence >= 0.5:
        if intent == "chat":
            # chat 门槛最高 → 不确定时查数据
            return "fetch" if has_entities else "fallback"
        if intent == "discuss":
            # 降级到 explore —— 仍然提供数据，只是少拉评论
            return "explore"
        if intent == "profile":
            # profile 降级到 fetch —— 仍可查作品但失去画像维度
            return "fallback"
        return intent  # fetch/explore/realtime 中置信度仍可用

    # 低置信度 → 全部 fallback
    return "fallback"
