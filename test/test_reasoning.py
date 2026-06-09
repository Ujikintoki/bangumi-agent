"""
reasoning_node 测试（mock LLM）

验证意图分类、bind_tools 开关、消化态隔离、critic_feedback 注入、LLM 异常处理。
可独立运行: python -m pytest test/test_reasoning.py -v
"""

from __future__ import annotations

from unittest.mock import patch

from langchain_core.messages import AIMessage, ToolMessage

from agent.research.nodes import reasoning_node
from test.conftest import make_mock_llm, make_state

import pytest

pytestmark = pytest.mark.asyncio

# ═══════════════════════════════════════════════════════════════════
# 说明：reasoning_node 调用 create_llm() 两次（分类器 + 主 LLM）。
# 为避免分类器 mock 干扰，需要预置 query_intent（跳过分类步骤）
# 的测试将 query_intent + iterations≥1 作为前置条件。
# ═══════════════════════════════════════════════════════════════════


def _extract_tool_calls_from_result(result: dict) -> list[dict]:
    """从 reasoning_node 返回的 messages 中提取 tool_calls。"""
    for msg in result.get("messages", []):
        if isinstance(msg, AIMessage) and hasattr(msg, "tool_calls") and msg.tool_calls:
            return list(msg.tool_calls)
    return []


