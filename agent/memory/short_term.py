"""
短期记忆管理 — Layer 1 滑动窗口截断

使用 tiktoken ``cl100k_base`` 精确计数（非 ``len//4`` 估算），
在 Token 预算超限时从头部截断旧消息，保留 SystemMessage 和最近消息。

设计决策：
    - 使用 tiktoken 而非 len//4 估算：中文单字占 1.5-2.5 tokens，JSON 中大括号/引号
      各占 1 token，生产环境中 len//4 会低估 30-50%，导致 context_length_exceeded。
    - 编码器选用 ``cl100k_base``：GPT-4/DeepSeek/Qwen 的通用编码，无需按模型切换。
    - 触发时机在 reasoning_node 开头（方式二）：不改图拓扑，实现更简单。
      工具返回的数据量最不可控，在进入下一轮推理前截断最可靠。

用法::

    from agent.memory import manage_memory

    messages = manage_memory(state["messages"], max_tokens=8000)
"""

from __future__ import annotations

import json
import logging

import tiktoken
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

logger = logging.getLogger("bgm-agent.memory")

# ── 全局编码器（cl100k_base 是 GPT-4/DeepSeek/Qwen 的通用编码） ─
try:
    _ENCODER = tiktoken.get_encoding("cl100k_base")
except Exception:
    logger.warning(
        "tiktoken 编码器初始化失败（版本不兼容或编码缺失），"
        "将回退到 len//2 字符估算。建议: pip install tiktoken>=0.5.0"
    )
    _ENCODER = None

# ── Token 预算分配（Phase 8: 按 depth 分级，与 system prompt 大小解耦） ─
DEPTH_TOKEN_BUDGETS: dict[str, int] = {
    "fast": 10000,
    "deep": 16000,
}
"""按深度模式的 Token 预算。

fast: 10000 tok，覆盖日常精确查询和时效查询
deep: 16000 tok，覆盖深度分析和探索

分配逻辑（以 deep 为例）：
  System Prompt:    ≤2,200 tokens
  对话历史 + tool:  ~12,800 tokens
  LLM 输出缓冲:     ~1,000 tokens
"""

DEFAULT_MAX_TOKENS = 10000
"""默认 Token 预算（auto 模式，向后兼容旧常量引用）。"""

L2_MEMORY_BUDGET_TOKENS = 500
"""L2 记忆注入预留 Token 数上限。Phase 8 收紧至 500（原 700）。"""

# 单条消息最大 Token 数（超出则截断内容，主要针对 ToolMessage 返回的海量 JSON）
# 2000 的依据：条目详情是最大的单条记录，infobox 白名单过滤后 24 部实测
# 均值 1180 / 最大 1465（走真实 API 的最坏一条 1503），取 2000 留余量。
# fast 预算 10000，单条最多占 20%。
_MAX_SINGLE_MESSAGE_TOKENS = 2000

# 截断标记
_TRUNCATION_MARKER = "\n\n...[内容已截断]"


