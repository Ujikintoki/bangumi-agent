"""
共享 Guardrail 函数

纯函数集合，供 Research Agent 和 Dialogue Agent 共用：
- 终端回复检测（逃逸舱）— 防止 Critic/路由对合法短回复误判
- XML 工具调用泄漏剥离 — 防止 DeepSeek 等模型在无工具通道输出 <function_calls>
- 重复工具调用检测 — 防止 LLM 在无效结果上反复调用同一工具
- ToolNode 错误格式化 — 剥离堆栈信息防止进入 LLM 上下文
"""

from __future__ import annotations

import re

from langchain_core.messages import AIMessage

# ═══════════════════════════════════════════════════════════════════
# 终端回复识别模式（逃逸舱）
# ═══════════════════════════════════════════════════════════════════
# 当 AI 回复匹配以下任一模式时，视为合法终端状态（追问、澄清、
# 诚实告知数据不存在、说明领域约束），即使字数较少也不应被
# Critic 判定为 REVISE 或被 Dialogue Agent 继续迭代。

TERMINAL_RESPONSE_PATTERNS = [
    # 追问澄清
    r"您(是指|说的|想查|要找).{1,30}(吗|\?|？)",
    r"请问.{1,30}(吗|\?|？)",
    r"(需要|请).{1,20}(确认|指定|明确|说明)",
    # 诚实告知不存在
    r"(未|没有|无法)(找到|检索到|搜索到|匹配|收录|发现)",
    r"暂无.{1,20}(数据|信息|结果|记录|评分|评论)",
    r"(数据库|站内|系统|本地|Bangumi).{0,10}(不含|没有|不存在|未收录)",
    r"(暂无|没有|无)(收录|相关|匹配).{0,10}(条目|信息|数据)",
    # 建议用户下一步操作
    r"(建议|推荐|您可以|请尝试|不妨).{1,30}(搜索|查找|确认|尝试|访问)",
    # 角色/人物无评分说明
    r"(角色|人物|声优|真人).{0,5}(没有|无|不含|不提供).{0,5}(评分|rating)",
    r"(只有|仅有).{1,10}(条目|作品|subject).{1,10}(评分|rating)",
    # 多候选让用户选
    r"(可能|也许).{1,10}(是|指).{1,30}(还是|或者|哪一个)",
    r"以下.{1,20}(候选|可能|结果)",
    # ── 2026-06-10 新增：数据不足/结果有限的诚实告知 ──
    r"数据不足.{0,10}(建议|请|可)",
    r"(结果|数据|信息).{0,5}(较少|不足|有限|不多)",
    r"(可|请).{0,5}(扩大|放宽|调整|更换).{0,5}(搜索|范围|关键词)",
]


def is_terminal_response(content: str) -> bool:
    """判断 AI 回复是否为合法的终端状态。

    当 LLM 在执行以下操作时，说明它已经完成了"尽职"的部分，
    不需要 Critic 要求它继续搜索或展开：
    - 向用户追问以澄清意图
    - 诚实告知数据客观不存在
    - 建议用户换一种方式搜索
    - 说明 Bangumi 数据模型的边界（如角色没有评分）
    - 告知搜索结果不足并建议调整

    Args:
        content: AI 回复的文本内容。

    Returns:
        True 如果该回复应被视为合法终端状态。
    """
    return any(re.search(pattern, content) for pattern in TERMINAL_RESPONSE_PATTERNS)


# ═══════════════════════════════════════════════════════════════════
# XML 工具调用泄漏检测与剥离
# ═══════════════════════════════════════════════════════════════════
# DeepSeek 等 function-calling 微调模型在解绑工具后仍可能在 .content
# 中输出原始 XML/DSML 标签。这些模式用于检测和剥离泄漏的标签。

TOOL_CALL_XML_BLOCK = re.compile(
    r"<\s*function_calls\s*>.*?</\s*function_calls\s*>",
    re.IGNORECASE | re.DOTALL,
)
"""匹配完整的 <function_calls>...</function_calls> 块（DeepSeek DSML 格式）。"""

TOOL_CALL_XML_RESIDUE = re.compile(
    r"<\s*(?:function_calls|invoke|parameter|xml)[\s>]",
    re.IGNORECASE,
)
"""匹配 XML 工具调用标签的残骸（用于 Critic 快速检测）。"""


def strip_tool_call_xml(content: str) -> tuple[str, bool]:
    """剥离 LLM 回复中泄漏的工具调用 XML/DSML 标签。

    只剥离完整 XML 块（``<function_calls>...</function_calls>``），
    不破坏正常文本内容。空字符串/纯空白不触发剥离。

    Args:
        content: LLM 回复的文本内容。

    Returns:
        ``(cleaned_content, was_stripped)`` 元组。
    """
    if not content or not content.strip():
        return content, False
    cleaned = TOOL_CALL_XML_BLOCK.sub("", content).strip()
    was_stripped = cleaned != content.strip()
    return cleaned, was_stripped


# ═══════════════════════════════════════════════════════════════════
# 重复工具调用检测
# ═══════════════════════════════════════════════════════════════════


def check_duplicate_tool_calls(messages: list) -> str:
    """检测 LLM 是否连续两轮调用相同工具（参数完全一致）。

    当 LLM 连续两轮调用相同工具且参数一致时，说明工具可能返回了错误
    或空结果，LLM 陷入无效重试。此时应强制切换到不同策略。

    Args:
        messages: 完整消息历史列表。

    Returns:
        非空字符串表示检测到重复调用（可直接作为反馈注入），
        空字符串表示无重复。
    """
    tool_call_rounds: list[list[dict]] = []
    for m in messages:
        if isinstance(m, AIMessage) and hasattr(m, "tool_calls") and m.tool_calls:
            tool_call_rounds.append(list(m.tool_calls))

    if len(tool_call_rounds) < 2:
        return ""

    prev = tool_call_rounds[-2]
    curr = tool_call_rounds[-1]

    dup_names: set[str] = set()
    for ptc in prev:
        for ctc in curr:
            if ptc.get("name") == ctc.get("name") and ptc.get("args") == ctc.get("args"):
                dup_names.add(ctc.get("name", "?"))

    if dup_names:
        return (
            f"连续两轮调用了相同工具 {'/'.join(sorted(dup_names))} 且参数未变 | "
            "上一轮该工具返回了错误或空数据，请换用不同工具（如 get_trending_topics 替代 get_calendar）"
            "或直接告知用户当前数据不可用 | "
            "重复调用"
        )
    return ""


# ═══════════════════════════════════════════════════════════════════
# ToolNode 错误格式化
# ═══════════════════════════════════════════════════════════════════


def format_tool_error(error: Exception) -> str:
    """格式化工具执行错误，剥离堆栈信息防止进入 LLM 上下文。

    LangGraph ToolNode 的 ``handle_tool_errors`` 接受 callable，
    用此函数替换 ``True`` 可防止文件路径和堆栈帧泄漏到 LLM 上下文中。

    Args:
        error: 工具函数抛出的异常。

    Returns:
        仅含异常类型和消息的简短错误描述。
    """
    return f"工具执行失败（{type(error).__name__}）：{error}"
