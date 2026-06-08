"""
AgentState 结构 & 路由测试

验证 State 字段完整性、原生消息路由逻辑、辅助函数。
可独立运行: python -m pytest test/test_state.py -v
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END

from agent.research.graph import route_after_critic, route_after_reasoning
from agent.research.nodes import _extract_user_input
from agent.research.state import _MAX_ITERATIONS
from test.conftest import make_state


class TestAgentStateStructure:
    """AgentState 字段完整性（8 字段，last_tool_calls 已删除）"""

    def test_all_required_keys_present(self):
        state = make_state()
        for key in ("messages", "iterations", "critic_status", "critic_feedback",
                     "query_intent", "session_id", "user_id", "error_flag"):
            assert key in state, f"缺少必需字段: {key}"

    def test_last_tool_calls_removed(self):
        """last_tool_calls 已从 AgentState 中删除"""
        assert "last_tool_calls" not in make_state()
        assert "needs_tool" not in make_state()

    def test_defaults(self):
        state = make_state()
        assert state["query_intent"] == "unknown"
        assert state["critic_feedback"] == ""
        assert state["session_id"] == "test-session"
        assert state["user_id"] == "test-user"


class TestRouteAfterReasoning:
    """原生消息路由：读取 messages[-1] 的 tool_calls 判定路由。"""

    def test_routes_to_tool_when_ai_has_tool_calls(self):
        """AIMessage 含 tool_calls → tool_node"""
        state = make_state(
            messages=[
                SystemMessage(content="..."),
                HumanMessage(content="搜巨人"),
                AIMessage(content="", tool_calls=[{"name": "search", "args": {}, "id": "c1"}]),
            ],
        )
        assert route_after_reasoning(state) == "tool_node"

    def test_routes_to_critic_when_no_tool_calls(self):
        """AIMessage 无 tool_calls + lookup intent → critic_node"""
        state = make_state(
            messages=[
                SystemMessage(content="..."),
                HumanMessage(content="搜巨人"),
                AIMessage(content="根据搜索结果..."),
            ],
            query_intent="lookup",
        )
        assert route_after_reasoning(state) == "critic_node"

    def test_routes_to_critic_when_empty_messages(self):
        """空消息列表 → critic_node（防御性处理）"""
        state = make_state(messages=[])
        assert route_after_reasoning(state) == "critic_node"

    def test_chitchat_fast_path_to_end(self):
        """chitchat 无工具调用 → 快速通道直达 END"""
        state = make_state(
            messages=[
                SystemMessage(content="..."),
                HumanMessage(content="你好"),
                AIMessage(content="你好！有什么可以帮你的？"),
            ],
            query_intent="chitchat",
        )
        assert route_after_reasoning(state) == END

    def test_factual_still_goes_to_critic(self):
        """factual 不走快速通道 → critic_node"""
        state = make_state(
            messages=[
                SystemMessage(content="..."),
                HumanMessage(content="什么是三集定律"),
                AIMessage(content="三集定律是指..."),
            ],
            query_intent="factual",
        )
        assert route_after_reasoning(state) == "critic_node"

    def test_tool_calls_override_fast_path(self):
        """即使 chitchat 意图，有 tool_calls 时仍然走 tool_node"""
        state = make_state(
            messages=[
                SystemMessage(content="..."),
                HumanMessage(content="你好"),
                AIMessage(content="", tool_calls=[{"name": "search", "args": {}, "id": "c1"}]),
            ],
            query_intent="chitchat",
        )
        assert route_after_reasoning(state) == "tool_node"

    def test_tool_calls_routes_to_tool_for_lookup(self):
        """lookup intent + AIMessage 含 tool_calls → tool_node"""
        state = make_state(
            messages=[
                SystemMessage(content="..."),
                HumanMessage(content="搜巨人"),
                AIMessage(content="", tool_calls=[{"name": "search", "args": {}, "id": "c1"}]),
            ],
            query_intent="lookup",
        )
        assert route_after_reasoning(state) == "tool_node"


class TestRouteAfterCritic:
    def test_pass_goes_to_end(self):
        assert route_after_critic(make_state(critic_status="PASS", iterations=1)) == END

    def test_revise_goes_to_reasoning(self):
        assert route_after_critic(make_state(critic_status="REVISE", iterations=1)) == "reasoning_node"

    def test_circuit_breaker_at_max(self):
        assert route_after_critic(make_state(critic_status="REVISE", iterations=_MAX_ITERATIONS)) == END

    def test_circuit_breaker_beyond_max(self):
        assert route_after_critic(make_state(critic_status="REVISE", iterations=10)) == END


class TestExtractUserInput:
    def test_extracts_last_human_message(self):
        state = make_state(messages=[
            SystemMessage(content="system"),
            HumanMessage(content="first"),
            AIMessage(content="answer"),
            HumanMessage(content="second"),
        ])
        assert _extract_user_input(state) == "second"

    def test_skips_system_and_ai(self):
        state = make_state(messages=[SystemMessage(content="sys"), AIMessage(content="ai")])
        assert _extract_user_input(state) == ""

    def test_returns_empty_for_no_human(self):
        state = make_state(messages=[SystemMessage(content="sys")])
        assert _extract_user_input(state) == ""
