"""
查询 Action 分类器 — LLM 单阶段分类（v3: 8→4）

v3 将 8 个 intent 精简为 4 个 Action：
- debate 合并进 lookup（工具调用层面无区别：都是搜→查详情→查评论）
- emotional/factual/unknown 合并进 chitchat（都不需要工具）
- 分类结果直接决定图路由：chitchat→直接回复, lookup/realtime→Fast Track, discovery→ReAct

设计：一次轻量 LLM 调用（temperature=0, max_tokens=10），无关键词表、无正则规则。

==== 历史教训（旧正则分类器，d01cc7f 删除）====

旧两阶段分类器（213行关键词+正则 → LLM fallback）在以下场景系统性失败：
- 关键词劫持："推荐治愈番"→discovery 在 emotional 前匹配了"推荐"
- 歧义词："今天好累"→"今天"匹配 realtime 规则
- 极短文本："EVA""86"→不匹配任何规则
- 维护成本：每新增一个 ACGN 社区用语需加规则
结论：LLM 单阶段分类在一致性和可维护性上优于规则+LLM 两阶段。
"""

from __future__ import annotations

import logging
from typing import Optional

from langchain_openai import ChatOpenAI

logger = logging.getLogger("bgm-agent.classifier")

# ═══════════════════════════════════════════════════════════════════
# LLM 分类 Prompt（v3: 4 Action）
# ═══════════════════════════════════════════════════════════════════

INTENT_CLASSIFIER_PROMPT = """将用户消息分类为以下类别之一，只回复类别名称（一个单词）：

- chitchat: 纯寒暄、问候、感谢、情绪表达（"好累"、"好无聊"）、常识问题（"什么是三集定律"）、无法明确分类的输入。总之：不涉及具体作品名或数据查询的，都归这里
- lookup: 精确查找特定条目、评分、声优、评论；争论或质疑某部作品（"EVA被高估了"）；追问上文作品的详情（"具体说说"、"第一部讲什么"）；单独出现的作品名（"EVA"、"进击的巨人"）。总之：用户提到了或指向了某个具体作品/人物
- discovery: 模糊推荐、探索发现、"类似XX的番"、"有什么好看的"、找新内容。总之：用户没指定具体作品，在寻求推荐或探索
- realtime: 询问当前热门、放送排期、最新动态、今季新番等时效性信息

注意：
- 包含"第一/二/三部"、"最早"、"最后那个"等追问信号 → lookup
- 单字或短文本作品名（"EVA"、"86"、"K"）→ lookup
- 情绪表达（"好累"、"烦死了"）→ chitchat，不是 discovery
- 混合查询（"你好，EVA评分怎么样？"）→ lookup
- 争论质疑（"EVA被高估了"、"巨人的结尾真的很烂"）→ lookup，不是 chitchat

用户消息: {user_message}

类别:"""

# ═══════════════════════════════════════════════════════════════════
# 公开接口
# ═══════════════════════════════════════════════════════════════════

_VALID_ACTIONS = frozenset({"chitchat", "lookup", "discovery", "realtime"})


async def classify_intent_llm(user_message: str, llm: ChatOpenAI) -> str:
    """LLM Action 分类。v3: 4 Action。

    用轻量 prompt 让 LLM 判断 Action。temperature=0, max_tokens=10
    确保输出稳定且低成本。

    Args:
        user_message: 用户原始输入。
        llm: ChatOpenAI 实例（应已配置为低 temperature、低 max_tokens）。

    Returns:
        action 字符串，非预期值时 fallback 为 "chitchat"。
    """
    try:
        safe_message = user_message.replace("{", "{{").replace("}", "}}")
        response = await llm.ainvoke(
            INTENT_CLASSIFIER_PROMPT.format(user_message=safe_message)
        )
        raw = (
            response.content.strip().lower()
            if hasattr(response, "content")
            else str(response).strip().lower()
        )
        action = raw.split()[0] if raw else "chitchat"
        if action not in _VALID_ACTIONS:
            logger.warning(
                "classify_intent_llm: 非预期输出 '%s'，fallback 为 chitchat", raw
            )
            return "chitchat"
        return action
    except Exception as e:
        logger.warning("classify_intent_llm: LLM 调用失败 (%s)，fallback 为 chitchat", e)
        return "chitchat"


async def classify_intent(
    user_message: str,
    llm: ChatOpenAI | None = None,
) -> tuple[str, str]:
    """LLM 单阶段 Action 分类。v3: 4 Action（chitchat/lookup/discovery/realtime）。

    Args:
        user_message: 用户原始输入。
        llm: ChatOpenAI 实例。None 时直接返回 "chitchat"。

    Returns:
        (action, method) 元组：
        - action: 分类结果（4 Action 之一）
        - method: 始终为 "llm"
    """
    if not user_message or not user_message.strip():
        return ("chitchat", "llm")

    if llm is None:
        return ("chitchat", "llm")

    action = await classify_intent_llm(user_message, llm)
    return (action, "llm")
