"""
AgentState 结构 & 路由测试

验证 State 字段完整性、原生消息路由逻辑、辅助函数。
可独立运行: python -m pytest test/test_state.py -v
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END

from agent.graph import route_after_reasoning
from agent.orchestrate.nodes import _extract_user_input
from agent.state import _MAX_ITERATIONS_DEEP as _MAX_ITERATIONS
from test.conftest import make_state


class TestAgentStateStructure:
    """AgentState 字段完整性（Phase 6 新增 depth 和 _memory_context）"""

    def test_all_required_keys_present(self):
        state = make_state()
        for key in ("messages", "iterations",
                     "query_intent", "session_id", "user_id", "error_flag",
                     "_memory_context", "output_style", "depth"):
            assert key in state, f"缺少必需字段: {key}"

    def test_last_tool_calls_removed(self):
        """last_tool_calls 已从 AgentState 中删除"""
        assert "last_tool_calls" not in make_state()
        assert "needs_tool" not in make_state()

    def test_defaults(self):
        state = make_state()
        assert state["query_intent"] == "unknown"
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

    def test_deep_routes_to_end_when_no_tool_calls(self):
        """AIMessage 无 tool_calls + depth=deep → END"""
        state = make_state(
            messages=[
                SystemMessage(content="..."),
                HumanMessage(content="搜巨人"),
                AIMessage(content="根据搜索结果..."),
            ],
            query_intent="lookup",
            depth="deep",
        )
        assert route_after_reasoning(state) == END

    def test_routes_to_end_when_shallow_no_tool_calls(self):
        """AIMessage 无 tool_calls + depth=auto → END"""
        state = make_state(
            messages=[
                SystemMessage(content="..."),
                HumanMessage(content="搜巨人"),
                AIMessage(content="根据搜索结果..."),
            ],
            query_intent="lookup",
            depth="fast",
        )
        assert route_after_reasoning(state) == END

    def test_routes_to_end_when_empty_messages(self):
        """空消息列表 + depth=auto → END"""
        state = make_state(messages=[], depth="fast")
        assert route_after_reasoning(state) == END

    def test_chitchat_routes_to_end(self):
        """chitchat 无工具调用 → END"""
        state = make_state(
            messages=[
                SystemMessage(content="..."),
                HumanMessage(content="你好"),
                AIMessage(content="你好！有什么可以帮你的？"),
            ],
            query_intent="chitchat",
        )
        assert route_after_reasoning(state) == END

    def test_factual_deep_routes_to_end(self):
        """factual + depth=deep → END"""
        state = make_state(
            messages=[
                SystemMessage(content="..."),
                HumanMessage(content="什么是三集定律"),
                AIMessage(content="三集定律是指..."),
            ],
            query_intent="factual",
            depth="deep",
        )
        assert route_after_reasoning(state) == END

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
