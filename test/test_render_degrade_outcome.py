"""模型未收尾时的降级 —— 回归测试（离线、确定、不调 LLM）。

**背景（2026-09-14，测 ConnectError 修复时顺带抓到）**：熔断（连续空搜索 /
重复调用 / 迭代上限）在 tool_node 之后直接掐断图，最后一条消息是 ToolMessage，
模型压根没机会说话。端点 `_extract_final_reply` 一看"有工具结果但没有 AI 文本"，
就吐出「工具执行完成但未能生成文本回复，请重试或换个方式提问。」

这不是 ConnectError 造成的：冻结样本里 `A5-deep深入` 修复前有 6 次**成功**调用，
照样是这句。真正的问题是把"没查到"这件事讲成了一句技术黑话。

修复后要锁住的契约：

1. 走到这条路 **必定给出回复**，绝不返回 None（否则又掉回那句罐头话）；
2. 回复要**分层说实话** —— 够不着 / 没找着 / 有数据但没接住，是三件不同的事，
   说错任何一件都是对用户撒谎；
3. 交付方式是**追加** AIMessage，不是替换 —— 此刻最后一条 AIMessage 是空
   content + tool_calls，替换会把它的 ToolMessage 变成孤儿（配对丢失）；
4. 结局话术是人格可见输出：三个角色各说各的，且不含机制词（接口/系统/工具）。

全程离线：`main.render_reply` 被 mock，不联网、不花钱、不碰数据库。
"""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import main
from agent.persona.render import (
    _RENDER_FALLBACK_LINES,
    _TOOL_BLOCKED_LINES,
    _TOOL_EMPTY_LINES,
    tool_outcome_line,
)
from eval.reply_quality_eval import (
    _DEGRADE_MARKERS,
    _LEAK_PATTERNS,
    _RenderSignalHandler,
    _is_error_reply,
)

# ═══════════════════════════════════════════════════════════════════
# 夹具：三种结局的真实 payload 形状
# ═══════════════════════════════════════════════════════════════════

# 2026-09-14 实测：ConnectError 经 tools/bgm_tools.py 包装后的真实形状
BLOCKED_PAYLOAD = json.dumps(
    {"_error": "搜索失败。连接失败（ConnectError）(path=/p1/search/subjects)"},
    ensure_ascii=False,
)
# 熔断器认的那种空结果
EMPTY_PAYLOAD = json.dumps({"results": [], "total": 0}, ensure_ascii=False)
# 有真东西
DATA_PAYLOAD = json.dumps(
    {"results": [{"id": 265, "name": "新世紀エヴァンゲリオン"}], "total": 1},
    ensure_ascii=False,
)
# get_user_profile 那类部分失败：错误藏在 {key}_error 子键里
PARTIAL_PAYLOAD = json.dumps(
    {"username": "nobody", "profile_error": "用户不存在"}, ensure_ascii=False
)


def _messages(*payloads: str, name: str = "search_bangumi_subject") -> list:
    """构造"模型调了工具、拿到结果、然后一个字都没说"的转录。"""
    tool_calls = [
        {"name": name, "args": {"keyword": "x"}, "id": f"call_{i}", "type": "tool_call"}
        for i in range(len(payloads))
    ]
    msgs: list = [
        HumanMessage(content="进击的巨人评分多少"),
        AIMessage(content="", tool_calls=tool_calls),
    ]
    msgs += [
        ToolMessage(content=p, name=name, tool_call_id=f"call_{i}")
        for i, p in enumerate(payloads)
    ]
    return msgs


async def _followup(messages: list, *, style: str = "bangumi", render_returns):
    with patch("main.render_reply", AsyncMock(return_value=render_returns)):
        return await main._render_no_text_followup(
            messages, "进击的巨人评分多少", style, "fast"
        )


# ═══════════════════════════════════════════════════════════════════
# 契约 2：结局分层 —— 三件事不能混着说
# ═══════════════════════════════════════════════════════════════════


