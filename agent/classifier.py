"""
查询意图分类器

两阶段分类设计：
    1. 规则层（优先级列表）：关键词 + 正则 → 覆盖 ~80% 常见查询，零延迟
    2. LLM fallback：轻量 prompt → 处理规则无法匹配的模糊边界

输出: query_intent ∈ {chitchat, factual, lookup, discovery, realtime, unknown}

关键设计：使用优先级列表（list[tuple]）而非无序字典，
复合意图（discovery, realtime）必须先于简单意图（lookup, factual）求值。
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from langchain_openai import ChatOpenAI

logger = logging.getLogger("bgm-agent.classifier")

# ═══════════════════════════════════════════════════════════════════
# 优先级规则列表
# ═══════════════════════════════════════════════════════════════════
# 顺序即优先级：排在前面的意图先匹配。
# 复合意图（discovery, realtime）排在前面，防止被简单意图的关键词"劫持"。
# 例如 "找类似XX的番" 应匹配 discovery 而非 lookup 的"找"。

INTENT_RULES: list[tuple[str, dict]] = [
    # ── 优先级 1: 复合意图 ──────────────────────────────
    (
        "discovery",
        {
            "keywords": [
                "类似", "推荐", "差不多", "还有哪些", "还有什么",
                "冷门", "小众", "神作", "评分最高", "最好看",
                "必看", "经典", "值得",
            ],
            "patterns": [
                r"(类似|推荐|像.{1,4}一样|还有什么|找.{1,4}番|求.{1,4}番|跟.{1,4}差不多|和.{1,4}类似|有哪些.{1,4}(番|动漫|作品))",
            ],
        },
    ),
    (
        "realtime",
        {
            "keywords": [
                "今天", "本周", "这周", "放送", "播出", "排期", "日历",
                "最近什么火", "最近流行", "热门", "趋势", "新番",
                "新番推荐", "本季", "这季度", "当季",
            ],
            "patterns": [
                r"(今天|本周|这周|这季度|本季|最近).*(放|播|火|流行|热门|排|新番)",
            ],
        },
    ),
    # ── 优先级 2: 简单意图 ──────────────────────────────
    (
        "lookup",
        {
            "keywords": [
                "搜索", "找", "查", "声优", "角色", "详情",
                "评价", "评论", "吐槽", "几集", "多少集",
                "评分", "排名", "信息",
            ],
            "patterns": [
                r"^(搜|找|查|帮我).*(评分|声优|角色|详情|评论|评价|多少|几集|信息|排名)",
            ],
        },
    ),
    (
        "factual",
        {
            "keywords": [
                "什么是", "什么叫", "定义", "解释", "三集定律", "作画崩坏",
                "是谁", "哪一年", "什么时候出的", "为什么叫",
                "原案", "企划",
            ],
            "patterns": [
                r"^(什么是|什么叫|谁是的|解释一下|为什么叫)",
            ],
        },
    ),
    # ── 优先级 3: 兜底 ──────────────────────────────────
    (
        "chitchat",
        {
            "keywords": ["你好", "谢谢", "再见", "嗨", "hello", "hi", "晚安", "早安", "早上好"],
            "patterns": [r"^(你好|谢谢|再见|嗨|hello|hi|晚安|早安|早上好)$"],
        },
    ),
]

# ═══════════════════════════════════════════════════════════════════
# LLM fallback prompt
# ═══════════════════════════════════════════════════════════════════

INTENT_CLASSIFIER_PROMPT = """将用户消息分类为以下类别之一，只回复类别名称（一个单词）：

- chitchat: 寒暄、问候、闲聊、感谢
- factual: 领域常识问题，不需要查询实时数据就能回答
- lookup: 精确查找特定条目、评分、声优、评论等具体信息
- discovery: 模糊推荐、探索发现、"类似XX的番"、找新内容
- realtime: 询问当前热门、放送排期、最新动态等时效性信息
- unknown: 无法明确分类

用户消息: {user_message}

类别:"""

# ═══════════════════════════════════════════════════════════════════
# 公开接口
# ═══════════════════════════════════════════════════════════════════

_VALID_INTENTS = frozenset({"chitchat", "factual", "lookup", "discovery", "realtime", "unknown"})


def classify_intent_rule(user_message: str) -> Optional[str]:
    """规则层分类：按优先级列表匹配关键词和正则。

    关键设计：
        1. 使用有序列表 list[tuple] 而非 dict，保证匹配顺序等于优先级顺序
        2. 复合意图（discovery, realtime）排前面，防止被简单意图的关键词劫持
        3. chitchat 排最后作为兜底——更具体的意图都不匹配时才命中
        4. 短消息（< 5 字）且无工具意图时，默认归为 chitchat

    Args:
        user_message: 用户原始输入。

    Returns:
        匹配到的 intent 字符串，或 None（需要 LLM fallback）。
    """
    msg = user_message.strip().lower()
    if not msg:
        return "chitchat"

    for intent, config in INTENT_RULES:
        # 关键词匹配
        for kw in config["keywords"]:
            if kw in msg:
                logger.debug("classify_intent_rule: keyword='%s' → %s", kw, intent)
                return intent
        # 正则匹配
        for pattern in config["patterns"]:
            if re.search(pattern, msg):
                logger.debug("classify_intent_rule: pattern='%s' → %s", pattern, intent)
                return intent

    # 短消息（< 5 字）且无明确工具意图 → chitchat
    if len(msg) < 5:
        return "chitchat"

    return None  # 需要 LLM fallback


def classify_intent_llm(user_message: str, llm: ChatOpenAI) -> str:
    """LLM fallback 分类。

    用轻量 prompt 让 LLM 判断意图。temperature=0, max_tokens=10
    确保输出稳定且低成本。

    Args:
        user_message: 用户原始输入。
        llm: ChatOpenAI 实例（应已配置为低 temperature）。

    Returns:
        intent 字符串，非预期值时 fallback 为 "unknown"。
    """
    try:
        response = llm.invoke(
            INTENT_CLASSIFIER_PROMPT.format(user_message=user_message)
        )
        raw = response.content.strip().lower() if hasattr(response, "content") else str(response).strip().lower()
        # 提取第一个有效单词
        intent = raw.split()[0] if raw else "unknown"
        if intent not in _VALID_INTENTS:
            logger.warning("classify_intent_llm: 非预期输出 '%s'，fallback 为 unknown", raw)
            return "unknown"
        return intent
    except Exception as e:
        logger.warning("classify_intent_llm: LLM 调用失败 (%s)，fallback 为 unknown", e)
        return "unknown"


def classify_intent(
    user_message: str,
    llm: ChatOpenAI | None = None,
) -> tuple[str, str]:
    """两阶段意图分类：规则优先，LLM 兜底。

    Args:
        user_message: 用户原始输入。
        llm: ChatOpenAI 实例（规则无法匹配时使用）。None 时跳过 LLM fallback，
            直接返回 "unknown"。

    Returns:
        (intent, method) 元组：
        - intent: 分类结果
        - method: "rule" | "llm" | "rule(short)" | "rule(empty)"
    """
    # 空消息
    if not user_message or not user_message.strip():
        return ("chitchat", "rule(empty)")

    # Stage 1: 规则匹配
    result = classify_intent_rule(user_message)
    if result is not None:
        method = "rule(short)" if len(user_message.strip()) < 5 else "rule"
        return (result, method)

    # Stage 2: LLM fallback
    if llm is not None:
        intent = classify_intent_llm(user_message, llm)
        return (intent, "llm")

    return ("unknown", "rule")
