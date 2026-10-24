"""
reasoning_node 测试（mock LLM）

验证意图分类、bind_tools 开关、消化态隔离、critic_feedback 注入、LLM 异常处理。
depth="deep" 模式测试。
可独立运行: python -m pytest test/test_reasoning.py -v
"""

from __future__ import annotations

from unittest.mock import patch

from langchain_core.messages import AIMessage, ToolMessage

from agent.nodes.reasoning import reasoning_node
from test.conftest import make_mock_llm, make_state

import pytest

pytestmark = pytest.mark.asyncio


def _extract_tool_calls_from_result(result: dict) -> list[dict]:
    """从 reasoning_node 返回的 messages 中提取 tool_calls。"""
    for msg in result.get("messages", []):
        if isinstance(msg, AIMessage) and hasattr(msg, "tool_calls") and msg.tool_calls:
            return list(msg.tool_calls)
    return []


class TestReasoningNode:
    """reasoning_node — mock LLM（depth="deep" 模式）"""

    @patch("agent.nodes.reasoning.create_llm")
    @patch("agent.nodes.reasoning.get_agent_tools")
    async def test_chitchat_still_binds_tools(self, mock_get_tools, mock_create_llm):
        mock_get_tools.return_value = []
        mock = make_mock_llm(content="你好！有什么可以帮你的？")
        mock_create_llm.return_value = mock

        from langchain_core.messages import SystemMessage, HumanMessage
        state = make_state(
            messages=[SystemMessage(content="..."), HumanMessage(content="你好")],
            query_intent="chitchat", iterations=1, depth="deep",
        )
        result = await reasoning_node(state)

        mock.bind_tools.assert_called_once()
        assert _extract_tool_calls_from_result(result) == []
        assert result["query_intent"] == "chitchat"

    @patch("agent.nodes.reasoning.create_llm")
    @patch("agent.nodes.reasoning.get_agent_tools")
    async def test_factual_still_binds_tools(self, mock_get_tools, mock_create_llm):
        mock_get_tools.return_value = []
        mock = make_mock_llm(content="三集定律是指...")
        mock_create_llm.return_value = mock

        from langchain_core.messages import SystemMessage, HumanMessage
        state = make_state(
            messages=[SystemMessage(content="..."), HumanMessage(content="什么是三集定律")],
            query_intent="factual", iterations=1, depth="deep",
        )
        result = await reasoning_node(state)

        mock.bind_tools.assert_called_once()
        assert result["query_intent"] == "factual"

    @patch("agent.nodes.reasoning.create_llm")
    @patch("agent.nodes.reasoning.get_agent_tools")
    async def test_lookup_binds_tools(self, mock_get_tools, mock_create_llm):
        mock_get_tools.return_value = []
        mock = make_mock_llm(
            content="",
            tool_calls=[{"name": "search_bangumi_subject", "args": {"keyword": "巨人"}, "id": "call_1"}],
        )
        mock_create_llm.return_value = mock

        state = make_state(query_intent="lookup", iterations=1, depth="deep")
        result = await reasoning_node(state)

        mock.bind_tools.assert_called_once()
        tool_calls = _extract_tool_calls_from_result(result)
        assert len(tool_calls) == 1
        assert tool_calls[0]["name"] == "search_bangumi_subject"

    @patch("agent.nodes.reasoning.create_llm")
    @patch("agent.nodes.reasoning.get_agent_tools")
    async def test_discovery_binds_tools(self, mock_get_tools, mock_create_llm):
        mock_get_tools.return_value = []
        mock = make_mock_llm(
            content="",
            tool_calls=[{"name": "search_local_bangumi", "args": {"query": "机战"}, "id": "call_1"}],
        )
        mock_create_llm.return_value = mock

        state = make_state(query_intent="discovery", iterations=1, depth="deep")
        result = await reasoning_node(state)
        mock.bind_tools.assert_called_once()

    @patch("agent.nodes.reasoning.create_llm")
    async def test_no_tool_calls_when_llm_answers_directly(self, mock_create_llm):
        mock = make_mock_llm(content="顶上战争是...", tool_calls=[])
        mock_create_llm.return_value = mock

        state = make_state(query_intent="factual", iterations=1, depth="deep")
        result = await reasoning_node(state)
        assert _extract_tool_calls_from_result(result) == []

    @patch("agent.nodes.reasoning.create_llm")
    async def test_error_flag_returns_fallback(self, mock_create_llm):
        """节点失败只返回结构化结局，用户话术由唯一出口生成。"""
        state = make_state(error_flag=True, depth="deep")
        result = await reasoning_node(state)
        assert _extract_tool_calls_from_result(result) == []
        assert result["messages"] == []
        assert result["turn_result"]["status"] == "failed"

    @patch("agent.nodes.reasoning.create_llm")
    async def test_increments_iterations(self, mock_create_llm):
        mock = make_mock_llm(content="test")
        mock_create_llm.return_value = mock

        state = make_state(query_intent="chitchat", iterations=0, depth="deep")
        assert (await reasoning_node(state))["iterations"] == 1

    @patch("agent.nodes.reasoning.create_llm")
    @patch("agent.nodes.reasoning.get_agent_tools")
    async def test_critic_feedback_injected_and_cleared(self, mock_get_tools, mock_create_llm):
        """critic_feedback 已废弃——验证 reasoning_node 正常运行。"""
        mock_get_tools.return_value = []
        mock = make_mock_llm(content="已修正的回复")
        mock_create_llm.return_value = mock

        state = make_state(
            query_intent="lookup", iterations=1, depth="deep",
        )
        result = await reasoning_node(state)
        # critic_feedback 不再由 reasoning_node 管理

    @patch("agent.nodes.reasoning.create_llm")
    async def test_preserves_existing_query_intent(self, mock_create_llm):
        mock = make_mock_llm(content="已修正")
        mock_create_llm.return_value = mock

        state = make_state(query_intent="lookup", iterations=1, depth="deep")
        assert (await reasoning_node(state))["query_intent"] == "lookup"

    @patch("agent.nodes.reasoning.create_llm")
    async def test_handles_llm_call_failure(self, mock_create_llm):
        mock = make_mock_llm()
        mock.ainvoke.side_effect = RuntimeError("Connection timeout")
        mock_create_llm.return_value = mock

        state = make_state(query_intent="lookup", iterations=1, depth="deep")
        result = await reasoning_node(state)
        assert result["messages"] == []
        assert result["turn_result"] == {
            "status": "failed",
            "error_type": "RuntimeError",
            "missing": ["reasoning"],
        }

    @patch("agent.nodes.reasoning.recall_memory_step", return_value="")
    @patch("agent.nodes.reasoning.create_llm")
    async def test_llm_failure_is_structured_not_user_visible_text(
        self, mock_create_llm, _mock_recall
    ):
        """供应商异常是本轮失败状态，不是可被 Render 当事实扩写的摘要。"""
        mock = make_mock_llm()
        mock.ainvoke.side_effect = RuntimeError("provider detail must not escape")
        mock_create_llm.return_value = mock

        result = await reasoning_node(
            make_state(query_intent="discuss", iterations=0, depth="fast")
        )

        assert result["turn_result"]["status"] == "failed"
        assert result["turn_result"]["error_type"] == "RuntimeError"
        assert result["messages"] == []
        assert "provider detail" not in str(result)

    # ── 消化态测试 ──

    @patch("agent.nodes.reasoning.create_llm")
    @patch("agent.nodes.reasoning.get_agent_tools")
    async def test_digestion_mode_still_binds_tools(self, mock_get_tools, mock_create_llm):
        from unittest.mock import Mock
        mock_get_tools.return_value = [Mock(name="search"), Mock(name="detail")]
        mock = make_mock_llm(content="根据搜索结果，进击的巨人是...")
        mock_create_llm.return_value = mock

        from langchain_core.messages import SystemMessage, HumanMessage
        state = make_state(
            messages=[
                SystemMessage(content="..."),
                HumanMessage(content="搜巨人"),
                AIMessage(content="", tool_calls=[{"name": "search", "args": {}, "id": "c1"}]),
                ToolMessage(content="找到 5 个结果", tool_call_id="c1"),
            ],
            query_intent="lookup",
            iterations=1, depth="deep",
        )
        result = await reasoning_node(state)

        mock_get_tools.assert_called_once()
        assert result["messages"][0].content == "根据搜索结果，进击的巨人是..."
        assert _extract_tool_calls_from_result(result) == []

    @patch("agent.nodes.reasoning.create_llm")
    @patch("agent.nodes.reasoning.get_agent_tools")
    async def test_digestion_mode_chitchat_still_binds_tools(self, mock_get_tools, mock_create_llm):
        mock_get_tools.return_value = []
        mock = make_mock_llm(content="你好！有什么可以帮你的？")
        mock_create_llm.return_value = mock

        from langchain_core.messages import SystemMessage, HumanMessage
        state = make_state(
            messages=[
                SystemMessage(content="..."),
                HumanMessage(content="你好"),
                AIMessage(content="", tool_calls=[{"name": "search", "args": {}, "id": "c1"}]),
                ToolMessage(content="结果", tool_call_id="c1"),
            ],
            query_intent="chitchat",
            iterations=1, depth="deep",
        )
        result = await reasoning_node(state)
        mock.bind_tools.assert_called_once()
        assert _extract_tool_calls_from_result(result) == []
        assert result["query_intent"] == "chitchat"
