"""
查询意图分类器

两阶段分类设计：
    1. 规则层（优先级列表）：关键词 + 正则 → 覆盖 ~80% 常见查询，零延迟
    2. LLM fallback：轻量 prompt → 处理规则无法匹配的模糊边界

==== 意图总览（8 个）====

| intent     | 优先级 | 绑工具 | 说明 | strategy |
|------------|--------|--------|------|----------|
| discovery  | 1      | ✅     | 推荐、探索、类似作品 | research/prompts.py INTENT_PROMPTS |
| realtime   | 1      | ✅     | 时效数据：热门、排期 | research/prompts.py INTENT_PROMPTS |
| debate     | 2      | ❌     | 争论、质疑、观点表达 | research/prompts.py INTENT_PROMPTS |
| emotional  | 2      | ❌     | 情绪表达：开心/难过/无聊 | research/prompts.py INTENT_PROMPTS |
| lookup     | 3      | ✅     | 精确查找：评分、角色、声优 | research/prompts.py INTENT_PROMPTS |
| factual    | 4      | ❌     | 常识问答（不需要实时数据） | research/prompts.py INTENT_PROMPTS |
| chitchat   | 5      | ❌     | 寒暄、感谢、纯社交 | research/prompts.py INTENT_PROMPTS |
| unknown    | —      | ✅     | LLM fallback 兜底 | research/prompts.py INTENT_PROMPTS |

==== 新增/修改意图步骤 ====

1. 在 INTENT_RULES 加关键词/patterns（本文件，~第30行）
   → 关键词选型原则写在注释里
2. 在 _VALID_INTENTS 加 intent key（本文件，~第256行）
3. 在 INTENT_CLASSIFIER_PROMPT 加 LLM fallback 描述（本文件，~第235行）
4. 在 _NO_TOOL_INTENTS 决定默认是否绑工具（本文件，~第263行）
5. 在 INTENT_PROMPTS 加策略变体（agent/research/prompts.py）
   → debate/emotional 策略参考：少调工具、角色人格主导

==== 设计原则 ====

- **优先级列表**：list[tuple] 而非 dict，保证有序匹配。复合意图在前。
- **关键词 ≠ 语义理解**：关键词表不能 100% 准确，但可以做到：
  当误判发生时，30 秒内定位到一条规则、改一个字、立即生效。
- **误判可接受**：对于娱乐型产品，关键词分类的覆盖率和速度优先于准确率。
  未被规则匹配的走 LLM fallback，规则匹配错了的无法自动纠正——
  因此关键词选型要保守，宁可漏判（走 LLM）不可误判。
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
                "类似",
                "推荐",
                "差不多",
                "还有哪些",
                "还有什么",
                "冷门",
                "小众",
                "神作",
                "评分最高",
                "最好看",
                "必看",
                "经典",
                "值得",
                "德不配位",
                "cjb",
                "CJB",
            ],
            "patterns": [
                r"(类似|推荐|像.{1,4}一样|还有什么|找.{1,4}番|求.{1,4}番|跟.{1,4}差不多|和.{1,4}类似|有哪些.{1,4}(番|动漫|作品|番剧|动画))",
            ],
        },
    ),
    (
        "realtime",
        {
            "keywords": [
                "今天",
                "本周",
                "这周",
                "放送",
                "播出",
                "排期",
                "日历",
                "最近什么火",
                "最近流行",
                "热门",
                "趋势",
                "新番",
                "新番推荐",
                "本季",
                "这季度",
                "当季",
            ],
            "patterns": [
                r"(今天|本周|这周|这季度|本季|最近).*(放|播|火|流行|热门|排|新番)",
            ],
        },
    ),
    # ── 优先级 2: 对话向意图 ──────────────────────────
    # ═══════════════════════════════════════════════════════
    # debate — 用户想争论/质疑/表达强烈观点
    #
    # 关键词选型原则：
    #   - 带**主语**的主观评价（"我不服这个评分"），不是客观描述（"主角不服输精神"）
    #   - 强质疑信号（"凭什么封神"），不是普通反问（"为什么好看" → 可能是 lookup）
    #   - 优先覆盖 ACGN 社区常用争论句式（"过誉"、"烂尾"、"就我觉得"）
    #   - 单字关键词容易误判：入选需确认歧义度低
    # ═══════════════════════════════════════════════════════
    (
        "debate",
        {
            "keywords": [
                "过誉",
                "被高估",
                "烂尾",
                "烂不烂",
                "不接受反驳",
                "没有之一",
                "凭什么封神",
                "真有那么好看",
                "真有那么神",
                "怎么都说好",
                "为什么都说",
                "就我觉得",
                "难道只有我",
                "德不配位",
            ],
            "patterns": [
                r"(过誉|烂尾|被高估|德不配位).{0,10}$",
                r".{0,5}(不接受反驳|没有之一|凭什么封神|真有那么好看|真有那么神)",
                r"(就我|难道只有我).{0,5}(觉得|认为)",
            ],
        },
    ),
    # ═══════════════════════════════════════════════════════
    # emotional — 用户有明显的情绪表达
    #
    # 关键词选型原则：
    #   - 第一人称情绪声明（"我失恋了"、"我今天好开心"）
    #   - 显式情绪状态词（"心情不好"、"郁闷"、"情绪低落"）
    #   - 求助信号（"陪我聊聊"、"治愈我"、"让我开心"）
    #   - 避免覆盖：单纯的"推荐治愈番" → discovery
    #              "这部番让我哭了" → 可能是 factual/lookup
    # ═══════════════════════════════════════════════════════
    (
        "emotional",
        {
            "keywords": [
                "失恋",
                "难过",
                "伤心",
                "好想哭",
                "太开心",
                "好开心",
                "开心死了",
                "无聊死了",
                "好无聊",
                "心情不好",
                "郁闷",
                "情绪低落",
                "需要治愈",
                "治愈我",
                "让我开心",
                "陪我聊聊",
            ],
            "patterns": [
                r"^(我|今天|最近).*(失恋|难过|伤心|好?开心|无聊|郁闷|好想哭)",
                r".*(让我|帮我).*(开心|治愈|哭|笑|振作)",
                r"^(心情|情绪).*(不好|低落|差)",
            ],
        },
    ),
    # ── 优先级 3: 简单意图 ──────────────────────────────
    (
        "lookup",
        {
            "keywords": [
                "搜索",
                "找",
                "查",
                "声优",
                "角色",
                "详情",
                "评论",
                "吐槽",
                "几集",
                "多少集",
                "评分",
                "排名",
                "信息",
                # 用户相关查询（@用户名、班友、用户资料）
                "@",
                "班友",
                "用户",
                # ── 序数指代 + 追问信号 ──
                # "具体说说第一部" / "最早那个" / "第二个呢?"
                # 用户在上文有推荐列表，用序数指代追问——明确的 lookup
                "具体说说",
                "详细说说",
                "详细讲",
                "展开说说",
                "展开讲讲",
                "细说",
                "讲一下",
                "最早",
                "最后那个",
            ],
            "patterns": [
                r"^(搜|找|查|帮我).*(评分|声优|角色|详情|评论|评价|多少|几集|信息|排名|用户)",
                r"@\S{1,20}",  # @用户名 是明确的 Bangumi 用户查询信号
                # 序数指代：“第一部”、“第2个”、“第三”
                r"第[一二三四五六七八九十\d]+[部个条件首]",
                # 追问详情：“具体/详细说说XX”、“展开讲讲” —— 不是闲聊
                r"(具体|详细|仔细)(说说|讲讲|介绍|聊聊|说一下)",
            ],
        },
    ),
    (
        "factual",
        {
            "keywords": [
                "什么是",
                "什么叫",
                "定义",
                "解释",
                "三集定律",
                "作画崩坏",
                "是谁",
                "哪一年",
                "什么时候出的",
                "为什么叫",
                "原案",
                "企划",
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
            "keywords": [
                "你好",
                "谢谢",
                "再见",
                "嗨",
                "hello",
                "hi",
                "晚安",
                "早安",
                "早上好",
            ],
            "patterns": [r"^(你好|谢谢|再见|嗨|hello|hi|晚安|早安|早上好)$"],
        },
    ),
]

# ═══════════════════════════════════════════════════════════════════
# LLM fallback prompt
# ═══════════════════════════════════════════════════════════════════

INTENT_CLASSIFIER_PROMPT = """将用户消息分类为以下类别之一，只回复类别名称（一个单词）：