def count_tokens(text: str) -> int:
    """精确 Token 计数（tiktoken cl100k_base）。

    Args:
        text: 任意文本。

    Returns:
        Token 数量。编码失败时回退到 ``len(text) // 2`` 估算。
    """
    try:
        return len(_ENCODER.encode(text))
    except Exception:
        logger.warning("tiktoken encode 失败，使用 len//2 估算（%d 字符）", len(text))
        return max(1, len(text) // 2)


def estimate_tokens(messages: list[BaseMessage]) -> int:
    """精确计算消息列表的 Token 总数。

    对每条消息的 ``content`` 字段进行 tiktoken 编码计数。
    支持 ``content`` 为 ``str`` 或 ``list[dict]``（如 AIMessage 的多模态 content）。

    Args:
        messages: LangChain 消息列表。

    Returns:
        所有消息的总 Token 数。
    """
    total = 0
    for m in messages:
        content = m.content if hasattr(m, "content") else str(m)
        if isinstance(content, str):
            total += count_tokens(content)
        elif isinstance(content, list):
            # AIMessage content 可能为 list[dict]（如 tool_call 结果）
            total += count_tokens(str(content))
        # ToolMessage 的 tool_call_id 不计入（非 LLM 消费内容）
    return total


def _truncate_text_by_tokens(text: str, max_tokens: int) -> str:
    """按 token 数精确截断文本（tiktoken 编码后截断再解码）。

    Args:
        text: 原始文本。
        max_tokens: 保留的最大 token 数。

    Returns:
        截断后的文本。编码失败时回退到字符截断。
    """
    try:
        tokens = _ENCODER.encode(text)
        if len(tokens) <= max_tokens:
            return text
        return _ENCODER.decode(tokens[:max_tokens])
    except Exception:
        logger.warning("tiktoken encode/decode 失败，使用字符截断")
        return text[: max_tokens * 2]  # 退避：中文字符约 2 tokens/字


def _truncate_message_content(msg: BaseMessage, max_tokens: int) -> BaseMessage:
    """截断单条消息内容到指定 token 预算内。

    保留消息元数据（ToolMessage 的 tool_call_id/name、
    AIMessage 的 tool_calls）。非字符串 content 不做截断。

    Args:
        msg: 原始消息。
        max_tokens: 内容 token 上限（含截断标记）。

    Returns:
        截断后的消息（新对象），无需截断时返回原消息。
    """
    content = msg.content if hasattr(msg, "content") else str(msg)
    if not isinstance(content, str):
        return msg

    current_tokens = count_tokens(content)
    if current_tokens <= max_tokens:
        return msg

    marker_tokens = count_tokens(_TRUNCATION_MARKER)
    available = max(50, max_tokens - marker_tokens)
    truncated = _truncate_text_by_tokens(content, available) + _TRUNCATION_MARKER

    logger.debug(
        "memory: 截断消息内容 %d→%d tokens", current_tokens, count_tokens(truncated)
    )

    if isinstance(msg, ToolMessage):
        return ToolMessage(
            content=truncated,
            tool_call_id=getattr(msg, "tool_call_id", ""),
            name=getattr(msg, "name", None),
        )
    elif isinstance(msg, AIMessage):
        new_msg = AIMessage(content=truncated)
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            new_msg.tool_calls = msg.tool_calls
        return new_msg
    elif isinstance(msg, HumanMessage):
        return HumanMessage(content=truncated)

    return msg


def _truncate_oversized_messages(
    messages: list[BaseMessage],
    max_single_tokens: int = _MAX_SINGLE_MESSAGE_TOKENS,
) -> list[BaseMessage]:
    """截断超过单条上限的消息内容（主要针对 ToolMessage 海量 JSON）。

    在列表级截断之前执行，防止一条 ToolMessage 挤占全部上下文窗口。

    Args:
        messages: 消息列表。
        max_single_tokens: 单条消息 token 上限。

    Returns:
        新列表；无变化时返回原列表避免不必要的复制。
    """
    changed = False
    result: list[BaseMessage] = []
    for m in messages:
        # Phase 8: System prompt is sacred — never truncate
        if isinstance(m, SystemMessage):
            result.append(m)
            continue
        content = m.content if hasattr(m, "content") else str(m)
        if isinstance(content, str) and count_tokens(content) > max_single_tokens:
            result.append(_truncate_message_content(m, max_single_tokens))
            changed = True
            logger.info(
                "memory: 截断超大消息 (%s)，%d → ≤%d tokens",
                type(m).__name__,
                count_tokens(content),
                max_single_tokens,
            )
        else:
            result.append(m)
    return result if changed else messages


def _remove_orphaned_tool_messages(
    messages: list[BaseMessage],
) -> list[BaseMessage]:
    """移除孤儿 ToolMessage —— 其配对 AIMessage(tool_calls) 已被截断丢弃。

    当滑动窗口丢弃旧的 AIMessage(tool_calls) 时，其触发的 ToolMessage
    仍留在列表中。这些孤儿 ToolMessage 既浪费 token 预算，又可能在
    DeepSeek 等严格 API 上触发 400 BadRequestError：
    "Messages with role 'tool' must be a response to a preceding
    message with 'tool_calls'."

    同时移除无 content、无 tool_calls 的空 AIMessage（纯 tool_call 壳，
    其配对的 ToolMessage 都已被丢弃时再无意义）。

    Args:
        messages: 待清理的消息列表。

    Returns:
        清理后的消息列表。
    """
    # 收集所有 AIMessage 中声明的 tool_call id
    valid_call_ids: set[str] = set()
    for m in messages:
        if isinstance(m, AIMessage):
            tcs = getattr(m, "tool_calls", None) or []
            for tc in tcs:
                if isinstance(tc, dict) and "id" in tc:
                    valid_call_ids.add(tc["id"])

    result: list[BaseMessage] = []
    removed_orphans = 0
    removed_empty_ai = 0

    for m in messages:
        if isinstance(m, ToolMessage):
            tc_id = getattr(m, "tool_call_id", "")
            if tc_id and tc_id not in valid_call_ids:
                removed_orphans += 1
                logger.debug(
                    "memory: 移除孤儿 ToolMessage (tool_call_id=%s, name=%s)",
                    tc_id,
                    getattr(m, "name", "?"),
                )
                continue
        elif isinstance(m, AIMessage):
            has_content = bool(getattr(m, "content", ""))
            has_tool_calls = bool(getattr(m, "tool_calls", None))
            if not has_content and not has_tool_calls:
                removed_empty_ai += 1
                logger.debug("memory: 移除空 AIMessage（无 content 无 tool_calls）")
                continue

        result.append(m)

    if removed_orphans or removed_empty_ai:
        logger.info(
            "memory: 清理孤儿消息 — ToolMessage=%d, 空 AIMessage=%d",
            removed_orphans,
            removed_empty_ai,
        )

    return result


def trim_messages(
    messages: list[BaseMessage],
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> list[BaseMessage]:
    """滑动窗口截断：保留 SystemMessage + 最近消息。

    策略：
        1. SystemMessage 始终保留（系统提示词不可丢失）
        2. 从消息列表尾部向头部遍历，逐条加入直到 Token 预算耗尽
        3. 超出预算的旧消息被丢弃

    Args:
        messages: 完整消息列表。
        max_tokens: Token 预算上限。默认 8000。

    Returns:
        截断后的消息列表。
    """
    # 分离系统消息（始终保留）
    system_msgs: list[BaseMessage] = [m for m in messages if isinstance(m, SystemMessage)]
    other_msgs: list[BaseMessage] = [m for m in messages if not isinstance(m, SystemMessage)]

    # 计算系统消息的 Token 开销
    system_tokens = estimate_tokens(system_msgs)

    # 从尾部向头部保留（保留最近的消息）
    kept: list[BaseMessage] = []
    token_count = system_tokens

    for m in reversed(other_msgs):
        estimated = estimate_tokens([m])
        if token_count + estimated > max_tokens:
            # 超大单条 ToolMessage：截断内容而非整条丢弃
            if isinstance(m, ToolMessage):
                remaining = max_tokens - token_count
                if remaining > 100:  # 至少保留 100 tokens 才有意义
                    truncated_m = _truncate_message_content(m, remaining)
                    kept.insert(0, truncated_m)
                    token_count += estimate_tokens([truncated_m])
                    logger.warning(
                        "memory: ToolMessage 超出预算，截断至 %d tokens (%s)",
                        remaining,
                        getattr(m, "name", "?"),
                    )
            break
        kept.insert(0, m)
        token_count += estimated

    trimmed_count = len(other_msgs) - len(kept)
    if trimmed_count > 0:
        logger.info(
            "memory: 截断 %d 条旧消息（%d → %d 条），Token: %d/%d",
            trimmed_count,
            len(other_msgs),
            len(kept),
            token_count,
            max_tokens,
        )

    # ── 清理孤儿消息 ──────────────────────────────────────
    # 滑动窗口可能丢弃了 AIMessage(tool_calls) 但保留了其
    # 配对 ToolMessage → 孤儿消息既浪费 token 又触发 API 400 错误
    result = system_msgs + kept
    result = _remove_orphaned_tool_messages(result)
    return result


# ═══════════════════════════════════════════════════════════════════════════
# Tool Result 压缩（Phase 8: 提取关键字段，2000 tokens → ~80 tokens）
# ═══════════════════════════════════════════════════════════════════════════

_COMPRESSION_MARKER = "[已压缩] "

# ═══════════════════════════════════════════════════════════════════════════
# 形状判定（HANDOFF 18 Step 0：先判据、后分派）
# ═══════════════════════════════════════════════════════════════════════════
# 16 个注册工具的返回形状清单见
# docs/tmp/handoff_files/HANDOFF_compress-shape-dispatch-18.md §2；
# 落桶 pin 测试在 test/test_compression_shapes.py。
#
# 判据四条缺一不可（每条都对应一个真实工具，不是防御性编程）：
#   · 非空顶层 list 恰好 1 个      → get_bangumi_subject_detail 有 2 个（rating_count+tags）
#   · 顶层不许有 dict              → get_blog 顶部永远有 blog(dict)、get_user_profile 有 3 个
#   · 元素必须是 dict              → get_*_comments 的 comments 是 str 列表
#   · 元素必须带 id                → get_bangumi_subject_detail 的 tags 无 id

_RECORD_ID_KEYS = ("id", "character_id", "subject_id", "person_id")
"""记录型元素的身份键。**四键缺一不可**：
get_subject_characters 的元素只有 character_id、get_hot_topics 的只有 subject_id。
（eval/reply_quality_judge.py 的 _RECORD_ID_KEYS 是同一键集，两处都是独立定义。）"""

_RECORD_NAME_KEYS = ("name_cn", "name", "title", "subject_name")
"""记录的名字候选链。**顺序有意义，不是随便排的**：

· ``name_cn`` 优先 —— 中文名是站内主用名（search / characters / episodes / local 卡都带）；
· ``title`` 排在 ``subject_name`` 之前 —— get_hot_topics 的元素是**话题**，
  ``title`` 是话题标题（"押井守和神山健治是对的"）、``subject_name`` 是被讨论的条目
  （"攻壳机动队"）。模型要的是前者。
· ``subject_name`` 兜底 —— get_user_timeline 的收藏/进度事件只有它。

旧实现写死 ``name_cn or name or "?"``：hot_topics / timeline 两条链都取不到值，
压出来是 "?"（比不压缩还糟）。候选链就是为了让这两条也拿到名字。"""

_RECORD_TAIL_MAX_CHARS = 40
"""尾串的单值长度上限，超了整值丢弃 —— 记录桶是索引卡，不是正文。"""

_RECORD_TAIL_MAX_ITEMS = 8
"""列表尾串最多渲染几项（投影后的 tags 实测 8 个名字、占 30-51 字）。"""

_RECORD_MAX_LINES = 20
"""单条压缩消息最多渲染几行，与旧实现的上限一致。"""

_TEXT_ITEM_MAX_CHARS = 120
"""单条评论的字符上限。sanitizer 已经截到 200 字（``_truncate(content, 200)``），
这里再收一道：评论是散文，单条占满 200 字时 10 条就 2,400 tok，
得给「多留几条」腾空间。120 字仍够引一句原话。"""

_TEXT_MAX_TOKENS = 1000
"""文本列表桶正文的 token 预算，理由同 `_RECORD_MAX_TOKENS`。
实测：10 条 × 120 字的实体评论约 1,450 tok → 留得住 7 条上下；
现状是 299 tok 的 JSON 拦腰截断，存活率 8%–53%。"""

_PLAIN_MAX_TOKENS = 400
"""单体桶（条目 / 角色 / 人物详情）的 token 上限。

400 不是新拍的：旧实现里 `get_person_detail` / `get_character_detail` 走的就是
400（名单②），单体桶接手后那条硬编码名单才该消失（Step 5）。

这个数够用的账：三个单体工具的标量数是 9 / 4 / 4，合计 < 50 tok；
`sanitizer` 已把 `summary` 封在 200–300 字（约 250 tok），其余短字段约 60 tok。
实测 6 条真实条目详情装配后 300–390 tok。**但上限不靠这笔账兜底** ——
`_compress_plain_record` 会在装配后实测 token，超了就从后往前砍可选字段。"""

_PLAIN_STRING_MAX_CHARS = 120
"""单体桶单条字符串的上限。

比记录桶的 40 字宽得多，因为两者的约束不同：记录桶一行一条记录，
20 行要挤进 1500 tok，长了就得让位给别的条目；单体桶只有**一条**记录，
而 `summary` 正是这个工具的主要产出 —— 截到 40 字等于没给。
120 字够看清「这部番讲什么」，也是 §1 里被 `summary` 挤掉 score/rank 的那个量级。"""

_RECORD_MAX_TOKENS = 1500
"""记录桶正文的 token 预算。

行数封顶挡不住体量：一条 local 卡带 8 个 tags 约 170 字，20 条就是 3,385 字。
实测（2026-09-14）造最坏 20 条 × 8 tags = **2,526 tok**，越过
``_MAX_SINGLE_MESSAGE_TOKENS``（2000）→ 会被 ``_truncate_oversized_messages``
从中间切断，正是本任务要消灭的失败形态。真实样本最坏 1,743 tok（15 条 local 卡，
生产路径的 ``_LOCAL_CARD_CHAR_BUDGET`` 已封到 2,200 字，正常走不到）。
1,500 给标记行与两处「没列全」提示留出余量。
"""


def _try_json(content: str) -> dict | None:
    """解析工具返回的 JSON。失败、或顶层不是 dict，返回 None（→ 保持现状截断）。"""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _has_record_id(record: dict) -> bool:
    return any(k in record for k in _RECORD_ID_KEYS)


def _scalar_count(data: dict) -> int:
    return sum(1 for v in data.values() if isinstance(v, (int, float, bool)) or v is None)


def _classify(data: dict) -> tuple[str | None, str | None, list | None]:
    """按数据形状判定压缩桶：(kind, 承载数组的键, 数组)。

    kind ∈ {"record", "text", "plain", None}；None = 认不出 → 走现状截断（安全阀）。
    """
    if not isinstance(data, dict):
        return None, None, None

    candidates = [(k, v) for k, v in data.items() if isinstance(v, list) and v]
    enveloped = any(isinstance(v, dict) for v in data.values())

    if len(candidates) == 1 and not enveloped:
        key, items = candidates[0]
        if all(isinstance(x, dict) for x in items) and any(_has_record_id(x) for x in items):
            return "record", key, items
        if key == "comments" and all(isinstance(x, str) for x in items):
            return "text", key, items

    # 单体：有标量骨架（get_subject_opinions 只有 1 个标量、9 成内容在两个 dict 里，
    # 压成单体比现状截断还差 —— 它就该落到下面的 None）。
    if _scalar_count(data) >= 2:
        return "plain", None, None
    return None, None, None


def _tail_text(value: object) -> str | None:
    """尾串放得下的值：小标量、短字符串、短字符串列表。放不下返回 None（丢弃）。

    丢弃不是偷懒，是记录桶的定位：它产出索引卡，不是正文。
      · 长散文（``summary`` / ``desc`` / ``content``）—— episodes 的 desc 每集
        200 字，20 集就能把整条消息吃光，而它是「本轮读完就够」的内容；
      · dict（``infobox``）与 dict 列表（投影前的 ``tags``）—— 产出侧已经用
        索引卡形态给过一轮，这里再塞一份只是重复计费。
    两者改不改都不影响模型「知道有哪些条目」，而丢掉 score/rank 会影响。
    """
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        text = value.strip()
        return text if 0 < len(text) <= _RECORD_TAIL_MAX_CHARS else None
    if isinstance(value, list):
        texts: list[str] = []
        for item in value:
            if isinstance(item, bool) or not isinstance(item, (int, float, str)):
                return None
            text = str(item).strip()
            if not text:
                return None
            texts.append(text)
        if texts:
            return "、".join(texts[:_RECORD_TAIL_MAX_ITEMS])
    return None


def _render_record(record: dict) -> str:
    """一条记录 → 一行索卡片：``名字 | 身份键=值 | 其余小字段…``。

    **没有任何工具名分支**：名字从 `_RECORD_NAME_KEYS` 取、身份键从
    `_RECORD_ID_KEYS` 取、其余字段按记录**自身的键序**渲染 —— 同一份代码
    覆盖 search 卡片、日历、趋势、话题、剧集、角色、时间线。
    """
    name_key = next(
        (k for k in _RECORD_NAME_KEYS
         if isinstance(record.get(k), str) and record[k].strip()),
        None,
    )
    id_key = next((k for k in _RECORD_ID_KEYS if k in record), None)

    parts: list[str] = []
    name = ""
    if name_key is not None:
        name = str(record[name_key]).strip()
        parts.append(name)
    if id_key is not None:
        # 用**真实键名**渲染：character_id=86246 / subject_id=496276 / id=305429。
        # 一律写成 id= 会让「这是角色 ID 还是条目 ID」变得要靠猜 ——
        # 而下一步工具调用的入参类型正取决于此。
        parts.append(f"{id_key}={record[id_key]}")

    seen_score = seen_rank = False
    for key, value in record.items():
        if key == name_key or key == id_key:
            continue
        # ⭐/# 沿用产出侧卡片的字形：本轮工具结果里就是这个样子，压缩后换一套
        # 写法会让模型对不上账。0 是「无人评分」的哨兵（sanitize_subject_search
        # 给无评分条目填 score=0/rank=0），渲染成「⭐0 | #0」比不渲染更糟。
        if key == "score":
            seen_score = True
            if value:
                parts.append(f"⭐{value}")
            continue
        if key == "rank":
            seen_rank = True
            if value:
                parts.append(f"#{value}")
            continue
        # 名字链的另一端常常是同一个值（实测 456 条真实记录里 164 条
        # name == name_cn），重复渲染一份只是白花预算 —— 同值不算信息。
        if key in _RECORD_NAME_KEYS and str(value).strip() == name:
            continue
        tail = _tail_text(value)
        if tail is not None:
            parts.append(f"{key}={tail}")

    # 嵌套 rating 兜底（API 原始形状）：产出侧是扁平的，只有原始响应走这里。
    # 与旧实现同语义 —— 看的是「扁平键在不在」，不是「值真不真」。
    rating = record.get("rating")
    if isinstance(rating, dict):
        if not seen_score and rating.get("score"):
            parts.append(f"⭐{rating['score']}")
        if not seen_rank and rating.get("rank"):
            parts.append(f"#{rating['rank']}")

    return " | ".join(parts)


def _compress_text_list(msg: ToolMessage, data: dict, list_key: str, items: list) -> ToolMessage:
    """文本列表（评论）→ 每条一行，按预算收口。

    旧路径是 `_compress_by_truncation(300)` —— 把 JSON 从中间切断：字符串被砍
    一半，而排在 comments 之前的短键还要先占位。实测这个桶的两个工具存活率
    只有 8%–53%。

    **保留条目本身，而不是「每条平均分预算」**：切碎成 30 个 7 字残片谁也引不了，
    留 7 条完整评论才接得上话。砍掉的条数会披露出去（同记录桶）。

    排序沿用 sanitizer 的旧→新（`sanitize_comments` 末尾 `cleaned.reverse()`），
    所以截的是**最新**那几条。这是与现状一致的选择，不是没想过。
    """
    # 信封里的身份信息必须留：`get_entity_comments` 的 entity_name 排在 comments
    # 【之前】，今天靠截断恰好能活下来；换成先列评论就会把它挤掉 —— 那是把
    # 一个损失换成另一个损失，不是修复。
    head = " | ".join(
        f"{key}={text}"
        for key, value in data.items()
        if key != list_key and (text := _tail_text(value)) is not None
    )

    lines: list[str] = []
    used = 0
    for raw_text in items:
        body = " ".join(str(raw_text).split())  # 压掉换行，保持「一行一条」
        if len(body) > _TEXT_ITEM_MAX_CHARS:
            body = body[:_TEXT_ITEM_MAX_CHARS] + "…"
        cost = count_tokens(body) + 2  # "- " 与换行
        if lines and used + cost > _TEXT_MAX_TOKENS:
            break
        lines.append(f"- {body}")
        used += cost
    # `lines` 非空才肯 break：至少留一条，不让预算把整块变成空消息。

    summary = _COMPRESSION_MARKER + head + "\n" + "\n".join(lines)
    if len(lines) < len(items):
        summary += f"\n… 本行只列了前 {len(lines)} 条，另有 {len(items) - len(lines)} 条未列出"

    return ToolMessage(
        content=summary,
        tool_call_id=getattr(msg, "tool_call_id", ""),
        name=getattr(msg, "name", None),
    )


def _compress_records(msg: ToolMessage, data: dict, items: list) -> ToolMessage:
    """记录列表 → 每行一条索引卡。覆盖 7 个记录型工具 + 用户时间线。

    旧实现（`_compress_search_result`）写死了 ``name_cn or name or "?"`` 与
    ``results``/``data`` 两个承载键，只对 search 系两个工具成立；换到
    hot_topics / characters / episodes 上会各丢一样东西（名字、id、日期）。
    """
    lines = [f"- {line}" for line in map(_render_record, items) if line]

    # 两道闸：行数封顶（20，与旧实现一致）+ token 预算。后者是必需的——
    # 行数封顶只管条数不管体量，长名字 + tags 能让 20 行顶到 2,500 tok。
    kept: list[str] = []
    used = 0
    for line in lines[:_RECORD_MAX_LINES]:
        cost = count_tokens(line) + 1  # +1 是换行
        if kept and used + cost > _RECORD_MAX_TOKENS:
            break
        kept.append(line)
        used += cost
    # `kept` 非空才肯 break：至少留一条，不让预算把整块变成空消息。

    summary = _COMPRESSION_MARKER + "\n".join(kept)
    if len(kept) < len(lines):
        # 与下面 shown/total 那句措辞错开，免得模型读到两句「共 N 条」对不上。
        summary += f"\n… 本行只列了前 {len(kept)} 条，另有 {len(lines) - len(kept)} 条未列出"

    # 产出侧因整块预算丢过条时（search_local_bangumi 的信封带 shown/total），
    # 压缩后仍要让模型知道「没看全」——否则下一轮它会把 8 条当成全部。
    # search_bangumi_subject 没有 shown 键，天然跳过。
    shown = data.get("shown")
    total = data.get("total")
    if isinstance(shown, int) and isinstance(total, int) and shown < total:
        summary += f"\n（本次检索共 {total} 条，此处只有前 {shown} 条）"

    return ToolMessage(
        content=summary,
        tool_call_id=getattr(msg, "tool_call_id", ""),
        name=getattr(msg, "name", None),
    )


def _is_scalar(value: object) -> bool:
    """标量 = 不参与单体桶预算竞争的值（`_scalar_count` 同一口径）。"""
    return isinstance(value, (int, float)) or value is None


def _plain_value_text(value: object) -> str | None:
    """单体字段 → 渲染文本；容器返回 None（调用方丢弃，必要时披露）。

    与记录桶的 `_tail_text` 是两套策略，**别合并**：记录桶一行一条记录、40 字封顶，
    单体桶只有一条记录、给到 `_PLAIN_STRING_MAX_CHARS`。
    """
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return str(value)
    if value is None:
        return None  # null 是「这个字段是空的」，没有内容可显示，也不值得披露
    if isinstance(value, str):
        text = value.strip()
        return _clip_plain(text) if text else None
    if isinstance(value, list):
        if any(isinstance(x, (dict, list)) for x in value):
            return None  # 含容器的列表按容器处理（tags 就是这种），整块丢弃
        items = []
        for x in value:
            if isinstance(x, bool) or isinstance(x, (int, float)):
                items.append(str(x))
            elif isinstance(x, str) and x.strip():
                items.append(x.strip())
        # 标量列表整体留：rating_count 是 10 格评分分布（约 20 tok），
        # 是「这部番是不是争议作」的唯一依据，截前 8 格会让分布看着不完整。
        return _clip_plain("、".join(items)) if items else None
    return None  # dict


def _clip_plain(text: str) -> str:
    if len(text) <= _PLAIN_STRING_MAX_CHARS:
        return text
    return text[:_PLAIN_STRING_MAX_CHARS] + "…"


def _is_dropped_container(value: object) -> bool:
    """这个被丢弃的值值不值得披露。

    **空容器不披露**：`infobox: {}` 是「这个条目没有 infobox」，写一行
    「另有 infobox 未显示」反而是假消息 —— 模型会去找它其实没有的东西。
    """
    if isinstance(value, dict):
        return bool(value)
    if isinstance(value, list):
        return any(isinstance(x, (dict, list)) for x in value)
    return False


def _compress_plain_record(msg: ToolMessage, content: str, data: dict) -> ToolMessage:
    """单体记录（条目 / 角色 / 人物详情）→ ``key=value`` 串成一条，标量无条件保留。

    与记录桶不同，这里**不压换行**：只有一条记录，`summary` 里原有的分段
    留着比压平好读（记录桶压平是因为一行要装一条、20 行挤一条消息）。

    这是本任务真正的收益点。旧路径 `_compress_by_truncation(300)` 从 JSON 头部
    切 300 token：`get_bangumi_subject_detail` 实测 1,343 → 299 tok，
    `score`(4 字) 与 `rank`(2 字) 被前面 284 字的 `summary` 挤出窗口，**6/6 全丢**
    —— 同一部番在下一轮里，模型记得剧情梗概、忘了评分排名。

    两条保命规则：
      · **内容已在预算内 → 原样返回**。`get_character_detail` 真实 payload 只有
        60–181 tok，今天整条（含 infobox）活着；重排字段顺序反而会把它弄丢。
        这条不能省，`_compress_by_truncation` 也是这么做的。
      · **标量不参与预算竞争**。它们合计 < 50 tok，却是最容易被长文本挤掉的部分，
        而 score/rank/eps/date 恰恰是模型引用时最需要的。

    丢弃的容器（`infobox`/`tags`/`collection`）会在末尾列一行 —— 模型得知道
    它没看到什么，否则又会「以为自己看全了」。
    """
    # ① 已在预算内 → 一个字节都不动
    if count_tokens(content) <= _PLAIN_MAX_TOKENS:
        return msg

    # ② 拆字段。键序保持记录自身的顺序：不重排 —— 标量不靠「排前面」保命，
    #    靠的是下面装配时豁免预算。真按标量优先排会压出
    #    「id=265 | eps=26 | volumes=0 | series=False | … | name=新世纪福音战士」，
    #    读起来没有主语，而压缩后模型还得靠这一行认人。
    fields: list[tuple[str, str, bool]] = []      # (键, 渲染片段, 是否标量)
    containers: list[str] = []                     # 整块丢弃的容器，要披露
    for key, value in data.items():
        text = _plain_value_text(value)
        if text is None:
            if _is_dropped_container(value):
                containers.append(key)
            continue
        # ⭐/# 沿用记录桶与产出侧卡片的字形（同一个条目在两轮里长得一样，模型好对账）。
        # 0 是「无人评分」的哨兵：渲染成 ⭐0 / #0 比不渲染更糟（同 `_render_record`）。
        if key == "score":
            if value:
                fields.append((key, f"⭐{text}", True))
            continue
        if key == "rank":
            if value:
                fields.append((key, f"#{text}", True))
            continue
        fields.append((key, f"{key}={text}", _is_scalar(value)))

    # ③ 装配：标量无条件进，其余按剩余预算进；超了从**后**往前砍可选字段。
    #    砍完再实测一次（连同「未显示」那行一起算）—— 估算兜不住，
    #    得让「总输出 ≤ _PLAIN_MAX_TOKENS」是可验证的事实而不是设计意图。
    kept = list(fields)
    squeezed: list[str] = []
    while True:
        body = " | ".join(fragment for _, fragment, _ in kept)
        summary = _COMPRESSION_MARKER + body
        hidden = containers + squeezed
        if hidden:
            summary += f"\n（另有 {'/'.join(hidden)} 未显示）"
        if count_tokens(summary) <= _PLAIN_MAX_TOKENS:
            break
        idx = next((i for i in range(len(kept) - 1, -1, -1) if not kept[i][2]), None)
        if idx is None:
            # 标量自己就超预算（真实形状不可能：三个工具合计 < 50 tok）。
            # 不退化成空消息，也不硬撑 —— 交回现状截断路径（CLAUDE.md 规则 1）。
            logger.warning(
                "memory: 单体记录标量已超 %d tok，退回按 token 截断", _PLAIN_MAX_TOKENS
            )
            return _compress_by_truncation(msg, content, _PLAIN_MAX_TOKENS)
        squeezed.insert(0, kept.pop(idx)[0])

    return ToolMessage(
        content=summary,
        tool_call_id=getattr(msg, "tool_call_id", ""),
        name=getattr(msg, "name", None),
    )


def _compress_tool_result(msg: ToolMessage) -> ToolMessage:
    """将 ToolMessage 的 content 压缩为关键字段摘要。

    当前轮的工具结果保留完整；上一轮的工具结果调用此函数压缩。
    不是丢弃信息——是提取 agent 后续引用时最可能需要的关键字段。

    Args:
        msg: 原始 ToolMessage。

    Returns:
        压缩后的 ToolMessage（新对象）。无需压缩时返回原消息。
    """
    content = msg.content if hasattr(msg, "content") else ""
    if not isinstance(content, str) or not content.strip():
        return msg

    # ── 按数据形状分派（HANDOFF 18）──────────────────────────────────
    # **本函数里没有任何工具名**：认的是形状，不是名字。加一个新工具（哪怕
    # 名字还没想好）也一样落桶 —— 这正是本任务要消灭的长期负债。
    try:
        data = _try_json(content)
        kind, key, items = _classify(data) if data is not None else (None, None, None)
    except Exception:
        # 形状识别本身绝不能成为新的失败点：认不出就退回按 token 截断，
        # 与改动前的兜底分支完全一致（CLAUDE.md 规则 1：记忆层不抛异常）。
        logger.warning("memory: 工具结果形状识别失败，退回按 token 截断", exc_info=True)
        data, kind, key, items = None, None, None, None

    # 记录列表桶：7 个记录型工具 + get_user_timeline（收藏/进度事件）。
    # 【不看工具名】—— 认的是「非空顶层 list 恰好 1 个 + 无 dict 信封 +
    # 元素是 dict + 元素带身份键」这个形状。
    if kind == "record" and items is not None:
        return _compress_records(msg, data, items)

    # 文本列表桶：目前只有 get_entity_comments。
    # ★ get_episode_comments 不在这里 —— 它的真实返回顶层有个 episode(dict)
    #   （clients/client.py:187），被「顶层不许有 dict」这条挡在外面，落兜底。
    #   它属于文档 §6「多部件信封」那一族（episode + comments 两个子部件），
    #   要修得先有「每个子部件各自压缩」的机制，本任务范围外。
    if kind == "text" and items is not None and key is not None:
        return _compress_text_list(msg, data, key, items)

    # 单体桶：标量骨架（≥2 个）的单条记录 —— 条目 / 角色 / 人物详情。
    # ⭐ get_person_detail / get_character_detail 走的就是这里（Step 4 之前它们
    #    靠名单② 拿 400）；条目详情在此之前是 300 tok 的拦腰截断，
    #    score/rank 6/6 全丢。
    if kind == "plain" and data is not None:
        return _compress_plain_record(msg, content, data)

    # 认不出的形状 → 保持现状（多部件信封 / 空结果 / 纯文本）。
    # 【安全阀，一个字节都不改】：`get_subject_opinions` / `get_blog` /
    # `get_user_profile` 三个多部件信封在这里原样走老路（§6）。
    #
    # 名单② 删除后，`get_person_detail` / `get_character_detail` 的**非 JSON**
    # 内容也落到这里（400 → 300）。实测不影响任何可达输入：这条路径只由
    # `ToolNode(handle_tool_errors=format_tool_error)` 的异常文本产生
    # （`agent/guardrails.py:163`，纯文本不是 JSON），实测参数校验失败 63–77 tok、
    # `_error` 返回是定长短串且走 JSON 分支 —— 都远低于 300，两边都原样返回。
    # 要踩到差异得有 >300 tok 的异常文本（约 600 中文字），这两个工具不产生。
    return _compress_by_truncation(msg, content, max_tokens=300)


_ERROR_KEY_SUFFIX = "_error"
"""顶层错误键的统一后缀（``comments_error`` / ``reviews_error`` / ``blog_error`` …）。

``"_error".endswith("_error")`` 为真，所以裸 ``_error`` 也算 —— 一条判据覆盖两种写法。
"""

_ERROR_KEY_MAX_TOKENS = 75
"""错误提示在兜底预算里最多占多少 token（300 的四分之一）。

可达的错误串最长约 60 字 / 30 tok（``clients/base.py:178`` 的 404 文案带 path），
75 是照实测量级留的余量。真被这个上限截到会记 warning —— 那说明有工具开始
往 ``_error`` 里塞正文了，得回来看。
"""

_ERROR_VALUE_MAX_CHARS = 120
"""单个错误值的字符上限（与单体桶的字符串上限同口径）。"""


def _error_notice(data: object) -> str:
    """顶层错误键（``_error`` / ``*_error``）→ 一行提示；没有则返回空串。

    **错误提示必须比正文先活下来。** ``get_episode_comments`` 的返回里
    ``comments_error`` 排在 ``episode`` 之后，而 episode 一个就 334 tok（含 500 字
    ``desc``）—— 兜底从 JSON 头部切 300 tok 时，错误键**一定**在窗口之外。
    模型于是看到「这集没什么评论」，而不是「评论没拉到」。

    为什么这条比丢评论更严重：丢评论至少有「另有 N 条未列出」的披露，**丢错误是
    让 agent 不知道自己不知道** —— 它会照着空评论区把话讲圆。实测（2026-09-14）：
    评论拉取失败那条 payload 373 tok → 299 tok，``comments_error`` 被切掉，
    HTTP 503 的原因一个字都传不到下一轮。

    认的是**后缀约定**，不是工具名名单（本任务刚删掉的那个反模式）。这族工具
    产生顶层错误键的地方只有 5 处（``clients/client.py`` 的 198 / 238 / 252 /
    347 / 446 行），且**全部落在兜底路径上** —— 记录桶 / 文本桶 / 单体桶都接不到
    带错误键的形状（它们要么被「顶层不许有 dict」拦下，要么顶层 list 数不为 1），
    所以只需在兜底负责。`test_compression_shapes.py` 有 pin 钉着这条前提。
    """
    if not isinstance(data, dict):
        return ""
    parts: list[str] = []
    for key, value in data.items():
        if not isinstance(key, str) or not key.endswith(_ERROR_KEY_SUFFIX):
            continue
        if not isinstance(value, str) or not value.strip():
            continue
        text = value.strip()
        if len(text) > _ERROR_VALUE_MAX_CHARS:
            text = text[:_ERROR_VALUE_MAX_CHARS] + "…"
        parts.append(f"{key}={text}")
    return " | ".join(parts)


def _compress_by_truncation(msg: ToolMessage, content: str, max_tokens: int) -> ToolMessage:
    """按 token 截断内容，添加压缩标记。

    截断前先把顶层错误提示摘出来放在最前面（见 `_error_notice`）：它只有一二十
    token，却是「这次没拿到数据」的唯一凭据，不能跟着正文一起被切掉。

    **没有错误键时，这条路径与改动前逐字节相同** —— 对拍钉着（Step 6）。
    """
    current = count_tokens(content)
    if current <= max_tokens:
        return msg
    notice = _error_notice(_try_json(content))
    if notice:
        if count_tokens(notice) > _ERROR_KEY_MAX_TOKENS:
            logger.warning(
                "memory: 错误提示超 %d tok，截断后展示（有工具开始往 _error 里塞正文？）",
                _ERROR_KEY_MAX_TOKENS,
            )
            notice = _truncate_text_by_tokens(notice, _ERROR_KEY_MAX_TOKENS)
        notice += "\n"
    budget = max_tokens - count_tokens(_COMPRESSION_MARKER) - count_tokens(notice)
    truncated = _truncate_text_by_tokens(content, max(1, budget))
    return ToolMessage(
        content=_COMPRESSION_MARKER + notice + truncated,
        tool_call_id=getattr(msg, "tool_call_id", ""),
        name=getattr(msg, "name", None),
    )


def _find_last_real_human_idx(messages: list[BaseMessage]) -> int:
    """找到最后一条真实用户消息的索引（跳过系统注入的 HumanMessage）。"""
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if isinstance(m, HumanMessage):
            content = m.content if hasattr(m, "content") else ""
            if content and not str(content).startswith("（系统指令："):
                return i
    return -1


def _compress_previous_round_tools(messages: list[BaseMessage]) -> list[BaseMessage]:
    """压缩上一轮的 ToolMessage，当前轮的保留完整。

    判断当前轮：最后一条真实 HumanMessage 之后的所有消息视为当前轮。
    """
    last_human_idx = _find_last_real_human_idx(messages)
    if last_human_idx < 0:
        return messages

    changed = False
    result: list[BaseMessage] = []
    for i, m in enumerate(messages):
        if isinstance(m, ToolMessage) and i < last_human_idx:
            compressed = _compress_tool_result(m)
            if compressed is not m:
                changed = True
            result.append(compressed)
        else:
            result.append(m)

    if changed:
        before = estimate_tokens(messages)
        after = estimate_tokens(result)
        logger.info(
            "memory: 压缩历史 ToolMessage — %d → %d tokens (节省 %d%%)",
            before, after, int((1 - after / max(before, 1)) * 100),
        )

    return result


def manage_memory(
    messages: list[BaseMessage],
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> list[BaseMessage]:
    """记忆管理入口：压缩历史工具结果 → 截断超大消息 → 滑动窗口。

    Phase 8 三步策略：
        1. 压缩上一轮 ToolMessage——提取关键字段，2000→80 tokens
        2. 截断当前轮超大消息（SystemMessage 除外）
        3. 滑动窗口截断

    在 reasoning_node 开头调用。

    Args:
        messages: 当前消息列表。
        max_tokens: Token 预算上限。

    Returns:
        可能压缩/截断后的消息列表。
    """
    # Step 0: 压缩上一轮工具结果（Phase 8: 保留语义骨架）
    messages = _compress_previous_round_tools(messages)

    # Step 1: 截断超大单条消息（SystemMessage 除外）
    messages = _truncate_oversized_messages(messages)

    # Step 2: 检查总预算
    current_tokens = estimate_tokens(messages)
    if current_tokens <= max_tokens:
        logger.debug("memory: Token %d ≤ 预算 %d，无需截断", current_tokens, max_tokens)
        return messages

    logger.info(
        "memory: Token %d > 预算 %d，触发滑动窗口截断",
        current_tokens,
        max_tokens,
    )
    return trim_messages(messages, max_tokens)
