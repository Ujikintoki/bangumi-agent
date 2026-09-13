"""render 降级路径 —— 回归测试（离线、确定、不调 LLM）。

**背景（2026-09-13，轴 3 首跑抓到）**：chat 意图下 render 的 LLM 返回空串时，
调用方把【喂给模型的指令】当成正文降级吐给了用户 —— 内部提示词原样泄漏，
冻结样本 `B6-闲聊回应` 复现。根因是一个变量兼两用：`render_input` 既当模型
输入、又当降级素材，而判断条件 `render_input != "（无数据）"` 只能区分
"有没有数据"，区分不了"能不能给人看"。

修复后要锁住的契约：

1. chat 分支的指令文本 **永不**示人（这次泄漏的就是它）；
2. chat 分支 render 失败也 **不能返回 None** —— messages 里躺着 L1 缓存中
   上一轮的 AIMessage（chat 直通 END，本轮不产生新 AIMessage），端点的
   `_extract_final_reply` 会往上翻把它当成这一轮的回答，用户看到的是复读。
   修复前这条路径被泄漏掩盖着，拆掉泄漏就会露出来；
3. 非 chat 分支的降级行为 **一字不变** —— 那条路本来就对（反证：同一轮
   render 失败，`D5-社区评价` 降级出"搜索超时，未能获取…"是按设计工作的）。

代价是"没有 AI 回复"变成了"AI 角色说了一句它其实没想说的话"。这个取舍是
有意的：兜底话术诚实（承认没接住），且不泄漏任何内部文本。

全程离线：`main.render_reply` 被 mock 成返回 None，不联网、不花钱、不碰数据库。
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

import main
from agent.persona.render import _RENDER_FALLBACK_LINES, render_fallback_line
from eval.reply_quality_eval import (
    _DEGRADE_MARKERS,
    _LEAK_PATTERNS,
    _RenderSignalHandler,
)

# 触发这次修复的真实泄漏原文（冻结样本 reply_quality-20260913-111412 的 reply 字段）
LEAKED_TEXT = (
    "用户对你说：哈哈，你这回复太懂我了\n\n"
    "这是一段闲聊。自然地用你的角色性格回复。"
    "不要列数据、不要提搜索、就像朋友聊天一样。"
)

# Aggregator 的可读文本 + 一个 markdown 表格（验降级清理仍然生效）
AGGREGATOR_TEXT = (
    "搜索超时，未能获取《进击的巨人》的评分。\n\n"
    "| 项目 | 值 |\n| --- | --- |\n| 评分 | 8.3 |"
)

PREV_TURN_REPLY = "第一轮的回复，来自缓存"


def _chat_messages(*, with_history: bool = True) -> list:
    """chat 意图的 messages —— 默认带 L1 缓存来的上一轮 AIMessage。"""
    msgs: list = [SystemMessage(content="seed")]
    if with_history:
        msgs += [HumanMessage(content="第一轮问题"), AIMessage(content=PREV_TURN_REPLY)]
    return msgs + [HumanMessage(content="哈哈，你这回复太懂我了")]


def _explore_messages(*, with_aggregator_text: bool = True) -> list:
    """非 chat 意图的 messages —— 是否带 Aggregator 的可读文本。"""
    msgs: list = [SystemMessage(content="seed"), HumanMessage(content="进击的巨人评分")]
    if with_aggregator_text:
        msgs.append(AIMessage(content=AGGREGATOR_TEXT))
    return msgs


async def _render(messages: list, *, intent: str, style: str = "bangumi"):
    """在 render_reply 返回 None 的前提下跑一次统一渲染路径。"""
    with patch("main.render_reply", AsyncMock(return_value=None)):
        return await main._render_final_reply(
            messages=messages,
            user_query="哈哈，你这回复太懂我了",
            query_intent=intent,
            output_style=style,
            depth="fast",
        )


def _final_reply_from(messages: list, rendered: str | None) -> str:
    """复现端点的最后一步：把 messages 交给 _extract_final_reply。"""
    if rendered:
        messages = main._replace_last_ai_content(messages, rendered)
    return main._extract_final_reply(messages)


# ═══════════════════════════════════════════════════════════════════
# 契约 1 + 2：chat 分支
# ═══════════════════════════════════════════════════════════════════


class TestChatBranch:
    """chat 分支 render 失败 —— 曾经泄漏内部提示词的地方。"""

    @pytest.mark.asyncio
    async def test_no_scaffolding_in_reply(self):
        """修复的核心：给模型看的指令一个字都不能吐给用户。"""
        _, rendered = await _render(_chat_messages(), intent="chat")

        assert rendered, "chat 分支必须有回复，返回 None 会掉进复读"
        for pattern_name, pattern in _LEAK_PATTERNS.items():
            assert not pattern.search(rendered), (
                f"chat 降级回复命中了泄漏模式「{pattern_name}」：{rendered!r}"
            )

    @pytest.mark.asyncio
    async def test_does_not_echo_previous_turn(self):
        """只把泄漏拆掉、让 chat 返回 None 的话，端点会捞出缓存里的上一轮回复。

        断言的是**端点最终吐给用户的那段字**（走 `_extract_final_reply`），
        不是 `_render_final_reply` 的返回值 —— 复读是在端点那一步发生的。
        """
        messages, rendered = await _render(_chat_messages(), intent="chat")

        assert _final_reply_from(messages, rendered) != PREV_TURN_REPLY
        assert "第一轮" not in _final_reply_from(messages, rendered)

    def test_echo_failure_mode_is_real(self):
        """反证：上面那条断言不是空的。

        "chat 分支退回 None" 这个看起来最自然的修法，实际后果就是复读 ——
        这里直接演示：messages 原样交给端点，捞出来的正是上一轮的回复。
        """
        assert main._extract_final_reply(_chat_messages()) == PREV_TURN_REPLY

    @pytest.mark.asyncio
    async def test_first_turn_is_not_generic_error(self):
        """没有缓存时 `_extract_final_reply` 会落到"抱歉，无法处理您的请求"。

        对一段纯闲聊来说这句话是错的（请求处理得很好，只是 render 卡了），
        所以 chat 分支必须在更早的地方就给出人格化的兜底。
        """
        messages, rendered = await _render(_chat_messages(with_history=False), intent="chat")
        final = _final_reply_from(messages, rendered)

        assert rendered
        assert final == rendered
        assert "无法处理您的请求" not in final

    @pytest.mark.asyncio
    @pytest.mark.parametrize("style", sorted(_RENDER_FALLBACK_LINES))
    async def test_each_persona_has_its_own_fallback(self, style):
        """兜底话术是人格可见输出 —— 三个角色各说各的，不能共用一句中性 IT 话。"""
        _, rendered = await _render(_chat_messages(), intent="chat", style=style)

        assert rendered == _RENDER_FALLBACK_LINES[style]

    def test_unknown_persona_falls_back_to_neutral(self):
        """CHARACTER_REGISTRY 加了新角色却忘了配兜底时，不能抛异常（CLAUDE.md 规则 1）。"""

        class _Stub:
            key = "不存在的角色"

        assert render_fallback_line(_Stub()) == _RENDER_FALLBACK_LINES["neutral"]


# ═══════════════════════════════════════════════════════════════════
# 契约 3：非 chat 分支一字不变
# ═══════════════════════════════════════════════════════════════════


class TestNonChatBranchUnchanged:
    """隐式终止路径的降级本来就对，这次修复不许碰它。"""

    @pytest.mark.asyncio
    async def test_degrades_to_cleaned_aggregator_text(self):
        """非 chat：仍降级为清理后的 Aggregator 文本，只是不再拿 render_input 当判据。"""
        _, rendered = await _render(_explore_messages(), intent="explore")

        assert "搜索超时" in rendered, "可读的原文应当保留"
        assert "|" not in rendered, "markdown 表格仍须清掉"
        assert rendered not in _RENDER_FALLBACK_LINES.values(), "非 chat 分支不该走兜底话术"

    @pytest.mark.asyncio
    async def test_returns_none_without_aggregator_text(self):
        """没有 Aggregator 文本 → 返回 (messages, None)，交给端点自己的兜底。行为不变。"""
        messages = _explore_messages(with_aggregator_text=False)
        messages_out, rendered = await _render(messages, intent="explore")

        assert rendered is None
        assert messages_out == messages


# ═══════════════════════════════════════════════════════════════════
# 判定侧契约：出口日志 ↔ 降级率信号
# ═══════════════════════════════════════════════════════════════════


class TestDegradeSignalContract:
    """`ChatResponse` 里没有"降级"字段，判定侧数降级率只有听日志一条路。

    于是"main.py 改了日志文案"与"判定侧悄悄漏计"之间没有任何约束 —— 这里补上。
    挂的是判定侧那个真实的 handler（不是字符串比对），文案一漂就红。
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("intent", ["chat", "explore"], ids=["chat兜底", "非chat降级"])
    async def test_both_exits_are_counted(self, intent):
        messages = _chat_messages() if intent == "chat" else _explore_messages()
        handler = _RenderSignalHandler()
        bgm_logger = logging.getLogger("bgm-agent")
        bgm_logger.addHandler(handler)
        try:
            await _render(list(messages), intent=intent)
        finally:
            bgm_logger.removeHandler(handler)

        assert handler.degraded, (
            f"{intent} 分支的降级没被判定侧听到 —— "
            f"main.py 的日志文案漂了？判定侧只认 {_DEGRADE_MARKERS}"
        )


# ═══════════════════════════════════════════════════════════════════
# 回归哨兵自检
# ═══════════════════════════════════════════════════════════════════


def test_leak_sentinel_still_catches_historical_leak():
    """`prompt脚手架` 这一类是修好之后更要留的东西 —— 它是唯一能证明"没复发"的。"""
    assert _LEAK_PATTERNS["prompt脚手架"].search(LEAKED_TEXT)


def test_fallback_lines_are_leak_free():
    """修复不能引入新的泄漏：兜底话术要被判定侧同一套模式判为干净。"""
    for key, line in _RENDER_FALLBACK_LINES.items():
        for pattern_name, pattern in _LEAK_PATTERNS.items():
            assert not pattern.search(line), (
                f"角色 {key} 的兜底话术命中泄漏模式「{pattern_name}」：{line!r}"
            )