class TestToolOutcome:
    """倒推工具到底给了什么。说错一件就是撒谎，所以逐形状钉死。"""

    @pytest.mark.parametrize(
        "payload,expected",
        [
            (BLOCKED_PAYLOAD, "error"),
            (PARTIAL_PAYLOAD, "error"),          # {key}_error 子键同样是没数据
            (EMPTY_PAYLOAD, "empty"),
            ('{"results": [], "total": 0}', "empty"),
            ("[]", "empty"),
            ("null", "empty"),
            ('{"total": 0}', "empty"),           # 只有标量，没有可供用户看的实体
            (DATA_PAYLOAD, "data"),
            ('[{"id": 265}]', "data"),
            ("纯文本结果", "data"),               # 非 JSON 但有字
            ("", "empty"),
        ],
    )
    def test_payload_kind(self, payload, expected):
        assert main._tool_payload_kind(payload) == expected

    def test_any_data_wins(self):
        """混合结果：只要有一条可用，就不该说"没查到"——那是把有用的东西说没了。"""
        assert main._tool_outcome(_messages(BLOCKED_PAYLOAD, DATA_PAYLOAD)) == "has_data"

    def test_all_errors_is_blocked(self):
        assert main._tool_outcome(_messages(BLOCKED_PAYLOAD, BLOCKED_PAYLOAD)) == "blocked"

    def test_errors_beat_empty(self):
        """error + empty 混着：够不着是更准确的交代（empty 会让你以为站里没这东西）。"""
        assert main._tool_outcome(_messages(BLOCKED_PAYLOAD, EMPTY_PAYLOAD)) == "blocked"

    def test_empty_only(self):
        assert main._tool_outcome(_messages(EMPTY_PAYLOAD)) == "empty"

    def test_no_tool_messages_is_its_own_outcome(self):
        """一次都没查过 ≠ 查了没有。说"翻了一圈没找着"同样是撒谎。"""
        assert main._tool_outcome([HumanMessage(content="你好")]) == "no_tools"


# ═══════════════════════════════════════════════════════════════════
# 契约 1 + 3：必定给回复，且是追加不是替换
# ═══════════════════════════════════════════════════════════════════


