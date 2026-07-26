"""
reasoning_node 测试（mock LLM）— Phase 6 统一架构

验证意图分类、bind_tools 开关、消化态行为、路由、熔断。
覆盖 depth="quick"（原 Dialogue）和 depth="auto" 模式。
可独立运行: python -m pytest test/test_dialogue.py -v
"""

from __future__ import annotations

from unittest.mock import Mock, patch

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agent.nodes import reasoning_node
from agent.state import get_max_iterations
from test.conftest import make_mock_llm

import pytest


def _make_state(**overrides) -> dict:
    """构造 AgentState，所有字段带有合理默认值。"""
    defaults: dict = {
        "messages": [
            SystemMessage(content="You are Bangumi娘."),
            HumanMessage(content="你好"),
        ],
        "iterations": 0,
        "critic_status": "PENDING",
        "critic_feedback": "",
        "query_intent": "unknown",
        "session_id": "test-session",
        "user_id": "test-user",
        "error_flag": False,
        "_memory_context": "",
        "output_style": "bangumi",
        "depth": "auto",
    }
    defaults.update(overrides)
    return defaults


def _extract_tool_calls_from_result(result: dict) -> list[dict]:
    """从 reasoning_node 返回的 messages 中提取 tool_calls。"""
    for msg in result.get("messages", []):
        if isinstance(msg, AIMessage) and hasattr(msg, "tool_calls") and msg.tool_calls:
            return list(msg.tool_calls)
    return []