class TestReasoningNode:
    """reasoning_node — mock LLM"""

    @patch("agent.research.nodes.create_llm")
    async def test_chitchat_does_not_bind_tools(self, mock_create_llm):
        """chitchat（规则命中） → 不绑定工具"""
        mock = make_mock_llm(content="你好！有什么可以帮你的？")
        mock_create_llm.return_value = mock

        from langchain_core.messages import SystemMessage, HumanMessage
        state = make_state(messages=[SystemMessage(content="..."), HumanMessage(content="你好")])
        result = await reasoning_node(state)

        mock.bind_tools.assert_not_called()
        assert _extract_tool_calls_from_result(result) == []
        assert result["query_intent"] == "chitchat"

    @patch("agent.research.nodes.create_llm")
    async def test_factual_does_not_bind_tools(self, mock_create_llm):
        """factual → 不绑定工具"""
        mock = make_mock_llm(content="三集定律是指...")
        mock_create_llm.return_value = mock

        from langchain_core.messages import SystemMessage, HumanMessage
        state = make_state(messages=[SystemMessage(content="..."), HumanMessage(content="什么是三集定律")])
        result = await reasoning_node(state)

        mock.bind_tools.assert_not_called()
        assert result["query_intent"] == "factual"

    @patch("agent.research.nodes.create_llm")
    @patch("agent.research.nodes.get_agent_tools")
    async def test_lookup_binds_tools(self, mock_get_tools, mock_create_llm):
        """lookup → 绑定工具并返回 AIMessage 含 tool_calls"""
        mock_get_tools.return_value = []
        mock = make_mock_llm(
            content="",
            tool_calls=[{"name": "search_bangumi_subject", "args": {"keyword": "巨人"}, "id": "call_1"}],
        )
        mock_create_llm.return_value = mock

        state = make_state(query_intent="lookup", iterations=1)
        result = await reasoning_node(state)

        mock.bind_tools.assert_called_once()
        tool_calls = _extract_tool_calls_from_result(result)
        assert len(tool_calls) == 1
        assert tool_calls[0]["name"] == "search_bangumi_subject"

    @patch("agent.research.nodes.create_llm")
    @patch("agent.research.nodes.get_agent_tools")
    async def test_discovery_binds_tools(self, mock_get_tools, mock_create_llm):
        """discovery → 绑定工具"""
        mock_get_tools.return_value = []
        mock = make_mock_llm(
            content="",
            tool_calls=[{"name": "search_local_bangumi", "args": {"query": "机战"}, "id": "call_1"}],
        )
        mock_create_llm.return_value = mock

        state = make_state(query_intent="discovery", iterations=1)
        result = await reasoning_node(state)
        mock.bind_tools.assert_called_once()

    @patch("agent.research.nodes.create_llm")
    async def test_no_tool_calls_when_llm_answers_directly(self, mock_create_llm):
        mock = make_mock_llm(content="顶上战争是...", tool_calls=[])
        mock_create_llm.return_value = mock

        state = make_state(query_intent="factual", iterations=1)
        result = await reasoning_node(state)
        assert _extract_tool_calls_from_result(result) == []

    @patch("agent.research.nodes.create_llm")
    async def test_error_flag_returns_fallback(self, mock_create_llm):
        state = make_state(error_flag=True)
        result = await reasoning_node(state)
        assert _extract_tool_calls_from_result(result) == []
        assert "抱歉" in str(result["messages"][0].content)

    @patch("agent.research.nodes.create_llm")
    async def test_increments_iterations(self, mock_create_llm):
        mock = make_mock_llm(content="test")
        mock_create_llm.return_value = mock

        state = make_state(query_intent="chitchat", iterations=0)
        assert (await reasoning_node(state))["iterations"] == 1

    @patch("agent.research.nodes.create_llm")
    @patch("agent.research.nodes.get_agent_tools")
    async def test_critic_feedback_injected_and_cleared(self, mock_get_tools, mock_create_llm):
        """critic_feedback 注入到 system prompt 并在消费后清空"""
        mock_get_tools.return_value = []
        mock = make_mock_llm(content="已修正的回复")
        mock_create_llm.return_value = mock

        state = make_state(
            query_intent="lookup", iterations=1,
            critic_feedback="缺少评分 | 调用 get_detail | 缺失评分",
        )
        result = await reasoning_node(state)
        assert result["critic_feedback"] == ""

    @patch("agent.research.nodes.create_llm")
    async def test_preserves_existing_query_intent(self, mock_create_llm):
        mock = make_mock_llm(content="已修正")
        mock_create_llm.return_value = mock

        state = make_state(query_intent="lookup", iterations=1)
        assert (await reasoning_node(state))["query_intent"] == "lookup"

    @patch("agent.research.nodes.create_llm")
    async def test_handles_llm_call_failure(self, mock_create_llm):
        mock = make_mock_llm()
        mock.ainvoke.side_effect = RuntimeError("Connection timeout")
        mock_create_llm.return_value = mock

        state = make_state(query_intent="lookup", iterations=1)
        result = await reasoning_node(state)
        assert "暂时不可用" in str(result["messages"][0].content)

    # ── 消化态测试：验证消化态解绑工具 ──────────────────────

    @patch("agent.research.nodes.create_llm")
    @patch("agent.research.nodes.get_agent_tools")
    async def test_digestion_mode_skips_tools(self, mock_get_tools, mock_create_llm):
        """消化态（最后一条消息为 ToolMessage）+ lookup → 不绑定工具

        消化态从物理层面强制 LLM 输出文本回复，杜绝"工具诱惑陷阱"——
        LLM 在消化海量工具结果时被工具列表挟持，放弃生成总结、继续盲目调工具。
        """
        mock_get_tools.return_value = []
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
            iterations=1,
        )
        result = await reasoning_node(state)

        # 消化态下不绑定工具（get_agent_tools 不应被调用）
        mock_get_tools.assert_not_called()
        # LLM 直接生成文本回复，不发起新的工具调用
        assert result["messages"][0].content == "根据搜索结果，进击的巨人是..."
        assert _extract_tool_calls_from_result(result) == []

    @patch("agent.research.nodes.create_llm")
    async def test_digestion_mode_chitchat_still_skips_tools(self, mock_create_llm):
        """消化态 + chitchat → 仍不绑定工具（chitchat 任何情况下都不绑）"""
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
            iterations=1,
        )
        result = await reasoning_node(state)
        mock.bind_tools.assert_not_called()
        assert _extract_tool_calls_from_result(result) == []
        assert result["query_intent"] == "chitchat"
