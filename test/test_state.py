"""
AgentState 结构 & 路由测试

验证 State 字段完整性、条件边路由逻辑、辅助函数。
可独立运行: python -m pytest test/test_state.py -v
"""

from __future__ import annotations

from langgraph.graph import END

from agent.graph import _MAX_ITERATIONS, route_after_critic, route_after_reasoning
from agent.nodes import _extract_user_input
from test.conftest import make_state


class TestAgentStateStructure:
    """AgentState 字段完整性"""

    def test_all_required_keys_present(self):
        state = make_state()
        for key in ("messages", "iterations", "critic_status", "critic_feedback",
                     "last_tool_calls", "query_intent", "session_id", "user_id", "error_flag"):
            assert key in state, f"缺少必需字段: {key}"

    def test_needs_tool_removed(self):
        assert "needs_tool" not in make_state()

    def test_defaults(self):
        state = make_state()
        assert state["last_tool_calls"] == []
        assert state["query_intent"] == "unknown"
        assert state["critic_feedback"] == ""
        assert state["session_id"] == "test-session"
        assert state["user_id"] == "test-user"


class TestRouteAfterReasoning:
    def test_routes_to_tool_when_calls_present(self):
        assert route_after_reasoning(make_state(
            last_tool_calls=[{"name": "search", "args": {}, "id": "c1"}],
        )) == "tool_node"

    def test_routes_to_critic_when_calls_empty(self):
        assert route_after_reasoning(make_state(last_tool_calls=[])) == "critic_node"

    def test_routes_to_critic_when_calls_missing(self):
        state = make_state(last_tool_calls=None)  # type: ignore
        del state["last_tool_calls"]  # type: ignore
        assert route_after_reasoning(state) == "critic_node"


class TestRouteAfterCritic:
    def test_pass_goes_to_end(self):
        assert route_after_critic(make_state(critic_status="PASS", iterations=1)) == END

    def test_revise_goes_to_reasoning(self):
        assert route_after_reasoning(make_state(
            last_tool_calls=[{"name": "s", "args": {}, "id": "c1"}],
        )) == "tool_node"

    def test_circuit_breaker_at_max(self):
        assert route_after_critic(make_state(critic_status="REVISE", iterations=_MAX_ITERATIONS)) == END

    def test_circuit_breaker_beyond_max(self):
        assert route_after_critic(make_state(critic_status="REVISE", iterations=5)) == END


class TestExtractUserInput:
    def test_extracts_last_human_message(self):
        from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
        state = make_state(messages=[
            SystemMessage(content="system"),
            HumanMessage(content="first"),
            AIMessage(content="answer"),
            HumanMessage(content="second"),
        ])
        assert _extract_user_input(state) == "second"

    def test_skips_system_and_ai(self):
        from langchain_core.messages import SystemMessage, AIMessage
        state = make_state(messages=[SystemMessage(content="sys"), AIMessage(content="ai")])
        assert _extract_user_input(state) == ""

    def test_returns_empty_for_no_human(self):
        from langchain_core.messages import SystemMessage
        state = make_state(messages=[SystemMessage(content="sys")])
        assert _extract_user_input(state) == ""
