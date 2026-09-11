"""
图谱集成测试（mock LLM + mock 工具）— Phase 6 统一架构

验证跨模块耦合：critic_feedback 传播、memory 截断不破坏 graph、
消化态隔离、多轮状态一致性。
可独立运行: python -m pytest test/test_graph.py -v
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call, patch

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agent.graph import build_graph
from agent.config import get_max_iterations
from agent.memory.short_term import estimate_tokens
from agent.nodes.reasoning import reasoning_node
from test.conftest import MOCK_TOOLS, make_mock_llm, make_state

import pytest

pytestmark = pytest.mark.asyncio

_DEEP_MAX = get_max_iterations("deep")


def _classify_llm(intent: str = "fetch", confidence: float = 0.95):
    """mock 分类器 LLM：固定返回指定 intent。

    凡是要断言 ``query_intent`` 的用例都必须先打上它——真实分类器对同一句
    话有约 10% 的概率给出不同 intent（实测 "搜巨人" 20 次有 2 次返回
    explore），既飘又花钱。
    """
    mock = MagicMock()
    mock.bind_tools.return_value = mock
    mock.ainvoke = AsyncMock(
        return_value=AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "classify_intent",
                    "args": {"intent": intent, "confidence": confidence},
                    "id": "c0",
                }
            ],
        )
    )
    return mock


# ═══════════════════════════════════════════════════════════════════
# 1. 图谱端到端（mock LLM）
# ═══════════════════════════════════════════════════════════════════


class TestGraphIntegration:
    """端到端图谱：基本路径 + 熔断"""

    @patch("agent.nodes.reasoning.create_llm")
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

    @patch("agent.nodes.reasoning.create_llm")
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

    # 分类器必须 mock：真实分类对"搜巨人"有约 10% 概率给 explore，
    # 且命中 fetch 分支时还会真的去调 pipeline 的 LLM（付费 + 不确定）。
    @patch("agent.nodes.reasoning.create_llm")
    @patch("agent.nodes.pipeline.recall_memory_step", AsyncMock(return_value=""))
    @patch("agent.nodes.pipeline.get_agent_tools", lambda: MOCK_TOOLS)
    @patch("agent.nodes.pipeline.create_llm")
    @patch("agent.llm.create_classifier_llm", lambda *a, **k: _classify_llm("fetch"))
    async def test_query_intent_persists_across_rounds(
        self, mock_pipeline_llm, mock_create_llm
    ):
        mock_pipeline_llm.return_value = make_mock_llm(content="搜到了，评分 8.5。")
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

    @patch("agent.nodes.reasoning.create_llm")
    @patch("agent.nodes.reasoning.get_agent_tools")
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

        with patch("agent.nodes.reasoning.create_llm") as mock_create_llm:
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

        @patch("agent.nodes.reasoning.create_llm")
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

    @patch("agent.nodes.reasoning.create_llm")
    @patch("agent.nodes.pipeline.create_llm")
    @patch("agent.nodes.pipeline.get_agent_tools")
    async def test_shallow_mode_skips_critic(
        self, mock_pipeline_tools, mock_pipeline_llm, mock_create_llm
    ):
        """[DEPRECATED Phase 10] depth="fast" 模式：Critic 已移除，验证 graph 正常完成。

        原测试验证 critic_status 保持 PENDING。Critic 已从图谱中移除，
        graph 直接从 reasoning_node → END。
        """
        mock_pipeline_tools.return_value = MOCK_TOOLS
        mock_pipeline_llm.return_value = make_mock_llm(
            content="根据搜索结果，巨人评分 8.5 分。"
        )
        mock_create_llm.return_value = make_mock_llm(
            content="根据搜索结果，巨人评分 8.5 分。"
        )
        graph = build_graph(tools=MOCK_TOOLS)
        state = make_state(
            messages=[
                SystemMessage(content="..."),
                HumanMessage(content="搜巨人"),
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "mock_search_tool", "args": {"keyword": "巨人"}, "id": "call_x"}
                    ],
                ),
                ToolMessage(content="搜索结果", tool_call_id="call_x"),
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


# ═══════════════════════════════════════════════════════════════════
# 4. Subgraph 消息边界（P2）
# ═══════════════════════════════════════════════════════════════════


async def _invoke_pipeline_graph(intent: str, messages: list) -> dict:
    """驱动真实 ``build_graph`` 跑完一条 pipeline，返回最终 state。

    synthesize 的 mock LLM 返回纯文本（无 tool_calls）→ 隐式终止。
    """
    synth = make_mock_llm(content="搜到了，评分 9.1。")
    with (
        patch("agent.llm.create_classifier_llm", lambda *a, **k: _classify_llm(intent)),
        patch("agent.nodes.pipeline.create_llm", lambda *a, **k: synth),
        patch("agent.nodes.pipeline.get_agent_tools", lambda: MOCK_TOOLS),
        patch(
            "agent.nodes.pipeline.recall_memory_step",
            AsyncMock(return_value=""),
        ),
    ):
        graph = build_graph(tools=MOCK_TOOLS)
        return await graph.ainvoke(make_state(messages=messages))


class TestSubgraphMessageBoundary:
    """P2：子图被当节点用时，返回的是它自己的完整 state（含调用前收到的消息）。

    父图 ``messages`` 是 ``operator.add`` 语义，会把这份完整列表再追加一次，
    于是**输入消息整体重复入 state**。重复项随后写进 L1 session 缓存
    （``main.py`` 的 ``session_cache.store``），挤占多轮记忆预算。

    触发条件是 pipeline intent（fetch / realtime / profile）——它们走子图；
    ReAct（explore / discuss / fallback）在父图内用普通节点，不经过子图边界。
    """

    @pytest.mark.parametrize("intent", ["fetch", "realtime", "profile"])
    async def test_pipeline_intents_do_not_duplicate_input(self, intent):
        """三条 pipeline 都不得让输入消息重复入 state。"""
        question = "进击的巨人"
        state = await _invoke_pipeline_graph(intent, [HumanMessage(content=question)])

        msgs = state["messages"]
        humans = [m for m in msgs if isinstance(m, HumanMessage)]
        assert len(humans) == 1, (
            f"intent={intent}: 输入消息被重复追加，"
            f"实际 {len(humans)} 条 → {[m.content for m in msgs]}"
        )

    async def test_duplication_scales_with_history(self):
        """多轮会话下被复制的是**整个历史**，不是最后一条。

        这是 P2 真正的量级：L1 缓存裁剪到 20/30 条后，实际只剩约一半
        不重复的消息，表现为"聊几轮就忘事"。
        """
        history = [HumanMessage(content=f"历史第{i}轮") for i in range(6)]
        state = await _invoke_pipeline_graph("fetch", list(history))

        seen = [m.content for m in state["messages"] if isinstance(m, HumanMessage)]
        duplicated = [c for c in seen if seen.count(c) > 1]
        assert not duplicated, f"历史消息被整体复制: {duplicated}"

    async def test_react_intent_does_not_duplicate(self):
        """对照组：ReAct 不走子图边界，本就不该重复。

        断言存在的意义是证明上面的用例不是"恒红"——若本用例也红，
        说明环境或 mock 有问题，而不是 P2。
        """
        state = await _invoke_pipeline_graph("explore", [HumanMessage(content="推荐几部番")])

        msgs = state["messages"]
        humans = [m for m in msgs if isinstance(m, HumanMessage)]
        assert len(humans) == 1, f"ReAct 路径不应重复: {[m.content for m in msgs]}"

    async def test_subgraph_results_still_reach_parent(self):
        """修复不得把子图产出一起切掉——父图仍要拿到 pipeline 的结果。

        ``main.py`` 依赖最终 state 里的 AIMessage 做 render 与缓存。
        """
        state = await _invoke_pipeline_graph("fetch", [HumanMessage(content="进击的巨人")])

        ai_msgs = [m for m in state["messages"] if isinstance(m, AIMessage) and m.content]
        assert ai_msgs, f"子图产出丢失: {[type(m).__name__ for m in state['messages']]}"
        assert "9.1" in str(ai_msgs[-1].content)
