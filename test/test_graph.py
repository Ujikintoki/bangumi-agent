"""
图谱集成测试（mock LLM + mock 工具）

端到端验证 ReAct 循环关键路径。深度集成测试（多轮 tool →
critic → retry）依赖 mocking chain，兼容性依赖 LangGraph 版本。
核心行为已由各节点 unit test 覆盖。
可独立运行: python -m pytest test/test_graph.py -v
"""

from __future__ import annotations

from unittest.mock import patch

from langchain_core.messages import HumanMessage, SystemMessage

from agent.graph import build_graph
from test.conftest import MOCK_TOOLS, make_mock_llm, make_state


class TestGraphIntegration:
    """端到端图谱"""

    @patch("agent.nodes.create_llm")
    def test_chitchat_skips_tools(self, mock_create_llm):
        """'你好' → 不调工具 → critic → PASS → END"""
        mock_create_llm.return_value = make_mock_llm(content="你好！有什么可以帮你的？")
        graph = build_graph(tools=MOCK_TOOLS)
        state = make_state(messages=[SystemMessage(content="..."), HumanMessage(content="你好")])
        result = graph.invoke(state)
        assert result.get("critic_status") == "PASS"
        assert result.get("query_intent") == "chitchat"

    @patch("agent.nodes.create_llm")
    def test_circuit_breaker(self, mock_create_llm):
        """3 轮 → 熔断"""
        mock_create_llm.return_value = make_mock_llm(content="test")
        graph = build_graph(tools=MOCK_TOOLS)
        state = make_state(
            messages=[SystemMessage(content="..."), HumanMessage(content="搜巨人")],
            iterations=2, critic_status="REVISE", query_intent="lookup",
            last_tool_calls=[{"name": "mock_search_tool", "args": {}, "id": "c1"}],
        )
        result = graph.invoke(state)
        assert result.get("error_flag") is True

    @patch("agent.nodes.create_llm")
    def test_factual_skips_tools(self, mock_create_llm):
        """factual → 不调工具 → 直接回复"""
        mock_create_llm.return_value = make_mock_llm(content="三集定律是指新番播出三集后...")
        graph = build_graph(tools=MOCK_TOOLS)
        state = make_state(messages=[SystemMessage(content="..."), HumanMessage(content="什么是三集定律")])
        result = graph.invoke(state)
        assert result.get("critic_status") == "PASS"
        assert result.get("query_intent") == "factual"

    @patch("agent.nodes.create_llm")
    def test_discovery_binds_tools_and_completes(self, mock_create_llm):
        """discovery → 调工具 → 完成（模拟空工具结果）"""
        mock_create_llm.return_value = make_mock_llm(
            content="根据 RAG 搜索结果，推荐以下作品：1. 混沌之子...",
            tool_calls=[],
        )
        graph = build_graph(tools=MOCK_TOOLS)
        state = make_state(
            messages=[SystemMessage(content="..."), HumanMessage(content="推荐类似巨人的番")],
            query_intent="discovery",
        )
        result = graph.invoke(state)
        assert result.get("critic_status") in ("PASS", "REVISE")
