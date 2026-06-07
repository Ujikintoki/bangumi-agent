"""
reasoning_node 测试（mock LLM）

验证意图分类、bind_tools 开关、critic_feedback 注入、LLM 异常处理。
可独立运行: python -m pytest test/test_reasoning.py -v
"""

from __future__ import annotations

from unittest.mock import patch

from agent.research.nodes import reasoning_node
from test.conftest import make_mock_llm, make_state

# ═══════════════════════════════════════════════════════════════════
# 说明：reasoning_node 调用 create_llm() 两次（分类器 + 主 LLM）。
# 为避免分类器 mock 干扰，需要预置 query_intent（跳过分类步骤）
# 的测试将 query_intent + iterations≥1 作为前置条件。
# ═══════════════════════════════════════════════════════════════════


class TestReasoningNode:
    """reasoning_node — mock LLM"""

    @patch("agent.research.nodes.create_llm")
    def test_chitchat_does_not_bind_tools(self, mock_create_llm):
        """chitchat（规则命中） → 不绑定工具"""
        mock = make_mock_llm(content="你好！有什么可以帮你的？")
        mock_create_llm.return_value = mock

        from langchain_core.messages import SystemMessage, HumanMessage
        state = make_state(messages=[SystemMessage(content="..."), HumanMessage(content="你好")])
        result = reasoning_node(state)

        mock.bind_tools.assert_not_called()
        assert result["last_tool_calls"] == []
        assert result["query_intent"] == "chitchat"

    @patch("agent.research.nodes.create_llm")
    def test_factual_does_not_bind_tools(self, mock_create_llm):
        """factual → 不绑定工具"""
        mock = make_mock_llm(content="三集定律是指...")
        mock_create_llm.return_value = mock

        from langchain_core.messages import SystemMessage, HumanMessage
        state = make_state(messages=[SystemMessage(content="..."), HumanMessage(content="什么是三集定律")])
        result = reasoning_node(state)

        mock.bind_tools.assert_not_called()
        assert result["query_intent"] == "factual"

    @patch("agent.research.nodes.create_llm")
    @patch("agent.research.nodes.get_agent_tools")
    def test_lookup_binds_tools(self, mock_get_tools, mock_create_llm):
        """lookup → 绑定工具并返回 tool_calls"""
        mock_get_tools.return_value = []
        mock = make_mock_llm(
            content="",
            tool_calls=[{"name": "search_bangumi_subject", "args": {"keyword": "巨人"}, "id": "call_1"}],
        )
        mock_create_llm.return_value = mock

        state = make_state(query_intent="lookup", iterations=1)
        result = reasoning_node(state)

        mock.bind_tools.assert_called_once()
        assert len(result["last_tool_calls"]) == 1
        assert result["last_tool_calls"][0]["name"] == "search_bangumi_subject"

    @patch("agent.research.nodes.create_llm")
    @patch("agent.research.nodes.get_agent_tools")
    def test_discovery_binds_tools(self, mock_get_tools, mock_create_llm):
        """discovery → 绑定工具"""
        mock_get_tools.return_value = []
        mock = make_mock_llm(
            content="",
            tool_calls=[{"name": "search_local_bangumi", "args": {"query": "机战"}, "id": "call_1"}],
        )
        mock_create_llm.return_value = mock

        state = make_state(query_intent="discovery", iterations=1)
        result = reasoning_node(state)
        mock.bind_tools.assert_called_once()

    @patch("agent.research.nodes.create_llm")
    def test_no_tool_calls_when_llm_answers_directly(self, mock_create_llm):
        mock = make_mock_llm(content="顶上战争是...", tool_calls=[])
        mock_create_llm.return_value = mock

        state = make_state(query_intent="factual", iterations=1)
        result = reasoning_node(state)
        assert result["last_tool_calls"] == []

    @patch("agent.research.nodes.create_llm")
    def test_error_flag_returns_fallback(self, mock_create_llm):
        state = make_state(error_flag=True)
        result = reasoning_node(state)
        assert result["last_tool_calls"] == []
        assert "抱歉" in str(result["messages"][0].content)

    @patch("agent.research.nodes.create_llm")
    def test_increments_iterations(self, mock_create_llm):
        mock = make_mock_llm(content="test")
        mock_create_llm.return_value = mock

        state = make_state(query_intent="chitchat", iterations=0)
        assert reasoning_node(state)["iterations"] == 1

    @patch("agent.research.nodes.create_llm")
    @patch("agent.research.nodes.get_agent_tools")
    def test_critic_feedback_synthesis_mode(self, mock_get_tools, mock_create_llm):
        """REVISE 轮次（有 critic_feedback）→ 工具仍绑定但 prompt 含合成指令"""
        mock_get_tools.return_value = []
        mock = make_mock_llm(content="攻壳机动队 S.A.C. 评分 9.1，排名 #12。")
        mock_create_llm.return_value = mock

        state = make_state(
            query_intent="lookup", iterations=1,
            critic_feedback="请基于工具返回的内容组织回答 | 回复缺失",
        )
        result = reasoning_node(state)
        # 验证：工具仍然被绑定（不强制解绑）
        mock.bind_tools.assert_called_once()
        # 验证：feedback 已消费
        assert result["critic_feedback"] == ""
        # 验证：LLM 合成出回复
        assert result["last_tool_calls"] == []

    @patch("agent.research.nodes.create_llm")
    def test_preserves_existing_query_intent(self, mock_create_llm):
        mock = make_mock_llm(content="已修正")
        mock_create_llm.return_value = mock

        state = make_state(query_intent="lookup", iterations=1)
        assert reasoning_node(state)["query_intent"] == "lookup"

    @patch("agent.research.nodes.create_llm")
    def test_handles_llm_call_failure(self, mock_create_llm):
        mock = make_mock_llm()
        mock.invoke.side_effect = RuntimeError("Connection timeout")
        mock_create_llm.return_value = mock

        state = make_state(query_intent="lookup", iterations=1)
        result = reasoning_node(state)
        assert "暂时不可用" in str(result["messages"][0].content)
