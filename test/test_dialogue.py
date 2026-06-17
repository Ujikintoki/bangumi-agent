"""
dialogue_reasoning_node 测试（mock LLM）

验证意图分类、bind_tools 开关、消化态行为、路由、熔断。
可独立运行: python -m pytest test/test_dialogue.py -v
"""

from __future__ import annotations

from unittest.mock import Mock, patch

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agent.dialogue.nodes import dialogue_reasoning_node
from test.conftest import make_mock_llm

import pytest

def _make_dialogue_state(**overrides) -> dict:
    """构造 DialogueState，所有字段带有合理默认值。"""
    defaults: dict = {
        "messages": [
            SystemMessage(content="You are Bangumi娘."),
            HumanMessage(content="你好"),
        ],
        "iterations": 0,
        "query_intent": "unknown",
        "session_id": "test-session",
        "user_id": "test-user",
        "output_style": "bangumi",
    }
    defaults.update(overrides)
    return defaults


def _extract_tool_calls_from_result(result: dict) -> list[dict]:
    """从 dialogue_reasoning_node 返回的 messages 中提取 tool_calls。"""
    for msg in result.get("messages", []):
        if isinstance(msg, AIMessage) and hasattr(msg, "tool_calls") and msg.tool_calls:
            return list(msg.tool_calls)
    return []


@pytest.mark.asyncio
class TestDialogueReasoningNode:
    """dialogue_reasoning_node — mock LLM"""

    @patch("agent.dialogue.nodes.create_llm")
    async def test_chitchat_does_not_bind_tools(self, mock_create_llm):
        """chitchat → 不绑定工具，直接文本回复"""
        mock = make_mock_llm(content="哼，终于想起我了？")
        mock_create_llm.return_value = mock

        state = _make_dialogue_state(query_intent="chitchat", iterations=1)
        result = await dialogue_reasoning_node(state)

        mock.bind_tools.assert_not_called()
        assert _extract_tool_calls_from_result(result) == []
        assert "哼" in str(result["messages"][0].content)

    @patch("agent.dialogue.nodes.create_llm")
    async def test_factual_does_not_bind_tools(self, mock_create_llm):
        """factual → 不绑定工具"""
        mock = make_mock_llm(content="三集定律？不就是前3集定生死的老梗嘛。")
        mock_create_llm.return_value = mock

        state = _make_dialogue_state(query_intent="factual", iterations=1)
        result = await dialogue_reasoning_node(state)

        mock.bind_tools.assert_not_called()

    @patch("agent.dialogue.nodes.create_llm")
    @patch("agent.dialogue.nodes.get_agent_tools")
    async def test_lookup_binds_tools(self, mock_get_tools, mock_create_llm):
        """lookup → 绑定工具并返回 AIMessage 含 tool_calls"""
        mock_get_tools.return_value = [Mock(name="search"), Mock(name="detail")]
        mock = make_mock_llm(
            content="",
            tool_calls=[{"name": "search_bangumi_subject", "args": {"keyword": "巨人"}, "id": "call_1"}],
        )
        mock_create_llm.return_value = mock

        state = _make_dialogue_state(query_intent="lookup", iterations=1)
        result = await dialogue_reasoning_node(state)

        mock.bind_tools.assert_called_once()
        tool_calls = _extract_tool_calls_from_result(result)
        assert len(tool_calls) == 1
        assert tool_calls[0]["name"] == "search_bangumi_subject"

    @patch("agent.dialogue.nodes.create_llm")
    @patch("agent.dialogue.nodes.get_agent_tools")
    async def test_discovery_binds_tools(self, mock_get_tools, mock_create_llm):
        """discovery → 绑定工具"""
        mock_get_tools.return_value = [Mock(name="search_local")]
        mock = make_mock_llm(
            content="",
            tool_calls=[{"name": "search_local_bangumi", "args": {"query": "机战"}, "id": "call_1"}],
        )
        mock_create_llm.return_value = mock

        state = _make_dialogue_state(query_intent="discovery", iterations=1)
        await dialogue_reasoning_node(state)

        mock.bind_tools.assert_called_once()

    @patch("agent.dialogue.nodes.create_llm")
    async def test_increments_iterations(self, mock_create_llm):
        """正常推理 → iterations +1"""
        mock = make_mock_llm(content="test")
        mock_create_llm.return_value = mock

        state = _make_dialogue_state(query_intent="chitchat", iterations=0)
        assert (await dialogue_reasoning_node(state))["iterations"] == 1

    @patch("agent.dialogue.nodes.create_llm")
    async def test_preserves_existing_query_intent(self, mock_create_llm):
        """后续轮次 → 复用首轮的 query_intent"""
        mock = make_mock_llm(content="根据数据，这部的评分嘛...")
        mock_create_llm.return_value = mock

        state = _make_dialogue_state(query_intent="lookup", iterations=1)
        assert (await dialogue_reasoning_node(state))["query_intent"] == "lookup"

    @patch("agent.dialogue.nodes.create_llm")
    async def test_handles_llm_call_failure(self, mock_create_llm):
        """LLM 异常 → 返回错误消息，不崩溃"""
        mock = make_mock_llm()
        mock.ainvoke.side_effect = RuntimeError("Connection timeout")
        mock_create_llm.return_value = mock

        state = _make_dialogue_state(query_intent="lookup", iterations=1)
        result = await dialogue_reasoning_node(state)
        assert "短路" in str(result["messages"][0].content)

    # ── 消化态测试 ──

    @patch("agent.dialogue.nodes.create_llm")
    @patch("agent.dialogue.nodes.get_agent_tools")
    async def test_digestion_still_binds_tools(self, mock_get_tools, mock_create_llm):
        """消化态 + lookup → 仍绑定工具（模型自主判断是否继续）"""
        mock_get_tools.return_value = [Mock(name="search"), Mock(name="detail")]
        mock = make_mock_llm(content="巨人？8.3分，过誉了吧。")
        mock_create_llm.return_value = mock

        state = _make_dialogue_state(
            messages=[
                SystemMessage(content="..."),
                HumanMessage(content="搜巨人"),
                AIMessage(content="", tool_calls=[{"name": "search", "args": {}, "id": "c1"}]),
                ToolMessage(content="找到 5 个结果", tool_call_id="c1"),
            ],
            query_intent="lookup",
            iterations=1,
        )
        result = await dialogue_reasoning_node(state)

        mock_get_tools.assert_called_once()
        assert _extract_tool_calls_from_result(result) == []

    @patch("agent.dialogue.nodes.create_llm")
    async def test_digestion_chitchat_skips_tools(self, mock_create_llm):
        """消化态 + chitchat → 仍不绑工具"""
        mock = make_mock_llm(content="又来找我干嘛？")
        mock_create_llm.return_value = mock

        state = _make_dialogue_state(
            messages=[
                SystemMessage(content="..."),
                HumanMessage(content="你好"),
                AIMessage(content="", tool_calls=[{"name": "search", "args": {}, "id": "c1"}]),
                ToolMessage(content="结果", tool_call_id="c1"),
            ],
            query_intent="chitchat",
            iterations=1,
        )
        await dialogue_reasoning_node(state)

        mock.bind_tools.assert_not_called()