class TestNoTextFollowup:
    @pytest.mark.asyncio
    async def test_never_returns_none(self):
        """返回 None 就掉回端点那句「工具执行完成但未能生成文本回复」。"""
        messages, reply = await _followup(_messages(BLOCKED_PAYLOAD), render_returns=None)

        assert reply and isinstance(reply, str)
        assert main._extract_final_reply(messages) == reply

    @pytest.mark.asyncio
    async def test_never_returns_none_even_if_render_explodes(self):
        """render 抛异常也不能穿透（CLAUDE.md 规则 1：不抛异常）。"""
        with patch("main.render_reply", AsyncMock(side_effect=RuntimeError("boom"))):
            messages, reply = await main._render_no_text_followup(
                _messages(BLOCKED_PAYLOAD), "进击的巨人评分多少", "bangumi", "fast"
            )

        assert reply == _TOOL_BLOCKED_LINES["bangumi"]
        assert main._extract_final_reply(messages) == reply

    @pytest.mark.asyncio
    async def test_appends_and_keeps_tool_pairing(self):
        """契约 3：追加而非替换。

        替换最后一条 AIMessage（空 content + tool_calls）会把它的 ToolMessage
        变成孤儿 —— 下轮请求要么被严格 API 判 400，要么白白丢掉工具上下文。
        """
        messages = _messages(BLOCKED_PAYLOAD, EMPTY_PAYLOAD)
        out, _ = await _followup(messages, render_returns="（交代）")

        assert len(out) == len(messages) + 1
        assert out[: len(messages)] == messages, "原有消息一条都不许动"

        # 配对仍然完整：每个 ToolMessage 的 tool_call_id 都有 AIMessage 认领
        declared = {
            tc["id"]
            for m in out
            if isinstance(m, AIMessage)
            for tc in (m.tool_calls or [])
        }
        for m in out:
            if isinstance(m, ToolMessage):
                assert m.tool_call_id in declared, f"{m.tool_call_id} 成了孤儿"

    @pytest.mark.asyncio
    async def test_blocked_asks_to_come_back_later(self):
        """够不着 → 该让用户待会儿再来，而不是让他换说法（换了也没用）。"""
        _, reply = await _followup(_messages(BLOCKED_PAYLOAD), render_returns=None)
        assert reply == _TOOL_BLOCKED_LINES["bangumi"]

    @pytest.mark.asyncio
    async def test_empty_suggests_rephrasing(self):
        _, reply = await _followup(_messages(EMPTY_PAYLOAD), render_returns=None)
        assert reply == _TOOL_EMPTY_LINES["bangumi"]

    @pytest.mark.asyncio
    async def test_has_data_does_not_claim_no_result(self):
        """有数据只是没接住 —— 说"没查到"是撒谎，说"我走神了"才对。"""
        messages = _messages(DATA_PAYLOAD)
        _, reply = await _followup(messages, render_returns="不该被用到")

        assert reply == _RENDER_FALLBACK_LINES["bangumi"]
        assert reply not in _TOOL_EMPTY_LINES.values()
        assert reply not in _TOOL_BLOCKED_LINES.values()

    @pytest.mark.asyncio
    async def test_has_data_skips_render_entirely(self):
        """有数据时没有"数据"要转述，硬渲染只会逼模型去编 —— 所以根本不调。"""
        with patch("main.render_reply", AsyncMock(return_value="不该被用到")) as mocked:
            await main._render_no_text_followup(
                _messages(DATA_PAYLOAD), "进击的巨人评分多少", "bangumi", "fast"
            )

        assert not mocked.called

    @pytest.mark.asyncio
    async def test_never_looked_does_not_claim_it_looked(self):
        """模型空手而归（无 tool_calls、无文本）→ "我没接住"，不是"没找着"。"""
        messages = [HumanMessage(content="进击的巨人评分多少"), AIMessage(content="")]
        with patch("main.render_reply", AsyncMock(return_value="不该被用到")) as mocked:
            out, reply = await main._render_no_text_followup(
                messages, "进击的巨人评分多少", "bangumi", "fast"
            )

        assert reply == _RENDER_FALLBACK_LINES["bangumi"]
        assert reply not in _TOOL_EMPTY_LINES.values()
        assert not mocked.called

    @pytest.mark.asyncio
    @pytest.mark.parametrize("style", sorted(_TOOL_BLOCKED_LINES))
    async def test_render_gets_the_right_outcome(self, style):
        """喂给 render 的是"发生了什么"，不是通用模板 —— 两个结局文案必须不同。"""
        seen: list[str] = []

        async def _capture(render_input, **kwargs):
            seen.append(render_input)
            return "（渲染结果）"

        with patch("main.render_reply", AsyncMock(side_effect=_capture)):
            await main._render_no_text_followup(
                _messages(BLOCKED_PAYLOAD), "进击的巨人评分多少", style, "fast"
            )
            await main._render_no_text_followup(
                _messages(EMPTY_PAYLOAD), "进击的巨人评分多少", style, "fast"
            )

        assert len(seen) == 2
        assert seen[0] != seen[1]
        assert "没连上" in seen[0]
        assert "没有找到" in seen[1]
        # render_input 是喂给模型的，不是给用户看的 —— 但绝不能含"可以编"的余地
        for text in seen:
            assert "不要补充" in text


# ═══════════════════════════════════════════════════════════════════
# 契约 4：结局话术是人格可见输出
# ═══════════════════════════════════════════════════════════════════


