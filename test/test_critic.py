"""
Critic 节点测试 — Phase 6 统一架构

覆盖规则版（默认）和 LLM 版双模式。
仅 depth=="deep" 时注册 critic_node。
可独立运行: python -m pytest test/test_critic.py -v
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage, HumanMessage

from agent.orchestrate.nodes import critic_node
from agent.state import get_max_iterations
from test.conftest import make_mock_llm, make_state

import pytest

pytestmark = pytest.mark.asyncio

_DEEP_MAX = get_max_iterations("deep")


class TestCriticNodeRule:
    """规则版 Critic：零 Token 结构化检查"""

    @staticmethod
    def _set_rule_mode(mock_get_settings):
        s = MagicMock()
        s.CRITIC_MODE = "rule"
        s.LLM_CRITIC_MODEL = ""
        s.LLM_MODEL = "test"
        mock_get_settings.return_value = s

    async def test_revise_when_tools_returned_but_no_ai_response(self):
        state = make_state(iterations=1, depth="deep", messages=[
            SystemMessage(content="..."), HumanMessage(content="搜巨人"),
            AIMessage(content="", tool_calls=[{"name": "search", "args": {}, "id": "c1"}]),
            ToolMessage(content="找到 5 个结果", tool_call_id="c1"),
        ])
        result = await critic_node(state)
        assert result["critic_status"] == "REVISE"
        assert "回复缺失" in result.get("critic_feedback", "")

    @patch("agent.orchestrate.nodes.get_settings")
    async def test_revise_when_reply_too_short(self, mock_settings):
        self._set_rule_mode(mock_settings)
        state = make_state(iterations=1, depth="deep", messages=[
            SystemMessage(content="..."), HumanMessage(content="搜巨人"),
            AIMessage(content="", tool_calls=[{"name": "search", "args": {}, "id": "c1"}]),
            ToolMessage(content="找到 5 个结果", tool_call_id="c1"),
            AIMessage(content="好的。"),
        ])
        result = await critic_node(state)
        assert result["critic_status"] == "REVISE"
        assert "过短" in result.get("critic_feedback", "")

    async def test_pass_for_chitchat(self):
        state = make_state(iterations=1, depth="deep", messages=[
            SystemMessage(content="..."), HumanMessage(content="你好"),
            AIMessage(content="你好！有什么可以帮你的？"),
        ])
        assert (await critic_node(state))["critic_status"] == "PASS"

    @patch("agent.orchestrate.nodes.get_settings")
    async def test_pass_for_normal_reply(self, mock_settings):
        self._set_rule_mode(mock_settings)
        state = make_state(iterations=2, depth="deep", messages=[
            SystemMessage(content="..."), HumanMessage(content="搜巨人"),
            AIMessage(content="", tool_calls=[{"name": "search", "args": {}, "id": "c1"}]),
            ToolMessage(content="找到结果", tool_call_id="c1"),
            AIMessage(content="进击的巨人评分 8.5，排名 #15，经典热血战斗番。"),
        ])
        assert (await critic_node(state))["critic_status"] == "PASS"

    @patch("agent.orchestrate.nodes.get_settings")
    async def test_circuit_breaker(self, mock_settings):
        self._set_rule_mode(mock_settings)
        """熔断在 iterations >= _DEEP_MAX(=12) 时触发，强制 PASS + error_flag。"""
        for it, expect_breaker in ((_DEEP_MAX - 2, False), (_DEEP_MAX, True)):
            r = await critic_node(make_state(iterations=it, depth="deep"))
            assert r["critic_status"] == "PASS"
            if expect_breaker:
                assert r.get("error_flag") is True
            else:
                assert "error_flag" not in r

    async def test_feedback_uses_pipe_format(self):
        state = make_state(iterations=1, depth="deep", messages=[
            SystemMessage(content="..."), HumanMessage(content="搜"),
            AIMessage(content="", tool_calls=[{"name": "s", "args": {}, "id": "c1"}]),
            ToolMessage(content="数据", tool_call_id="c1"),
        ])
        fb = (await critic_node(state))["critic_feedback"]
        assert fb.count("|") >= 2

    # ── 逃逸舱：语义终端回复识别 ──────────────────────────

    @patch("agent.orchestrate.nodes.get_settings")
    async def test_pass_for_honest_not_found(self, mock_settings):
        self._set_rule_mode(mock_settings)
        state = make_state(iterations=2, depth="deep", messages=[
            SystemMessage(content="..."), HumanMessage(content="查上伊娜牡丹"),
            AIMessage(content="", tool_calls=[{"name": "search_bangumi_subject", "args": {}, "id": "c1"}]),
            ToolMessage(content="未找到匹配条目", tool_call_id="c1"),
            AIMessage(content="未找到上伊娜牡丹的相关信息。"),
        ])
        result = await critic_node(state)
        assert result["critic_status"] == "PASS", (
            f"诚实告知'未找到'应被逃逸舱保护为 PASS，实际: {result.get('critic_feedback')}"
        )

    @patch("agent.orchestrate.nodes.get_settings")
    async def test_pass_for_clarification(self, mock_settings):
        self._set_rule_mode(mock_settings)
        state = make_state(iterations=2, depth="deep", messages=[
            SystemMessage(content="..."), HumanMessage(content="评分多少"),
            AIMessage(content="", tool_calls=[{"name": "search_bangumi_subject", "args": {}, "id": "c1"}]),
            ToolMessage(content="找到 50 个结果", tool_call_id="c1"),
            AIMessage(content="您是指哪一部作品？巨人还是鲁路修？"),
        ])
        result = await critic_node(state)
        assert result["critic_status"] == "PASS", (
            f"追问用户应被逃逸舱保护为 PASS，实际: {result.get('critic_feedback')}"
        )

    @patch("agent.orchestrate.nodes.get_settings")
    async def test_pass_for_character_no_rating_explanation(self, mock_settings):
        self._set_rule_mode(mock_settings)
        state = make_state(iterations=2, depth="deep", messages=[
            SystemMessage(content="..."), HumanMessage(content="上伊那牡丹的评分"),
            AIMessage(content="", tool_calls=[{"name": "search_local_bangumi", "args": {}, "id": "c1"}]),
            ToolMessage(content="角色信息", tool_call_id="c1"),
            AIMessage(content="上伊那牡丹是一个角色，角色本身没有评分。她所属的作品评分可为您查询。"),
        ])
        result = await critic_node(state)
        assert result["critic_status"] == "PASS", (
            f"角色无评分说明应被逃逸舱保护为 PASS，实际: {result.get('critic_feedback')}"
        )

    async def test_still_revise_for_truly_short_reply(self):
        state = make_state(iterations=1, depth="deep", messages=[
            SystemMessage(content="..."), HumanMessage(content="搜巨人"),
            AIMessage(content="", tool_calls=[{"name": "search", "args": {}, "id": "c1"}]),
            ToolMessage(content="找到 5 个结果，评分 8.5", tool_call_id="c1"),
            AIMessage(content="好的。"),
        ])
        result = await critic_node(state)
        assert result["critic_status"] == "REVISE"

    # ── 重复工具调用检测 ──────────────────────────────────

    @patch("agent.orchestrate.nodes.get_settings")
    async def test_revise_on_duplicate_tool_calls(self, mock_settings):
        self._set_rule_mode(mock_settings)
        state = make_state(iterations=3, depth="deep", messages=[
            SystemMessage(content="..."), HumanMessage(content="今天有什么番"),
            AIMessage(content="", tool_calls=[{"name": "get_calendar", "args": {}, "id": "c1"}]),
            ToolMessage(content="API 错误", tool_call_id="c1"),
            AIMessage(content="", tool_calls=[{"name": "get_calendar", "args": {}, "id": "c2"}]),
            ToolMessage(content="API 错误", tool_call_id="c2"),
            AIMessage(content="让我重试..."),
        ])
        result = await critic_node(state)
        assert result["critic_status"] == "REVISE"
        assert "重复调用" in result.get("critic_feedback", "")

    @patch("agent.orchestrate.nodes.get_settings")
    async def test_pass_when_different_tool_calls(self, mock_settings):
        self._set_rule_mode(mock_settings)
        state = make_state(iterations=3, depth="deep", messages=[
            SystemMessage(content="..."), HumanMessage(content="今天有什么番"),
            AIMessage(content="", tool_calls=[{"name": "get_calendar", "args": {}, "id": "c1"}]),
            ToolMessage(content="API 错误", tool_call_id="c1"),
            AIMessage(content="", tool_calls=[{"name": "get_trending_subjects", "args": {}, "id": "c2"}]),
            ToolMessage(content="热门数据", tool_call_id="c2"),
            AIMessage(content="今日热门番剧有 A、B、C 三部，评分分别为 8.5、8.0、7.5 分。"),
        ])
        result = await critic_node(state)
        assert result["critic_status"] == "PASS"

    @patch("agent.orchestrate.nodes.get_settings")
    async def test_duplicate_detection_skips_single_tool_round(self, mock_settings):
        self._set_rule_mode(mock_settings)
        state = make_state(iterations=2, depth="deep", messages=[
            SystemMessage(content="..."), HumanMessage(content="今天有什么番"),
            AIMessage(content="", tool_calls=[{"name": "get_calendar", "args": {}, "id": "c1"}]),
            ToolMessage(content="今日放送数据...", tool_call_id="c1"),
            AIMessage(content="今日放送的番剧有 A、B、C 三部，其中 A 评分最高。"),
        ])
        result = await critic_node(state)
        assert result["critic_status"] == "PASS"

    # ── Critic 窗口缩窄测试 ──────────────────────────────────

    @patch("agent.orchestrate.nodes.get_settings")
    async def test_has_tool_msgs_scoped_to_current_iteration(self, mock_settings):
        self._set_rule_mode(mock_settings)
        state = make_state(iterations=3, depth="deep", messages=[
            SystemMessage(content="..."),
            HumanMessage(content="搜巨人"),
            AIMessage(content="", tool_calls=[{"name": "search", "args": {}, "id": "c1"}]),
            ToolMessage(content="找到 5 个结果", tool_call_id="c1"),
            AIMessage(content="进击的巨人评分 8.5，排名 #15。"),
            AIMessage(content="进击的巨人最终季评分 8.7 分，非常推荐。"),
        ])
        result = await critic_node(state)
        assert result["critic_status"] == "PASS"

    async def test_has_tool_msgs_detects_current_iteration(self):
        state = make_state(iterations=2, depth="deep", messages=[
            SystemMessage(content="..."),
            HumanMessage(content="搜巨人"),
            AIMessage(content="", tool_calls=[{"name": "search", "args": {}, "id": "c1"}]),
            ToolMessage(content="找到 5 个结果", tool_call_id="c1"),
            AIMessage(content="好的。"),
        ])
        result = await critic_node(state)
        assert result["critic_status"] == "REVISE"


class TestCriticNodeLLM:
    """LLM 版 Critic（CRITIC_MODE='llm'）"""

    @staticmethod
    def _set_mode(mock_get_settings, mode: str):
        s = MagicMock()
        s.CRITIC_MODE = mode
        s.LLM_CRITIC_MODEL = ""
        s.LLM_MODEL = "test"
        mock_get_settings.return_value = s

    @patch("agent.orchestrate.nodes.get_settings")
    @patch("agent.orchestrate.nodes.create_llm")
    async def test_pass(self, mock_llm, mock_settings):
        self._set_mode(mock_settings, "llm")
        mock_llm.return_value = make_mock_llm(content="PASS: 回复完整。")
        state = make_state(iterations=1, depth="deep", messages=[
            SystemMessage(content="..."), HumanMessage(content="评分"),
            AIMessage(content="评分 8.7"),
        ])
        r = await critic_node(state)
        assert r["critic_status"] == "PASS" and "PASS" in r["critic_feedback"]

    @patch("agent.orchestrate.nodes.get_settings")
    @patch("agent.orchestrate.nodes.create_llm")
    async def test_revise_with_feedback(self, mock_llm, mock_settings):
        self._set_mode(mock_settings, "llm")
        mock_llm.return_value = make_mock_llm(
            content="REVISE: 缺少评分 | 调用 get_detail | 缺失评分"
        )
        state = make_state(iterations=1, depth="deep", messages=[
            SystemMessage(content="..."), HumanMessage(content="评分"),
            AIMessage(content="还不错"),
        ])
        r = await critic_node(state)
        assert r["critic_status"] == "REVISE"
        assert "get_detail" in r["critic_feedback"]

    @patch("agent.orchestrate.nodes.get_settings")
    @patch("agent.orchestrate.nodes.create_llm")
    async def test_escape_hatch(self, mock_llm, mock_settings):
        self._set_mode(mock_settings, "llm")
        mock_llm.return_value = make_mock_llm(
            content="PASS: 助手已调用工具并如实告知数据不存在。"
        )
        state = make_state(iterations=2, depth="deep", messages=[
            SystemMessage(content="..."), HumanMessage(content="查角色"),
            AIMessage(content="", tool_calls=[{"name": "get_comments", "args": {}, "id": "c1"}]),
            ToolMessage(content="暂无数据", tool_call_id="c1"),
            AIMessage(content="抱歉，暂无相关信息。"),
        ])
        r = await critic_node(state)
        assert r["critic_status"] == "PASS"

    @patch("agent.orchestrate.nodes.get_settings")
    @patch("agent.orchestrate.nodes.create_llm")
    async def test_default_pass_on_llm_error(self, mock_llm, mock_settings):
        self._set_mode(mock_settings, "llm")
        mock = make_mock_llm()
        mock.ainvoke.side_effect = RuntimeError("timeout")
        mock_llm.return_value = mock
        state = make_state(iterations=1, depth="deep", messages=[
            SystemMessage(content="..."), HumanMessage(content="t"), AIMessage(content="r"),
        ])
        assert (await critic_node(state))["critic_status"] == "PASS"

    @patch("agent.orchestrate.nodes.get_settings")
    @patch("agent.orchestrate.nodes.create_llm")
    async def test_default_pass_on_unexpected_output(self, mock_llm, mock_settings):
        self._set_mode(mock_settings, "llm")
        mock_llm.return_value = make_mock_llm(content="UNKNOWN xyz")
        state = make_state(iterations=1, depth="deep", messages=[
            SystemMessage(content="..."), HumanMessage(content="t"), AIMessage(content="r"),
        ])
        assert (await critic_node(state))["critic_status"] == "PASS"

    @patch("agent.orchestrate.nodes.get_settings")
    @patch("agent.orchestrate.nodes.create_llm")
    async def test_circuit_breaker_in_llm_mode(self, mock_llm, mock_settings):
        """LLM 模式下熔断应在 iterations >= max 时直接 PASS，不调 LLM。"""
        self._set_mode(mock_settings, "llm")
        r = await critic_node(make_state(iterations=_DEEP_MAX, depth="deep"))
        assert r["critic_status"] == "PASS" and r["error_flag"] is True
        mock_llm.assert_not_called()