class TestDialogueRouting:
    """route_after_dialogue_reasoning — 条件边逻辑"""

    def test_tool_calls_routes_to_tool_node(self):
        """AIMessage 含 tool_calls → tool_node"""
        from agent.dialogue.graph import route_after_dialogue_reasoning

        state = _make_dialogue_state(
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
        assert route_after_dialogue_reasoning(state) == "tool_node"

    def test_no_tool_calls_routes_to_end(self):
        """AIMessage 有 content 但无 tool_calls → END"""
        from agent.dialogue.graph import route_after_dialogue_reasoning

        state = _make_dialogue_state(
            messages=[
                SystemMessage(content="..."),
                HumanMessage(content="你好"),
                AIMessage(content="哼，又来找我？"),
            ],
            iterations=1,
            query_intent="chitchat",
        )
        assert route_after_dialogue_reasoning(state) == "__end__"

    def test_max_iterations_enforced(self):
        """iterations >= 3 → 熔断 END，即使有 tool_calls"""
        from agent.dialogue.graph import route_after_dialogue_reasoning
        from agent.dialogue.state import _MAX_ITERATIONS

        state = _make_dialogue_state(
            messages=[
                SystemMessage(content="..."),
                HumanMessage(content="搜巨人"),
                AIMessage(
                    content="",
                    tool_calls=[{"name": "search_bangumi_subject", "args": {"keyword": "巨人"}, "id": "c1"}],
                ),
            ],
            iterations=_MAX_ITERATIONS,
        )
        assert route_after_dialogue_reasoning(state) == "__end__"


@pytest.mark.asyncio
class TestDialogueMemoryIntegration:
    """manage_memory 在 dialogue_reasoning_node 中正常调用"""

    @patch("agent.dialogue.nodes.create_llm")
    @patch("agent.dialogue.nodes.manage_memory")
    async def test_manage_memory_called(self, mock_memory, mock_create_llm):
        """dialogue_reasoning_node 调用 manage_memory"""
        mock_memory.return_value = [
            SystemMessage(content="You are Bangumi娘."),
            HumanMessage(content="你好"),
        ]
        mock = make_mock_llm(content="什么事？")
        mock_create_llm.return_value = mock

        state = _make_dialogue_state(query_intent="chitchat", iterations=1)
        await dialogue_reasoning_node(state)

        mock_memory.assert_called_once()