class TestOutcomeLines:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("style", sorted(_TOOL_BLOCKED_LINES))
    async def test_each_persona_says_its_own_thing(self, style):
        _, blocked = await _followup(
            _messages(BLOCKED_PAYLOAD), style=style, render_returns=None
        )
        _, empty = await _followup(
            _messages(EMPTY_PAYLOAD), style=style, render_returns=None
        )

        assert blocked == _TOOL_BLOCKED_LINES[style]
        assert empty == _TOOL_EMPTY_LINES[style]

    def test_unknown_persona_falls_back_to_neutral(self):
        """新角色忘了配话术时不能抛异常（CLAUDE.md 规则 1）。"""

        class _Stub:
            key = "不存在的角色"

        assert tool_outcome_line(_Stub(), blocked=True) == _TOOL_BLOCKED_LINES["neutral"]
        assert tool_outcome_line(_Stub(), blocked=False) == _TOOL_EMPTY_LINES["neutral"]

    @pytest.mark.parametrize(
        "table_name", ["_TOOL_BLOCKED_LINES", "_TOOL_EMPTY_LINES"]
    )
    def test_lines_are_leak_free(self, table_name):
        """与 chat 兜底同一套判定侧模式：不许泄漏、不许 emoji。"""
        table = {"_TOOL_BLOCKED_LINES": _TOOL_BLOCKED_LINES,
                 "_TOOL_EMPTY_LINES": _TOOL_EMPTY_LINES}[table_name]
        for key, line in table.items():
            for pattern_name, pattern in _LEAK_PATTERNS.items():
                assert not pattern.search(line), (
                    f"{table_name}[{key}] 命中泄漏模式「{pattern_name}」：{line!r}"
                )

    @pytest.mark.parametrize(
        "table_name", ["_TOOL_BLOCKED_LINES", "_TOOL_EMPTY_LINES"]
    )
    def test_lines_avoid_mechanism_words(self, table_name):
        """住在站里的角色不该知道"接口"是什么 —— 这是 2026-09-14 实测漏过一次的。"""
        table = {"_TOOL_BLOCKED_LINES": _TOOL_BLOCKED_LINES,
                 "_TOOL_EMPTY_LINES": _TOOL_EMPTY_LINES}[table_name]
        banned = ("接口", "系统", "工具", "数据库", "模型", "API", "服务器")
        for key, line in table.items():
            for word in banned:
                assert word not in line, f"{table_name}[{key}] 出现机制词「{word}」：{line!r}"


# ═══════════════════════════════════════════════════════════════════
# 判定侧契约：出口日志 ↔ 降级率信号
# ═══════════════════════════════════════════════════════════════════


class TestDegradeSignalContract:
    """`ChatResponse` 里没有"降级"字段，判定侧数降级率只有听日志一条路。

    2026-09-14 新增的出口**一条都不能漏计**：漏一条 = 修好一个缺陷、指标上反而少了
    两条失败，失败从"看得见"变成"看不见"。挂的是判定侧那个真实的 handler
    （不是字符串比对），main.py 的日志文案一漂就红。
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "payload,render_returns,exit_name",
        [
            (BLOCKED_PAYLOAD, "（人格层交代）", "blocked → 人格层交代"),
            (EMPTY_PAYLOAD, "（人格层交代）", "empty → 人格层交代"),
            (BLOCKED_PAYLOAD, None, "blocked → 结局话术兜底"),
            (DATA_PAYLOAD, None, "has_data → 走神话术"),
        ],
    )
    async def test_every_exit_is_counted(self, payload, render_returns, exit_name):
        handler = _RenderSignalHandler()
        bgm_logger = logging.getLogger("bgm-agent")
        bgm_logger.addHandler(handler)
        try:
            await _followup(_messages(payload), render_returns=render_returns)
        finally:
            bgm_logger.removeHandler(handler)

        assert handler.degraded, (
            f"{exit_name} 这条出口没被判定侧听到 —— main.py 的日志文案漂了？"
            f"判定侧只认 {_DEGRADE_MARKERS}"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "payload,render_returns",
        [(BLOCKED_PAYLOAD, "（人格层交代）"), (DATA_PAYLOAD, None)],
        ids=["人格层交代", "走神话术"],
    )
    async def test_new_exits_are_not_mistaken_for_canned_errors(
        self, payload, render_returns
    ):
        """反方向也要锁：新出口产出的是人格话，不该落进「报错回复」那一桶。

        落进去就是重复计数 —— 同一轮既算报错又算降级，两个数字都失真。
        """
        _, reply = await _followup(_messages(payload), render_returns=render_returns)

        assert not _is_error_reply(reply), f"新出口被误判成 canned 报错：{reply!r}"