- chitchat: 纯寒暄、问候、感谢，不涉及任何 Bangumi 内容查询
- factual: 领域常识问题（"什么是三集定律"），不需要查询实时数据
- lookup: 精确查找特定条目、评分、声优、评论，或追问上文已提及作品的详情（"具体说说"、"第一部讲什么"）
- discovery: 模糊推荐、探索发现、"类似XX的番"、找新内容
- realtime: 询问当前热门、放送排期、最新动态等时效性信息
- debate: 用户想争论、质疑或表达强烈观点（"EVA被高估了"、"巨人的结尾真的很烂"、"为什么都说这部是神作"）
- emotional: 用户有明显的情绪表达（"失恋了"、"太开心了"、"好无聊"、"心情不好"）
- unknown: 无法明确分类

注意：包含"第一/二/三部"、"最早"、"最后那个"、"具体说说"等追问信号的消息，应归为 lookup，不是 chitchat。

用户消息: {user_message}

类别:"""

# ═══════════════════════════════════════════════════════════════════
# 公开接口
# ═══════════════════════════════════════════════════════════════════

_VALID_INTENTS = frozenset(
    {"chitchat", "factual", "lookup", "discovery", "realtime", "debate", "emotional", "unknown"}
)

# 不绑定工具的意图——LLM 直接回复，角色人格主导。
# 这些意图下模型默认看不到工具 schema，节省 token 预算（~2000 tokens/次）。
# 如模型确实需要工具（如 debate 中想查社区观点），XML 泄漏自纠正机制会触发二次 LLM 调用。
_NO_TOOL_INTENTS = frozenset({"chitchat", "factual", "debate", "emotional"})


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

    # 短消息（< 5 字）且不匹配任何规则关键词 → 极可能是作品名缩写
    # "EVA"、"86"、"K"、"mygo" 等不应走 LLM fallback——LLM 可能误判为
    # chitchat 导致不绑工具、无法搜索。
    # 返回 "unknown" 而非 "lookup"：保留 LLM 自行判断的灵活性。
    # "unknown" 不在 _NO_TOOL_INTENTS 中，工具会绑定，LLM 自行决定是否调用。
    if len(msg) < 5:
        return "unknown"

    return None  # 需要 LLM fallback


async def classify_intent_llm(user_message: str, llm: ChatOpenAI) -> str:
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
        intent = await classify_intent_llm(user_message, llm)
        return (intent, "llm")

    return ("unknown", "rule")