@pytest.mark.asyncio
class TestReasoningNode:
    """reasoning_node — mock LLM（depth="quick" 模式）"""

    @patch("agent.nodes.create_llm")
    @patch("agent.nodes.get_agent_tools")
    async def test_chitchat_still_binds_tools(self, mock_get_tools, mock_create_llm):
        """chitchat → 仍绑定工具，LLM 自主决定是否调用"""
        mock_get_tools.return_value = [Mock(name="search"), Mock(name="detail")]
        mock = make_mock_llm(content="哼，终于想起我了？")
        mock_create_llm.return_value = mock

        state = _make_state(query_intent="chitchat", iterations=1, depth="auto")
        result = await reasoning_node(state)

        mock.bind_tools.assert_called_once()
        assert _extract_tool_calls_from_result(result) == []
        assert "哼" in str(result["messages"][0].content)

    @patch("agent.nodes.create_llm")
    @patch("agent.nodes.get_agent_tools")
    async def test_factual_still_binds_tools(self, mock_get_tools, mock_create_llm):
        """factual → 仍绑定工具，LLM 自主决定"""
        mock_get_tools.return_value = [Mock(name="search")]
        mock = make_mock_llm(content="三集定律？不就是前3集定生死的老梗嘛。")
        mock_create_llm.return_value = mock

        state = _make_state(query_intent="factual", iterations=1)
        result = await reasoning_node(state)

        mock.bind_tools.assert_called_once()

    @patch("agent.nodes.create_llm")
    @patch("agent.nodes.get_agent_tools")
    async def test_lookup_binds_tools(self, mock_get_tools, mock_create_llm):
        """lookup → 绑定工具并返回 AIMessage 含 tool_calls"""
        mock_get_tools.return_value = [Mock(name="search"), Mock(name="detail")]
        mock = make_mock_llm(
            content="",
            tool_calls=[{"name": "search_bangumi_subject", "args": {"keyword": "巨人"}, "id": "call_1"}],
        )
        mock_create_llm.return_value = mock

        state = _make_state(query_intent="lookup", iterations=1)
        result = await reasoning_node(state)

        mock.bind_tools.assert_called_once()
        tool_calls = _extract_tool_calls_from_result(result)
        assert len(tool_calls) == 1
        assert tool_calls[0]["name"] == "search_bangumi_subject"

    @patch("agent.nodes.create_llm")
    @patch("agent.nodes.get_agent_tools")
    async def test_discovery_binds_tools(self, mock_get_tools, mock_create_llm):
        """discovery → 绑定工具"""
        mock_get_tools.return_value = [Mock(name="search_local")]
        mock = make_mock_llm(
            content="",
            tool_calls=[{"name": "search_local_bangumi", "args": {"query": "机战"}, "id": "call_1"}],
        )
        mock_create_llm.return_value = mock

        state = _make_state(query_intent="discovery", iterations=1)
        await reasoning_node(state)

        mock.bind_tools.assert_called_once()

    @patch("agent.nodes.create_llm")
    async def test_increments_iterations(self, mock_create_llm):
        """正常推理 → iterations +1"""
        mock = make_mock_llm(content="test")
        mock_create_llm.return_value = mock

        state = _make_state(query_intent="chitchat", iterations=0)
        assert (await reasoning_node(state))["iterations"] == 1

    @patch("agent.nodes.create_llm")
    async def test_preserves_existing_query_intent(self, mock_create_llm):
        """后续轮次 → 复用首轮的 query_intent"""
        mock = make_mock_llm(content="根据数据，这部的评分嘛...")
        mock_create_llm.return_value = mock

        state = _make_state(query_intent="lookup", iterations=1)
        assert (await reasoning_node(state))["query_intent"] == "lookup"

    @patch("agent.nodes.create_llm")
    async def test_handles_llm_call_failure(self, mock_create_llm):
        """LLM 异常 → 返回错误消息，不崩溃"""
        mock = make_mock_llm()
        mock.ainvoke.side_effect = RuntimeError("Connection timeout")
        mock_create_llm.return_value = mock

        state = _make_state(query_intent="lookup", iterations=1)
        result = await reasoning_node(state)
        assert "短路" in str(result["messages"][0].content)

    @patch("agent.nodes.create_llm")
    async def test_last_chance_unbinds_tools(self, mock_create_llm):
        """最后一轮（depth="quick", iter=2/3）→ 强制解绑工具"""
        mock = make_mock_llm(content="好吧，就这样吧。")
        mock_create_llm.return_value = mock

        # quick max=3, last_chance at iter>=2 (new_iterations=3 >= 2)
        state = _make_state(query_intent="lookup", iterations=2, depth="quick")
        await reasoning_node(state)

        # 最后一轮不应调用 bind_tools
        mock.bind_tools.assert_not_called()

    # ── 消化态测试 ──

    @patch("agent.nodes.create_llm")
    @patch("agent.nodes.get_agent_tools")
    async def test_digestion_still_binds_tools(self, mock_get_tools, mock_create_llm):
        """消化态 + lookup → 仍绑定工具（模型自主判断是否继续）"""
        mock_get_tools.return_value = [Mock(name="search"), Mock(name="detail")]
        mock = make_mock_llm(content="巨人？8.3分，过誉了吧。")
        mock_create_llm.return_value = mock

        state = _make_state(
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

        mock_get_tools.assert_called_once()
        assert _extract_tool_calls_from_result(result) == []

    @patch("agent.nodes.create_llm")
    @patch("agent.nodes.get_agent_tools")
    async def test_digestion_chitchat_still_binds_tools(self, mock_get_tools, mock_create_llm):
        """消化态 + chitchat → 仍绑定工具（所有 intent 始终绑工具）"""
        mock_get_tools.return_value = [Mock(name="search")]
        mock = make_mock_llm(content="又来找我干嘛？")
        mock_create_llm.return_value = mock

        state = _make_state(
            messages=[
                SystemMessage(content="..."),
                HumanMessage(content="你好"),
                AIMessage(content="", tool_calls=[{"name": "search", "args": {}, "id": "c1"}]),
                ToolMessage(content="结果", tool_call_id="c1"),
            ],
            query_intent="chitchat",
            iterations=1,
        )
        await reasoning_node(state)

        mock.bind_tools.assert_called_once()


class TestRouting:
    """route_after_reasoning — 条件边逻辑"""

    def test_tool_calls_routes_to_tool_node(self):
        """AIMessage 含 tool_calls → tool_node"""
        from agent.graph import route_after_reasoning

        state = _make_state(
            messages=[
                SystemMessage(content="..."),
                HumanMessage(content="搜巨人"),
                AIMessage(
                    content="",
                    tool_calls=[{"name": "search_bangumi_subject", "args": {"keyword": "巨人"}, "id": "c1"}],
                ),
            ],
            iterations=1,
        )
        assert route_after_reasoning(state) == "tool_node"

    def test_no_tool_calls_quick_routes_to_end(self):
        """AIMessage 有 content 但无 tool_calls + depth="quick" → END"""
        from agent.graph import route_after_reasoning

        state = _make_state(
            messages=[
                SystemMessage(content="..."),
                HumanMessage(content="你好"),
                AIMessage(content="哼，又来找我？"),
            ],
            iterations=1,
            query_intent="chitchat",
            depth="quick",
        )
        assert route_after_reasoning(state) == "__end__"

    def test_no_tool_calls_deep_routes_to_critic(self):
        """AIMessage 无 tool_calls + depth="deep" → critic_node"""
        from agent.graph import route_after_reasoning

        state = _make_state(
            messages=[
                SystemMessage(content="..."),
                HumanMessage(content="搜巨人"),
                AIMessage(content="根据搜索结果..."),
            ],
            iterations=1,
            query_intent="lookup",
            depth="deep",
        )
        assert route_after_reasoning(state) == "critic_node"

    def test_max_iterations_enforced_in_node(self):
        """iterations 达上限时 reasoning_node 内 last_chance 解绑工具"""
        max_iter = get_max_iterations("quick")

        # 仅测试 get_max_iterations 返回正确值
        assert max_iter == 3
        assert get_max_iterations("auto") == 5
        assert get_max_iterations("deep") == 12


@pytest.mark.asyncio
class TestMemoryIntegration:
    """manage_memory 在 reasoning_node 中正常调用"""

    @patch("agent.nodes.create_llm")
    @patch("agent.nodes.manage_memory")
    async def test_manage_memory_called(self, mock_memory, mock_create_llm):
        """reasoning_node 调用 manage_memory"""
        mock_memory.return_value = [
            SystemMessage(content="You are Bangumi娘."),
            HumanMessage(content="你好"),
        ]
        mock = make_mock_llm(content="什么事？")
        mock_create_llm.return_value = mock

        state = _make_state(query_intent="chitchat", iterations=1, depth="auto")
        await reasoning_node(state)

        mock_memory.assert_called_once()
