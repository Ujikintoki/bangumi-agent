"""
路由函数回归测试 — route_after_tool / route_after_tool_react

背景（P1）：父图与 3 个 pipeline 子图原先共用同一个 ``route_after_tool``，
但 4 个图的 path_map 各不相同。分类置信度落在 [0.5, 0.7) 时，
``route_after_classify`` 把 fetch/realtime 降级进 ReAct，却不改写
``query_intent``——于是 ``route_after_tool`` 按意图返回 ``"fetch_detail"`` /
``"synthesize"``，而父图 path_map 里没有这些键 → KeyError → 被 main.py 的
通用兜底吞成 HTTP 200 + "抱歉，处理请求时遇到了问题"。

修复：父图改用 ``route_after_tool_react``（不做 pipeline 步骤路由），
两道熔断与子图共用同一实现。

本文件锁定两件事：
  1. 父图路由的返回值恒在父图 path_map 内（P1 的"类"级不变量）
  2. 三道熔断仍然生效（修复前它们在 test/ 下命中 0 次）

可独立运行: python -m pytest test/test_routes.py -v
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph import END, START, StateGraph

from agent.config import get_max_iterations
from agent.routing.routes import route_after_tool, route_after_tool_react
from agent.state import AgentState

# 父图 tool_node 条件边的 path_map 键集合（graph.py 中 route_after_tool_react 那条）
_PARENT_PATH_MAP_KEYS = {"reasoning_node", END}

_EMPTY = '{"results": [], "total": 0}'
_HIT = '{"results": [{"id": 1}], "total": 1}'


# ═══════════════════════════════════════════════════════════════════
# 构造工具
# ═══════════════════════════════════════════════════════════════════


def _search_call(call_id: str, query: str = "巨人") -> AIMessage:
    """一条发起 search_bangumi_subject 的 AIMessage。"""
    return AIMessage(
        content="",
        tool_calls=[
            {"name": "search_bangumi_subject", "args": {"query": query}, "id": call_id}
        ],
    )


def _result(call_id: str, content: str) -> ToolMessage:
    """一条 search_bangumi_subject 的 ToolMessage。"""
    return ToolMessage(
        content=content, name="search_bangumi_subject", tool_call_id=call_id
    )


def _state(**overrides) -> AgentState:
    """构造路由函数所需的最小 state。"""
    base: dict = {
        "query_intent": "discuss",
        "iterations": 1,
        "depth": "fast",
        "messages": [_search_call("c1"), _result("c1", _HIT)],
    }
    base.update(overrides)
    return base  # type: ignore[return-value]


def _graph_with(router) -> object:
    """复刻父图 tool_node 条件边的接线，返回可 invoke 的编译图。

    Args:
        router: 挂在 tool_node 后的路由函数。

    Returns:
        编译后的最小 StateGraph（tool_node → routing → reasoning_node/END）。
    """
    g = StateGraph(AgentState)
    g.add_node("tool_node", lambda s: {})
    g.add_node("reasoning_node", lambda s: {})
    g.add_conditional_edges(
        "tool_node", router, {"reasoning_node": "reasoning_node", END: END}
    )
    g.add_edge(START, "tool_node")
    g.add_edge("reasoning_node", END)
    return g.compile()


# ═══════════════════════════════════════════════════════════════════
# 1. P1 回归 — 父图路由不得返回 pipeline 步骤名
# ═══════════════════════════════════════════════════════════════════


class TestParentReactRouter:
    """route_after_tool_react：父图专用路由。"""

    @pytest.mark.parametrize(
        "intent",
        ["chat", "fetch", "realtime", "profile", "explore", "discuss", "fallback", "unknown"],
    )
    def test_returns_only_parent_path_map_targets(self, intent):
        """任意意图下，返回值恒在父图 path_map 内。"""
        target = route_after_tool_react(_state(query_intent=intent))
        assert target in _PARENT_PATH_MAP_KEYS, (
            f"intent={intent} → {target!r} 不在父图 path_map {_PARENT_PATH_MAP_KEYS} 内"
        )

    def test_never_leaves_path_map_across_intent_and_iterations(self):
        """穷举 intent × iterations —— P1 的"类"级不变量。

        不只覆盖 fetch/realtime 两个已知触发值；日后新增 pipeline intent
        或新的降级路径时，这条断言仍会守住父图。
        """
        intents = [
            "chat", "fetch", "realtime", "profile",
            "explore", "discuss", "fallback", "unknown", "lookup", "discovery",
        ]
        for intent in intents:
            for iterations in range(0, 10):
                target = route_after_tool_react(
                    _state(query_intent=intent, iterations=iterations)
                )
                assert target in _PARENT_PATH_MAP_KEYS, (
                    f"intent={intent} iter={iterations} → {target!r} 不在父图 path_map 内"
                )

    @pytest.mark.parametrize("intent", ["fetch", "realtime"])
    def test_pipeline_intent_downgraded_into_react_does_not_raise(self, intent):
        """P1 现场：分类降级进 ReAct，query_intent 仍是 pipeline 值。

        修复前这里的 invoke 会抛 ``KeyError: 'fetch_detail'``。
        """
        app = _graph_with(route_after_tool_react)
        app.invoke(_state(query_intent=intent))  # 不应抛异常

    @pytest.mark.parametrize("intent", ["fetch", "realtime"])
    def test_old_wiring_would_have_raised_keyerror(self, intent):
        """反向锁定：父图若误用 route_after_tool，fetch/realtime 必抛 KeyError。

        这条断言证明上一条不是空转——它复现了修复前的真实故障。
        """
        app = _graph_with(route_after_tool)
        with pytest.raises(KeyError):
            app.invoke(_state(query_intent=intent))

    def test_routers_diverge_only_on_pipeline_step_routing(self):
        """父图与子图路由的唯一区别就是 pipeline 步骤路由。"""
        st = _state(query_intent="fetch", iterations=1)

        # 子图：按步骤推进 → 进入 detail
        assert route_after_tool(st) == "fetch_detail"
        # 父图：继续 ReAct 循环
        assert route_after_tool_react(st) == "reasoning_node"
        # 正是这个子图返回值会炸父图
        assert route_after_tool(st) not in _PARENT_PATH_MAP_KEYS


# ═══════════════════════════════════════════════════════════════════
# 2. 三道熔断 — 父图 ReAct 路径
# ═══════════════════════════════════════════════════════════════════


class TestCircuitBreakersOnReactPath:
    """三道熔断在父图路由上仍然生效（修复不得以删除安全阀为代价）。"""

    @pytest.mark.parametrize(
        ("intent", "max_iter"),
        [("discuss", 4), ("explore", 3), ("fetch", 3), ("realtime", 2), ("fallback", 2)],
    )
    def test_hard_breaker_stops_at_max_iterations(self, intent, max_iter):
        """硬熔断：iterations >= per-intent max → END，其前一轮仍继续。"""
        assert get_max_iterations("fast", intent) == max_iter, "前提校验：上限值变了"

        assert route_after_tool_react(
            _state(query_intent=intent, iterations=max_iter)
        ) == END
        assert route_after_tool_react(
            _state(query_intent=intent, iterations=max_iter - 1)
        ) == "reasoning_node"

    def test_empty_search_breaker_after_two_empty_searches(self):
        """空搜索熔断：同一轮内两个搜索都返回空 → END。

        注意 ``_count_consecutive_empty_searches`` 遇到带 tool_calls 的
        AIMessage 会中断计数，所以"连续两次"指一轮内的并行搜索，
        而非跨轮——构造测试时必须用同一条 AIMessage 带两个 tool_calls。
        """
        msgs = [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "search_bangumi_subject", "args": {"query": "AAA"}, "id": "c1"},
                    {"name": "search_bangumi_subject", "args": {"query": "BBB"}, "id": "c2"},
                ],
            ),
            _result("c1", _EMPTY),
            _result("c2", _EMPTY),
        ]
        assert route_after_tool_react(_state(messages=msgs)) == END

    def test_single_empty_search_does_not_trip_breaker(self):
        """单次空搜索不熔断——还有换关键词的余地。"""
        msgs = [_search_call("c1", "AAA"), _result("c1", _EMPTY)]
        assert route_after_tool_react(_state(messages=msgs)) == "reasoning_node"

    def test_duplicate_tool_call_breaker(self):
        """重复调用熔断：连续两轮同工具同参数 → END。"""
        msgs = [
            _search_call("c1", "AAA"),
            _result("c1", _HIT),
            _search_call("c2", "AAA"),
            _result("c2", _HIT),
        ]
        assert route_after_tool_react(_state(messages=msgs)) == END

    def test_different_args_not_flagged_as_duplicate(self):
        """换关键词重试不算重复调用。"""
        msgs = [
            _search_call("c1", "AAA"),
            _result("c1", _HIT),
            _search_call("c2", "BBB"),
            _result("c2", _HIT),
        ]
        assert route_after_tool_react(_state(messages=msgs)) == "reasoning_node"


# ═══════════════════════════════════════════════════════════════════
# 3. 子图路由 — route_after_tool 原语义不变
# ═══════════════════════════════════════════════════════════════════


class TestSubgraphRouter:
    """route_after_tool：pipeline 子图专用，步骤路由语义保持不变。"""

    @pytest.mark.parametrize(
        ("iterations", "expected"),
        [(1, "fetch_detail"), (2, "synthesize")],
    )
    def test_fetch_pipeline_steps(self, iterations, expected):
        """fetch：search → detail → synthesize。"""
        assert route_after_tool(
            _state(query_intent="fetch", iterations=iterations)
        ) == expected

    def test_fetch_empty_search_early_stop_skips_detail(self):
        """空搜索早停：search 无结果时跳过 detail 直接 synthesize。"""
        msgs = [_search_call("c1", "AAA"), _result("c1", _EMPTY)]
        assert route_after_tool(
            _state(query_intent="fetch", iterations=1, messages=msgs)
        ) == "synthesize"

    @pytest.mark.parametrize("intent", ["realtime", "profile"])
    def test_realtime_profile_first_step_goes_to_synthesize(self, intent):
        """realtime / profile：search → synthesize，无中间步骤。"""
        assert route_after_tool(
            _state(query_intent=intent, iterations=1)
        ) == "synthesize"

    def test_fetch_hard_breaker_still_applies(self):
        """子图同样受硬熔断约束。"""
        assert route_after_tool(
            _state(query_intent="fetch", iterations=3)
        ) == END

    @pytest.mark.parametrize("intent", ["discuss", "explore", "fallback"])
    def test_react_intents_still_fall_through_to_reasoning(self, intent):
        """ReAct 意图走子图路由时仍回 reasoning_node。"""
        assert route_after_tool(
            _state(query_intent=intent, iterations=1)
        ) == "reasoning_node"


# ═══════════════════════════════════════════════════════════════════
# 4. 真实接线 — 编译图实际挂载的路由函数
# ═══════════════════════════════════════════════════════════════════


class TestCompiledGraphWiring:
    """锁定 graph.py 的真实接线。

    前三组测的是路由函数本身；这一组防的是"函数改对了、图却接回旧函数"——
    若父图重新挂上 route_after_tool，P1 会原样复活，而前面所有测试仍然全绿。
    """

    def _build(self):
        from agent.graph import build_graph
        from test.conftest import MOCK_TOOLS

        return build_graph(tools=MOCK_TOOLS)

    def test_parent_tool_node_uses_react_router(self):
        """父图 tool_node 必须挂 route_after_tool_react，且 path_map 无 pipeline 步骤名。"""
        branches = self._build().builder.branches["tool_node"]

        assert len(branches) == 1, "父图 tool_node 应只有一条条件边"
        spec = next(iter(branches.values()))
        assert spec.path.func is route_after_tool_react
        assert set(spec.ends) == _PARENT_PATH_MAP_KEYS

    @pytest.mark.parametrize(
        "subgraph", ["fetch_pipeline", "realtime_pipeline", "profile_pipeline"]
    )
    def test_subgraph_tool_nodes_still_use_pipeline_router(self, subgraph):
        """三个 pipeline 子图的 tool 节点仍挂 route_after_tool（步骤路由不变）。"""
        sub = self._build().builder.nodes[subgraph].runnable
        branches = sub.builder.branches["tool"]

        assert len(branches) == 1, f"{subgraph} 的 tool 应只有一条条件边"
        spec = next(iter(branches.values()))
        assert spec.path.func is route_after_tool
