"""
图谱集成测试（mock LLM + mock 工具）— Phase 6 统一架构

验证跨模块耦合：critic_feedback 传播、memory 截断不破坏 graph、
消化态隔离、多轮状态一致性。
可独立运行: python -m pytest test/test_graph.py -v
"""

from __future__ import annotations

from unittest.mock import call, patch

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agent.graph import build_graph
from agent.state import get_max_iterations
from agent.memory.short_term import estimate_tokens
from agent.orchestrate.nodes import _get_last_ai_response, reasoning_node
from test.conftest import MOCK_TOOLS, make_mock_llm, make_state

import pytest

pytestmark = pytest.mark.asyncio

_DEEP_MAX = get_max_iterations("deep")


# ═══════════════════════════════════════════════════════════════════
# 1. 图谱端到端（mock LLM）
# ═══════════════════════════════════════════════════════════════════


class TestGraphIntegration:
    """端到端图谱：基本路径 + 熔断"""

    @patch("agent.orchestrate.nodes.create_llm")
    async def test_chitchat_deep_routes_to_end(self, mock_create_llm):
        """chitchat + depth=deep → END"""
        mock_create_llm.return_value = make_mock_llm(content="你好！")
        graph = build_graph(tools=MOCK_TOOLS)
        state = make_state(
            messages=[SystemMessage(content="..."), HumanMessage(content="你好")],
            query_intent="chitchat", iterations=1, depth="deep",
        )
        result = await graph.ainvoke(state)
        assert "reply" in result or "messages" in result

    @patch("agent.orchestrate.nodes.create_llm")
    async def test_deep_completes_to_end(self, mock_create_llm):
        """deep 模式——reasoning → END。"""
        mock_create_llm.return_value = make_mock_llm(content="三集定律是指...")
        graph = build_graph(tools=MOCK_TOOLS)
        state = make_state(
            messages=[SystemMessage(content="..."), HumanMessage(content="什么是三集定律")],
            depth="deep",
        )
        result = await graph.ainvoke(state)
        messages = result.get("messages", [])
        assert len(messages) > 0

    @patch("agent.orchestrate.nodes.create_llm")
    async def test_query_intent_persists_across_rounds(self, mock_create_llm):
        mock_create_llm.return_value = make_mock_llm(content="done")
        graph = build_graph(tools=MOCK_TOOLS)
        state = make_state(
            messages=[
                SystemMessage(content="..."),
                HumanMessage(content="搜巨人"),
                AIMessage(content="", tool_calls=[{"name": "mock_search_tool", "args": {}, "id": "c1"}]),
            ],
            query_intent="lookup", iterations=1, critic_status="REVISE",
            depth="deep",
        )
        result = await graph.ainvoke(state)
        # v4: classify_node 总是先运行，会对"搜巨人"重新分类
        # lookup → fetch（通过 _INTENT_ALIASES），最终状态为 fetch
        assert result.get("query_intent") == "fetch"


# ═══════════════════════════════════════════════════════════════════
# 2. 跨模块耦合：critic_feedback → reasoning（mock LLM）
# ═══════════════════════════════════════════════════════════════════


class TestCriticFeedbackPropagation:
    """Phase 9: Critic 屏蔽后，critic_feedback 不再影响 prompt。保留测试验证该隔离。"""

    @patch("agent.orchestrate.nodes.create_llm")
    @patch("agent.orchestrate.nodes.get_agent_tools")
    async def test_critic_feedback_not_injected(self, mock_get_tools, mock_create_llm):
        """critic_feedback 不应出现在 system prompt 中（Critic 已屏蔽）。"""
        mock_get_tools.return_value = []
        mock_llm = make_mock_llm(content="正常回复")
        mock_create_llm.return_value = mock_llm

        state = make_state(
            messages=[
                SystemMessage(content="old system prompt"),
                HumanMessage(content="进击的巨人评分"),
                AIMessage(content="", tool_calls=[{"name": "search", "args": {}, "id": "c1"}]),
                ToolMessage(content="结果", tool_call_id="c1"),
            ],
            query_intent="lookup", iterations=1,
            critic_feedback="缺少评分 | 调用 get_detail | 缺失评分",
            depth="deep",
        )
        await reasoning_node(state)

        invoke_call = mock_llm.ainvoke.call_args
        assert invoke_call is not None, "LLM.invoke 未被调用"
        messages_to_llm = invoke_call[0][0]
        system_msgs = [m for m in messages_to_llm if isinstance(m, SystemMessage)]
        combined_system = " ".join(m.content for m in system_msgs)
        # Phase 9: critic_feedback 不再注入
        assert "上一轮回复需要改进" not in combined_system


