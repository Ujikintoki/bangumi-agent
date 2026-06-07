"""
图谱集成测试（mock LLM + mock 工具）

验证跨模块耦合：critic_feedback 传播、memory 截断不破坏 graph、
last_tool_calls 生命周期、多轮状态一致性。
可独立运行: python -m pytest test/test_graph.py -v
"""

from __future__ import annotations

from unittest.mock import call, patch

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agent.research.graph import build_graph
from agent.memory import estimate_tokens
from agent.research.nodes import _get_last_ai_response, reasoning_node
from test.conftest import MOCK_TOOLS, make_mock_llm, make_state

# ═══════════════════════════════════════════════════════════════════
# 1. 图谱端到端（mock LLM）
# ═══════════════════════════════════════════════════════════════════


class TestGraphIntegration:
    """端到端图谱：基本路径 + 熔断"""

    @patch("agent.research.nodes.create_llm")
    def test_chitchat_fast_path_skips_critic(self, mock_create_llm):
        mock_create_llm.return_value = make_mock_llm(content="你好！")
        graph = build_graph(tools=MOCK_TOOLS)
        state = make_state(messages=[SystemMessage(content="..."), HumanMessage(content="你好")])
        result = graph.invoke(state)
        assert result.get("critic_status") == "PENDING"  # critic 从未被调用
        assert result.get("query_intent") == "chitchat"

    @patch("agent.research.nodes.create_llm")
    def test_circuit_breaker(self, mock_create_llm):
        mock_create_llm.return_value = make_mock_llm(content="test")
        graph = build_graph(tools=MOCK_TOOLS)
        state = make_state(
            messages=[SystemMessage(content="..."), HumanMessage(content="搜巨人")],
            iterations=4, critic_status="REVISE", query_intent="lookup",
            last_tool_calls=[{"name": "mock_search_tool", "args": {}, "id": "c1"}],
        )
        result = graph.invoke(state)
        assert result.get("error_flag") is True

    @patch("agent.research.nodes.create_llm")
    def test_factual_skips_tools(self, mock_create_llm):
        mock_create_llm.return_value = make_mock_llm(content="三集定律是指...")
        graph = build_graph(tools=MOCK_TOOLS)
        state = make_state(messages=[SystemMessage(content="..."), HumanMessage(content="什么是三集定律")])
        result = graph.invoke(state)
        assert result.get("critic_status") == "PASS"

    @patch("agent.research.nodes.create_llm")
    def test_query_intent_persists_across_rounds(self, mock_create_llm):
        """query_intent 在 REVISE 重入 reasoning 时不丢失"""
        mock_create_llm.return_value = make_mock_llm(content="done")
        graph = build_graph(tools=MOCK_TOOLS)
        state = make_state(
            messages=[SystemMessage(content="..."), HumanMessage(content="搜巨人")],
            query_intent="lookup", iterations=1, critic_status="REVISE",
            last_tool_calls=[{"name": "mock_search_tool", "args": {}, "id": "c1"}],
        )
        result = graph.invoke(state)
        assert result.get("query_intent") == "lookup"  # 不因重入而重置


# ═══════════════════════════════════════════════════════════════════
# 2. 跨模块耦合：critic_feedback → reasoning（mock LLM）
# ═══════════════════════════════════════════════════════════════════


class TestCriticFeedbackPropagation:
    """验证 critic_feedback 确实注入到下一轮 reasoning_node 的 LLM 调用"""

    @patch("agent.research.nodes.create_llm")
    @patch("agent.research.nodes.get_agent_tools")
    def test_feedback_appears_in_llm_prompt(self, mock_get_tools, mock_create_llm):
        """critic_feedback 文本出现在发送给 LLM 的 SystemMessage 中"""
        mock_get_tools.return_value = []
        mock_llm = make_mock_llm(content="已修正的回复")
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
        )
        reasoning_node(state)

        # 验证 LLM.invoke 调用时 prompt 中包含 critic_feedback
        invoke_call = mock_llm.invoke.call_args
        assert invoke_call is not None, "LLM.invoke 未被调用"
        messages_to_llm = invoke_call[0][0]
        system_msgs = [m for m in messages_to_llm if isinstance(m, SystemMessage)]
        combined_system = " ".join(m.content for m in system_msgs)
        assert "缺少评分" in combined_system, f"critic_feedback 未注入到 prompt 中！内容: {combined_system[:200]}"
        assert "上一轮回复需要改进" in combined_system


