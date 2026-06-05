"""
Critic 节点测试

覆盖规则版（默认）和 LLM 版双模式。
可独立运行: python -m pytest test/test_critic.py -v
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage, HumanMessage

from agent.nodes import critic_node
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
        for it in (3, 5):
            r = critic_node(make_state(iterations=it))
            assert r["critic_status"] == "PASS" and r["error_flag"] is True

    def test_does_not_touch_last_tool_calls(self):
        state = make_state(iterations=1, last_tool_calls=[{"name": "s", "args": {}, "id": "c1"}])
        assert "last_tool_calls" not in critic_node(state)

    def test_feedback_uses_pipe_format(self):
        state = make_state(iterations=1, messages=[
            SystemMessage(content="..."), HumanMessage(content="搜"),
            AIMessage(content="", tool_calls=[{"name": "s", "args": {}, "id": "c1"}]),
            ToolMessage(content="数据", tool_call_id="c1"),
        ])
        fb = critic_node(state)["critic_feedback"]
        assert fb.count("|") >= 2


class TestCriticNodeLLM:
    """LLM 版 Critic（CRITIC_MODE='llm'）"""

    @staticmethod
    def _set_mode(mock_get_settings, mode: str):
        s = MagicMock()
        s.CRITIC_MODE = mode
        s.LLM_CRITIC_MODEL = ""
        s.LLM_MODEL = "test"
        mock_get_settings.return_value = s

    @patch("agent.nodes.get_settings")
    @patch("agent.nodes.create_llm")
    def test_pass(self, mock_llm, mock_settings):
        self._set_mode(mock_settings, "llm")
        mock_llm.return_value = make_mock_llm(content="PASS: 回复完整。")
        state = make_state(iterations=1, messages=[
            SystemMessage(content="..."), HumanMessage(content="评分"),
            AIMessage(content="评分 8.7"),
        ])
        r = critic_node(state)
        assert r["critic_status"] == "PASS" and "PASS" in r["critic_feedback"]

    @patch("agent.nodes.get_settings")
    @patch("agent.nodes.create_llm")
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

    @patch("agent.nodes.get_settings")
    @patch("agent.nodes.create_llm")
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

    @patch("agent.nodes.get_settings")
    @patch("agent.nodes.create_llm")
    def test_default_pass_on_llm_error(self, mock_llm, mock_settings):
        self._set_mode(mock_settings, "llm")
        mock = make_mock_llm()
        mock.invoke.side_effect = RuntimeError("timeout")
        mock_llm.return_value = mock
        state = make_state(iterations=1, messages=[
            SystemMessage(content="..."), HumanMessage(content="t"), AIMessage(content="r"),
        ])
        assert critic_node(state)["critic_status"] == "PASS"

    @patch("agent.nodes.get_settings")
    @patch("agent.nodes.create_llm")
    def test_default_pass_on_unexpected_output(self, mock_llm, mock_settings):
        self._set_mode(mock_settings, "llm")
        mock_llm.return_value = make_mock_llm(content="UNKNOWN xyz")
        state = make_state(iterations=1, messages=[
            SystemMessage(content="..."), HumanMessage(content="t"), AIMessage(content="r"),
        ])
        assert critic_node(state)["critic_status"] == "PASS"

    @patch("agent.nodes.get_settings")
    @patch("agent.nodes.create_llm")
    def test_circuit_breaker_in_llm_mode(self, mock_llm, mock_settings):
        self._set_mode(mock_settings, "llm")
        r = critic_node(make_state(iterations=3))
        assert r["critic_status"] == "PASS" and r["error_flag"] is True
        mock_llm.assert_not_called()
