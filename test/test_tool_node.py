"""
ToolNode 测试（mock 工具 + 最小化测试图）

验证 ToolNode 并发执行、ToolMessage 格式、last_tool_calls 生命周期安全。
可独立运行: python -m pytest test/test_tool_node.py -v
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from agent.state import AgentState
from test.conftest import (
    MOCK_TOOLS,
    make_state,
    mock_detail_tool,
    mock_failing_tool,
    mock_search_tool,
)


def _make_tool_graph(tools: list):
    """构建最小化测试图: reasoning_stub → tool_node → END。"""
    graph = StateGraph(AgentState)

    def _stub_reasoning(state: AgentState) -> dict:
        return {
            "messages": [AIMessage(content="", tool_calls=state.get("last_tool_calls", []))],
            "iterations": state.get("iterations", 0) + 1,
        }

    graph.add_node("reasoning_node", _stub_reasoning)
    graph.add_node("tool_node", ToolNode(tools, handle_tool_errors=True))
    graph.add_edge(START, "reasoning_node")
    graph.add_conditional_edges(
        "reasoning_node",
        lambda s: "tool_node" if s.get("last_tool_calls") else END,
        {"tool_node": "tool_node", END: END},
    )
    graph.add_edge("tool_node", END)
    return graph.compile()


class TestToolNodeExecution:
    """ToolNode: mock 工具执行"""

    def test_executes_tool_and_returns_tool_message(self):
        graph = _make_tool_graph([mock_search_tool])
        state = make_state(last_tool_calls=[
            {"name": "mock_search_tool", "args": {"keyword": "巨人"}, "id": "call_1"},
        ])
        result = graph.invoke(state)
        msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        assert len(msgs) == 1
        assert msgs[0].tool_call_id == "call_1"
        assert "巨人" in msgs[0].content

    def test_executes_multiple_tools_in_parallel(self):
        graph = _make_tool_graph([mock_search_tool, mock_detail_tool])
        state = make_state(last_tool_calls=[
            {"name": "mock_search_tool", "args": {"keyword": "巨人"}, "id": "call_1"},
            {"name": "mock_detail_tool", "args": {"subject_id": 8}, "id": "call_2"},
        ])
        result = graph.invoke(state)
        msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        assert len(msgs) == 2
        assert {m.tool_call_id for m in msgs} == {"call_1", "call_2"}

    def test_does_not_touch_last_tool_calls(self):
        graph = _make_tool_graph([mock_search_tool])
        state = make_state(last_tool_calls=[
            {"name": "mock_search_tool", "args": {"keyword": "巨人"}, "id": "call_1"},
        ])
        result = graph.invoke(state)
        # ToolNode 不应清除 last_tool_calls
        assert "last_tool_calls" not in result or len(result.get("last_tool_calls", [])) > 0

    def test_handles_tool_execution_failure(self):
        graph = _make_tool_graph([mock_failing_tool])
        state = make_state(last_tool_calls=[
            {"name": "mock_failing_tool", "args": {"query": "test"}, "id": "call_1"},
        ])
        result = graph.invoke(state)
        msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        assert len(msgs) == 1  # handle_tool_errors=True 捕获异常

    def test_unknown_tool_returns_error(self):
        graph = _make_tool_graph([mock_search_tool])
        state = make_state(last_tool_calls=[
            {"name": "nonexistent_tool", "args": {}, "id": "call_1"},
        ])
        result = graph.invoke(state)
        msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        assert len(msgs) == 1
        assert "nonexistent_tool" in msgs[0].content
