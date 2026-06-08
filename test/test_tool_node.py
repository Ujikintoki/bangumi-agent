"""
ToolNode 测试（mock 工具 + 最小化测试图）

验证 ToolNode 并发执行、ToolMessage 格式、原生消息路由。
可独立运行: python -m pytest test/test_tool_node.py -v
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from agent.research.state import AgentState
from test.conftest import (
    MOCK_TOOLS,
    make_state,
    mock_detail_tool,
    mock_failing_tool,
    mock_search_tool,
)


def _make_tool_graph(tools: list):
    """构建最小化测试图: reasoning_stub → (条件) tool_node → END。

    使用原生消息路由：stub reasoning 返回 AIMessage（含 tool_calls），
    条件边直接读 messages[-1].tool_calls 判定。
    """
    graph = StateGraph(AgentState)

    def _stub_reasoning(state: AgentState) -> dict:
        # 从 state 的 messages 中读取上一轮构造的 tool_calls
        # （由 make_state 的 messages 中最后一个 AIMessage 提供）
        messages = state.get("messages", [])
        last_msg = messages[-1] if messages else None
        tool_calls = []
        if isinstance(last_msg, AIMessage) and hasattr(last_msg, "tool_calls"):
            tool_calls = list(last_msg.tool_calls) if last_msg.tool_calls else []
        return {
            "messages": [AIMessage(content="", tool_calls=tool_calls)],
            "iterations": state.get("iterations", 0) + 1,
        }

    graph.add_node("reasoning_node", _stub_reasoning)
    graph.add_node("tool_node", ToolNode(tools, handle_tool_errors=True))
    graph.add_edge(START, "reasoning_node")
    graph.add_conditional_edges(
        "reasoning_node",
        lambda s: "tool_node" if (
            s.get("messages") and
            isinstance(s["messages"][-1], AIMessage) and
            hasattr(s["messages"][-1], "tool_calls") and
            s["messages"][-1].tool_calls
        ) else END,
        {"tool_node": "tool_node", END: END},
    )
    graph.add_edge("tool_node", END)
    return graph.compile()


def _state_with_tool_calls(tool_calls: list[dict]) -> dict:
    """构造含 tool_calls 的 AIMessage 的 state，用于触发 tool_node 路由。"""
    return make_state(
        messages=[
            AIMessage(content="", tool_calls=tool_calls),
        ],
    )


class TestToolNodeExecution:
    """ToolNode: mock 工具执行"""

    def test_executes_tool_and_returns_tool_message(self):
        graph = _make_tool_graph([mock_search_tool])
        state = _state_with_tool_calls([
            {"name": "mock_search_tool", "args": {"keyword": "巨人"}, "id": "call_1"},
        ])
        result = graph.invoke(state)
        msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        assert len(msgs) == 1
        assert msgs[0].tool_call_id == "call_1"
        assert "巨人" in msgs[0].content

    def test_executes_multiple_tools_in_parallel(self):
        graph = _make_tool_graph([mock_search_tool, mock_detail_tool])
        state = _state_with_tool_calls([
            {"name": "mock_search_tool", "args": {"keyword": "巨人"}, "id": "call_1"},
            {"name": "mock_detail_tool", "args": {"subject_id": 8}, "id": "call_2"},
        ])
        result = graph.invoke(state)
        msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        assert len(msgs) == 2
        assert {m.tool_call_id for m in msgs} == {"call_1", "call_2"}

    def test_handles_tool_execution_failure(self):
        graph = _make_tool_graph([mock_failing_tool])
        state = _state_with_tool_calls([
            {"name": "mock_failing_tool", "args": {"query": "test"}, "id": "call_1"},
        ])
        result = graph.invoke(state)
        msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        assert len(msgs) == 1  # handle_tool_errors=True 捕获异常

    def test_unknown_tool_returns_error(self):
        graph = _make_tool_graph([mock_search_tool])
        state = _state_with_tool_calls([
            {"name": "nonexistent_tool", "args": {}, "id": "call_1"},
        ])
        result = graph.invoke(state)
        msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        assert len(msgs) == 1
        assert "nonexistent_tool" in msgs[0].content

    def test_no_tool_calls_routes_to_end(self):
        """无 tool_calls 时路由到 END，不执行 tool_node"""
        graph = _make_tool_graph([mock_search_tool])
        state = make_state(
            messages=[AIMessage(content="直接回复，不调工具")],
        )
        result = graph.invoke(state)
        # 无 ToolMessage 产生
        msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        assert len(msgs) == 0
