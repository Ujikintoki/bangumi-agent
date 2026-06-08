"""
Critic 节点测试

覆盖规则版（默认）和 LLM 版双模式。
可独立运行: python -m pytest test/test_critic.py -v
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage, HumanMessage

from agent.research.nodes import critic_node
from test.conftest import make_mock_llm, make_state


class TestCriticNodeRule:
    """规则版 Critic（默认）：零 Token 结构化检查"""

    def test_revise_when_tools_returned_but_no_ai_response(self):
        state = make_state(iterations=1, messages=[
            SystemMessage(content="..."), HumanMessage(content="搜巨人"),
            AIMessage(content="", tool_calls=[{"name": "search", "args": {}, "id": "c1"}]),
            ToolMessage(content="找到 5 个结果", tool_call_id="c1"),
        ])
        result = critic_node(state)
        assert result["critic_status"] == "REVISE"
        assert "回复缺失" in result.get("critic_feedback", "")

    def test_revise_when_reply_too_short(self):
        state = make_state(iterations=1, messages=[
            SystemMessage(content="..."), HumanMessage(content="搜巨人"),
            AIMessage(content="", tool_calls=[{"name": "search", "args": {}, "id": "c1"}]),
            ToolMessage(content="找到 5 个结果", tool_call_id="c1"),
            AIMessage(content="好的。"),
        ])
        result = critic_node(state)
        assert result["critic_status"] == "REVISE"
        assert "过短" in result.get("critic_feedback", "")

    def test_pass_for_chitchat(self):
        state = make_state(iterations=1, messages=[
            SystemMessage(content="..."), HumanMessage(content="你好"),
            AIMessage(content="你好！有什么可以帮你的？"),
        ])
        assert critic_node(state)["critic_status"] == "PASS"

    def test_pass_for_normal_reply(self):
        state = make_state(iterations=2, messages=[
            SystemMessage(content="..."), HumanMessage(content="搜巨人"),
            AIMessage(content="", tool_calls=[{"name": "search", "args": {}, "id": "c1"}]),
            ToolMessage(content="找到结果", tool_call_id="c1"),
            AIMessage(content="进击的巨人评分 8.5，排名 #15，经典热血战斗番。"),
        ])
        assert critic_node(state)["critic_status"] == "PASS"

    def test_circuit_breaker(self):
        for it in (10, 12):
            r = critic_node(make_state(iterations=it))
            assert r["critic_status"] == "PASS" and r["error_flag"] is True

    def test_feedback_uses_pipe_format(self):
        state = make_state(iterations=1, messages=[
            SystemMessage(content="..."), HumanMessage(content="搜"),
            AIMessage(content="", tool_calls=[{"name": "s", "args": {}, "id": "c1"}]),
            ToolMessage(content="数据", tool_call_id="c1"),
        ])
        fb = critic_node(state)["critic_feedback"]
        assert fb.count("|") >= 2

    # ── 逃逸舱：语义终端回复识别 ──────────────────────────

    def test_pass_for_honest_not_found(self):
        """搜索空结果 + 诚实告知 → PASS（不因字数少而 REVISE）"""
        state = make_state(iterations=2, messages=[
            SystemMessage(content="..."), HumanMessage(content="查上伊娜牡丹"),
            AIMessage(content="", tool_calls=[{"name": "search_bangumi_subject", "args": {}, "id": "c1"}]),
            ToolMessage(content="未找到匹配条目", tool_call_id="c1"),
            AIMessage(content="未找到上伊娜牡丹的相关信息。"),
        ])
        result = critic_node(state)
        assert result["critic_status"] == "PASS", (
            f"诚实告知'未找到'应被逃逸舱保护为 PASS，实际: {result.get('critic_feedback')}"
        )

    def test_pass_for_clarification(self):
        """追问用户澄清意图 → PASS"""
        state = make_state(iterations=2, messages=[
            SystemMessage(content="..."), HumanMessage(content="评分多少"),
            AIMessage(content="", tool_calls=[{"name": "search_bangumi_subject", "args": {}, "id": "c1"}]),
            ToolMessage(content="找到 50 个结果", tool_call_id="c1"),
            AIMessage(content="您是指哪一部作品？巨人还是鲁路修？"),
        ])
        result = critic_node(state)
        assert result["critic_status"] == "PASS", (
            f"追问用户应被逃逸舱保护为 PASS，实际: {result.get('critic_feedback')}"
        )

    def test_pass_for_character_no_rating_explanation(self):
        """说明角色没有评分 → PASS"""
        state = make_state(iterations=2, messages=[
            SystemMessage(content="..."), HumanMessage(content="上伊那牡丹的评分"),
            AIMessage(content="", tool_calls=[{"name": "search_local_bangumi", "args": {}, "id": "c1"}]),
            ToolMessage(content="角色信息", tool_call_id="c1"),
            AIMessage(content="上伊那牡丹是一个角色，角色本身没有评分。她所属的作品评分可为您查询。"),
        ])
        result = critic_node(state)
        assert result["critic_status"] == "PASS", (
            f"角色无评分说明应被逃逸舱保护为 PASS，实际: {result.get('critic_feedback')}"
        )

    def test_still_revise_for_truly_short_reply(self):
        """有工具数据但回复真正过短且非终端语义 → 仍然 REVISE"""
        state = make_state(iterations=1, messages=[
            SystemMessage(content="..."), HumanMessage(content="搜巨人"),
            AIMessage(content="", tool_calls=[{"name": "search", "args": {}, "id": "c1"}]),
            ToolMessage(content="找到 5 个结果，评分 8.5", tool_call_id="c1"),
            AIMessage(content="好的。"),
        ])
        result = critic_node(state)
        assert result["critic_status"] == "REVISE", (
            "仅说'好的'不包含追问/澄清/诚实告知，应继续 REVISE"
        )


    # ── 重复工具调用检测 ──────────────────────────────────

    def test_revise_on_duplicate_tool_calls(self):
        """连续两轮调用相同工具且参数一致 → REVISE"""
        state = make_state(iterations=3, messages=[
            SystemMessage(content="..."), HumanMessage(content="今天有什么番"),
            AIMessage(content="", tool_calls=[{"name": "get_calendar", "args": {}, "id": "c1"}]),
            ToolMessage(content="API 错误", tool_call_id="c1"),
            AIMessage(content="", tool_calls=[{"name": "get_calendar", "args": {}, "id": "c2"}]),
            ToolMessage(content="API 错误", tool_call_id="c2"),
            AIMessage(content="让我重试..."),
        ])
        result = critic_node(state)
        assert result["critic_status"] == "REVISE", (
            f"重复调用 get_calendar 应被检测，实际: {result.get('critic_feedback')}"
        )
        assert "重复调用" in result.get("critic_feedback", "")

    def test_pass_when_different_tool_calls(self):
        """连续两轮调用不同工具 → 不触发重复检测"""
        state = make_state(iterations=3, messages=[
            SystemMessage(content="..."), HumanMessage(content="今天有什么番"),
            AIMessage(content="", tool_calls=[{"name": "get_calendar", "args": {}, "id": "c1"}]),
            ToolMessage(content="API 错误", tool_call_id="c1"),
            AIMessage(content="", tool_calls=[{"name": "get_trending_topics", "args": {}, "id": "c2"}]),
            ToolMessage(content="热门数据", tool_call_id="c2"),
            AIMessage(content="今日热门番剧有 A、B、C 三部，评分分别为 8.5、8.0、7.5 分。"),
        ])
        result = critic_node(state)
        assert result["critic_status"] == "PASS", (
            f"不同工具调用不应触发重复检测，实际: {result.get('critic_feedback')}"
        )

    def test_duplicate_detection_skips_single_tool_round(self):
        """只有一轮工具调用 → 不触发重复检测（正常 PASS）"""
        state = make_state(iterations=2, messages=[
            SystemMessage(content="..."), HumanMessage(content="今天有什么番"),
            AIMessage(content="", tool_calls=[{"name": "get_calendar", "args": {}, "id": "c1"}]),
            ToolMessage(content="今日放送数据...", tool_call_id="c1"),
            AIMessage(content="今日放送的番剧有 A、B、C 三部，其中 A 评分最高。"),
        ])
        result = critic_node(state)
        assert result["critic_status"] == "PASS", (
            f"单轮工具调用不应触发重复检测，实际: {result.get('critic_feedback')}"
        )


class TestCriticNodeLLM:
    """LLM 版 Critic（CRITIC_MODE='llm'）"""

    @staticmethod
    def _set_mode(mock_get_settings, mode: str):
        s = MagicMock()
        s.CRITIC_MODE = mode
        s.LLM_CRITIC_MODEL = ""
        s.LLM_MODEL = "test"
        mock_get_settings.return_value = s

    @patch("agent.research.nodes.get_settings")
    @patch("agent.research.nodes.create_llm")
    def test_pass(self, mock_llm, mock_settings):
        self._set_mode(mock_settings, "llm")
        mock_llm.return_value = make_mock_llm(content="PASS: 回复完整。")
        state = make_state(iterations=1, messages=[
            SystemMessage(content="..."), HumanMessage(content="评分"),
            AIMessage(content="评分 8.7"),
        ])
        r = critic_node(state)
        assert r["critic_status"] == "PASS" and "PASS" in r["critic_feedback"]

    @patch("agent.research.nodes.get_settings")
    @patch("agent.research.nodes.create_llm")
    def test_revise_with_feedback(self, mock_llm, mock_settings):
        self._set_mode(mock_settings, "llm")
        mock_llm.return_value = make_mock_llm(
            content="REVISE: 缺少评分 | 调用 get_detail | 缺失评分"
        )
        state = make_state(iterations=1, messages=[
            SystemMessage(content="..."), HumanMessage(content="评分"),
            AIMessage(content="还不错"),
        ])
        r = critic_node(state)
        assert r["critic_status"] == "REVISE"
        assert "get_detail" in r["critic_feedback"]

    @patch("agent.research.nodes.get_settings")
    @patch("agent.research.nodes.create_llm")
    def test_escape_hatch(self, mock_llm, mock_settings):
        """逃逸舱：API 无数据 → 强制 PASS"""
        self._set_mode(mock_settings, "llm")
        mock_llm.return_value = make_mock_llm(
            content="PASS: 助手已调用工具并如实告知数据不存在。"
        )
        state = make_state(iterations=2, messages=[
            SystemMessage(content="..."), HumanMessage(content="查角色"),
            AIMessage(content="", tool_calls=[{"name": "get_comments", "args": {}, "id": "c1"}]),
            ToolMessage(content="暂无数据", tool_call_id="c1"),
            AIMessage(content="抱歉，暂无相关信息。"),
        ])
        r = critic_node(state)
        assert r["critic_status"] == "PASS", f"逃逸舱失效！got: {r.get('critic_feedback')}"

    @patch("agent.research.nodes.get_settings")
    @patch("agent.research.nodes.create_llm")
    def test_default_pass_on_llm_error(self, mock_llm, mock_settings):
        self._set_mode(mock_settings, "llm")
        mock = make_mock_llm()
        mock.invoke.side_effect = RuntimeError("timeout")
        mock_llm.return_value = mock
        state = make_state(iterations=1, messages=[
            SystemMessage(content="..."), HumanMessage(content="t"), AIMessage(content="r"),
        ])
        assert critic_node(state)["critic_status"] == "PASS"

    @patch("agent.research.nodes.get_settings")
    @patch("agent.research.nodes.create_llm")
    def test_default_pass_on_unexpected_output(self, mock_llm, mock_settings):
        self._set_mode(mock_settings, "llm")
        mock_llm.return_value = make_mock_llm(content="UNKNOWN xyz")
        state = make_state(iterations=1, messages=[
            SystemMessage(content="..."), HumanMessage(content="t"), AIMessage(content="r"),
        ])
        assert critic_node(state)["critic_status"] == "PASS"

    @patch("agent.research.nodes.get_settings")
    @patch("agent.research.nodes.create_llm")
    def test_circuit_breaker_in_llm_mode(self, mock_llm, mock_settings):
        self._set_mode(mock_settings, "llm")
        r = critic_node(make_state(iterations=10))
        assert r["critic_status"] == "PASS" and r["error_flag"] is True
        mock_llm.assert_not_called()