class TestMemoryGraphIntegration:
    """验证 memory 截断与 graph 协同"""

    async def test_memory_truncation_before_llm_call(self):
        long_content = "长文本" * 2000
        messages = [
            SystemMessage(content="You are Bangumi assistant."),
            HumanMessage(content=long_content),
        ]
        state = make_state(messages=messages, query_intent="chitchat", depth="deep")

        with patch("agent.orchestrate.nodes.create_llm") as mock_create_llm:
            mock_llm = make_mock_llm(content="你好！")
            mock_create_llm.return_value = mock_llm
            result = await reasoning_node(state)

        assert result["iterations"] == 1

    async def test_trimmed_messages_still_contain_system(self):
        messages = [
            SystemMessage(content="You are Bangumi assistant."),
        ]
        for i in range(100):
            messages.append(HumanMessage(content=f"Q{i}: " + "数据" * 50))
            messages.append(AIMessage(content=f"A{i}: " + "回复" * 50))

        from agent.memory.short_term import manage_memory
        trimmed = manage_memory(messages, max_tokens=1000)
        assert any(isinstance(m, SystemMessage) for m in trimmed)
        assert len(trimmed) < len(messages)


# ═══════════════════════════════════════════════════════════════════
# 3. State 生命周期完整性
# ═══════════════════════════════════════════════════════════════════


class TestStateLifecycle:
    """验证跨轮次 state 字段的完整性"""

    async def test_tool_to_reasoning_to_end_pipeline(self):
        """tool → reasoning → END 完整链路"""
        from agent.graph import build_graph

        @patch("agent.orchestrate.nodes.create_llm")
        async def _test(mock_llm):
            mock_llm.return_value = make_mock_llm(
                content="根据搜索结果，巨人评分 8.5 分。",
                tool_calls=[],
            )
            graph = build_graph(tools=MOCK_TOOLS)
            state = make_state(
                messages=[
                    SystemMessage(content="..."),
                    HumanMessage(content="搜巨人"),
                    AIMessage(content="", tool_calls=[{"name": "mock_search_tool", "args": {"keyword": "巨人"}, "id": "call_x"}]),
                ],
                query_intent="lookup",
                depth="deep",
            )
            result = await graph.ainvoke(state)
            messages = result.get("messages", [])
            assert len(messages) > 0

        await _test()

    @patch("agent.orchestrate.nodes.get_settings")
    async def test_critic_status_transitions(self, mock_get_settings):
        from agent.orchestrate.nodes import critic_node
        from unittest.mock import MagicMock

        s = MagicMock()
        s.CRITIC_MODE = "rule"
        s.LLM_CRITIC_MODEL = ""
        s.LLM_MODEL = "test"
        mock_get_settings.return_value = s

        # REVISE: 工具返回但无有效回复
        state1 = make_state(iterations=1, depth="deep", messages=[
            SystemMessage(content="..."), HumanMessage(content="搜"),
            AIMessage(content="", tool_calls=[{"name": "s", "args": {}, "id": "c1"}]),
            ToolMessage(content="结果", tool_call_id="c1"),
        ])
        assert (await critic_node(state1))["critic_status"] == "REVISE"

        # PASS: 有效回复（长度 ≥ 20 字）
        state2 = make_state(iterations=2, depth="deep", messages=[
            SystemMessage(content="..."), HumanMessage(content="搜"),
            AIMessage(content="", tool_calls=[{"name": "s", "args": {}, "id": "c1"}]),
            ToolMessage(content="结果", tool_call_id="c1"),
            AIMessage(content="进击的巨人最终季评分 8.7 分，排名全站前二十，非常推荐观看。"),
        ])
        assert (await critic_node(state2))["critic_status"] == "PASS"

    async def test_get_last_ai_response_accepts_content_with_tool_calls(self):
        msgs = [
            AIMessage(content="根据搜索结果，以下是分析...", tool_calls=[]),
        ]
        assert _get_last_ai_response(msgs) is not None

        msgs2 = [
            AIMessage(content="我先介绍已知信息，同时查最新数据...",
                      tool_calls=[{"name": "get_detail", "args": {}, "id": "c1"}]),
        ]
        assert _get_last_ai_response(msgs2) is not None

    async def test_get_last_ai_response_skips_empty_content(self):
        msgs = [
            AIMessage(content="", tool_calls=[{"name": "search", "args": {}, "id": "c1"}]),
        ]
        assert _get_last_ai_response(msgs) is None

    @patch("agent.orchestrate.nodes.create_llm")
    async def test_shallow_mode_skips_critic(self, mock_create_llm):
        """[DEPRECATED Phase 10] depth="fast" 模式：Critic 已移除，验证 graph 正常完成。

        原测试验证 critic_status 保持 PENDING。Critic 已从图谱中移除，
        graph 直接从 reasoning_node → END。
        """
        mock_create_llm.return_value = make_mock_llm(content="根据搜索结果，巨人评分 8.5 分。")
        graph = build_graph(tools=MOCK_TOOLS)
        state = make_state(
            messages=[
                SystemMessage(content="..."),
                HumanMessage(content="搜巨人"),
                AIMessage(content="", tool_calls=[{"name": "mock_search_tool", "args": {"keyword": "巨人"}, "id": "call_x"}]),
            ],
            query_intent="lookup",
            depth="fast",
        )
        result = await graph.ainvoke(state)
        # Critic 已移除：graph 应正常完成，返回有效回复
        messages = result.get("messages", [])
        last_ai = [m for m in messages if isinstance(m, AIMessage) and m.content]
        assert len(last_ai) > 0
        assert "巨人" in str(last_ai[-1].content)