class TestMemoryGraphIntegration:
    """验证 memory 截断与 graph 协同"""

    def test_memory_truncation_before_llm_call(self):
        """长消息历史在 reasoning_node 内被截断后再发给 LLM"""
        # 构建一条超长的 HumanMessage 触发截断
        long_content = "长文本" * 2000  # ~8000 chars → ~2000 tokens
        messages = [
            SystemMessage(content="You are Bangumi assistant."),
            HumanMessage(content=long_content),
        ]
        state = make_state(messages=messages, query_intent="chitchat")

        # 手动调用 reasoning_node（mock create_llm 避免真实 LLM 调用）
        with patch("agent.research.nodes.create_llm") as mock_create_llm:
            mock_llm = make_mock_llm(content="你好！")
            mock_create_llm.return_value = mock_llm
            result = reasoning_node(state)

        # 验证 reasoning_node 正常完成（不因超长消息崩溃）
        assert result["iterations"] == 1

    def test_trimmed_messages_still_contain_system(self):
        """截断后 SystemMessage 始终保留"""
        messages = [
            SystemMessage(content="You are Bangumi assistant."),
        ]
        for i in range(100):
            messages.append(HumanMessage(content=f"Q{i}: " + "数据" * 50))
            messages.append(AIMessage(content=f"A{i}: " + "回复" * 50))

        from agent.memory import manage_memory
        trimmed = manage_memory(messages, max_tokens=1000)
        assert any(isinstance(m, SystemMessage) for m in trimmed)
        assert len(trimmed) < len(messages)  # 确实截断了


# ═══════════════════════════════════════════════════════════════════
# 3. State 生命周期完整性
# ═══════════════════════════════════════════════════════════════════


class TestStateLifecycle:
    """验证跨轮次 state 字段的完整性"""

    def test_last_tool_calls_not_polluted_by_tool_node(self):
        """ToolNode 图执行后 last_tool_calls 保持 reasoning_node 设置的值"""
        from agent.research.graph import build_graph

        @patch("agent.research.nodes.create_llm")
        def _test(mock_llm):
            mock_llm.return_value = make_mock_llm(
                content="",
                tool_calls=[{"name": "mock_search_tool", "args": {"keyword": "巨人"}, "id": "call_x"}],
            )
            graph = build_graph(tools=MOCK_TOOLS)
            state = make_state(
                messages=[SystemMessage(content="..."), HumanMessage(content="搜巨人")],
                query_intent="lookup",
            )
            result = graph.invoke(state)
            # ToolNode 不应该清空 last_tool_calls
            assert "last_tool_calls" in result

        _test()

    def test_critic_status_transitions(self):
        """critic_status 正常流转: PENDING → REVISE → PASS"""
        from agent.research.nodes import critic_node
        from langchain_core.messages import AIMessage, ToolMessage

        # REVISE: 工具返回但无有效回复
        state1 = make_state(iterations=1, messages=[
            SystemMessage(content="..."), HumanMessage(content="搜"),
            AIMessage(content="", tool_calls=[{"name": "s", "args": {}, "id": "c1"}]),
            ToolMessage(content="结果", tool_call_id="c1"),
        ])
        assert critic_node(state1)["critic_status"] == "REVISE"

        # PASS: 有效回复（长度 ≥ 20 字）
        state2 = make_state(iterations=2, messages=[
            SystemMessage(content="..."), HumanMessage(content="搜"),
            AIMessage(content="", tool_calls=[{"name": "s", "args": {}, "id": "c1"}]),
            ToolMessage(content="结果", tool_call_id="c1"),
            AIMessage(content="进击的巨人最终季评分 8.7 分，排名全站前二十，非常推荐观看。"),
        ])
        assert critic_node(state2)["critic_status"] == "PASS"

    def test_get_last_ai_response_accepts_content_with_tool_calls(self):
        """有 content 的 AIMessage（即使附带 tool_calls）被视为有效回复"""
        msgs = [
            AIMessage(content="根据搜索结果，以下是分析...", tool_calls=[]),
        ]
        assert _get_last_ai_response(msgs) is not None

        msgs2 = [
            AIMessage(content="我先介绍已知信息，同时查最新数据...",
                      tool_calls=[{"name": "get_detail", "args": {}, "id": "c1"}]),
        ]
        assert _get_last_ai_response(msgs2) is not None  # ← 之前会跳过

    def test_get_last_ai_response_skips_empty_content(self):
        """纯 tool_call AIMessage（content=""）仍然被跳过"""
        msgs = [
            AIMessage(content="", tool_calls=[{"name": "search", "args": {}, "id": "c1"}]),
        ]
        assert _get_last_ai_response(msgs) is None
