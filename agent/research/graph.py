"""
LangGraph 图谱编排

核心拓扑
========

完整工具调用回合: reasoning → tool → reasoning → (条件边) → critic/END
无工具调用路径:   reasoning → (条件边) → critic/END → END/retry
快速通道:         reasoning → END（chitchat，跳过 tool 和 critic）

关键设计决策：tool_node 后回到 reasoning_node 而非 critic_node
-----------------------------------------------------------------
LLM 需要看到工具返回的结果才能生成自然语言回复。如果工具执行后直接进入
critic，LLM 没有机会消化工具数据——critic 评估的是"空壳"tool_call 消息
而非对工具结果的回应。

一轮完整的工具回合：reasoning（决定调工具）→ tool（执行）→ reasoning（消化结果，生成回复）

Critic 永远评估 LLM 在**看到工具结果后**生成的回复，这保证了：
- 工具返回空时，LLM 可以诚实告知"未找到"而非被 critic 误判
- 工具返回数据时，LLM 已被 prompt 要求生成文字回复后才到 critic
"""

from __future__ import annotations

import logging
from typing import Literal

from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from agent.research.nodes import critic_node, reasoning_node
from agent.research.state import _MAX_ITERATIONS, AgentState
from tools.bgm_tools import get_agent_tools

logger = logging.getLogger("bgm-agent.graph")


# ── 条件路由: reasoning → tool / critic ──────────────────────

# 快速通道意图：纯闲聊跳过 Critic，直接结束。
_FAST_PATH_INTENTS = frozenset({"chitchat"})


def route_after_reasoning(
    state: AgentState,
) -> Literal["tool_node", "critic_node", "__end__"]:
    """reasoning_node 之后的条件边。

    四级路由决策（优先级从高到低）：
        1. last_tool_calls 非空 + iterations < MAX → tool_node
        2. last_tool_calls 非空 + iterations >= MAX → critic_node（熔断，跳过工具）
        3. query_intent = chitchat 且无工具调用 → END（快速通道）
        4. 其他无工具调用 → critic_node（质量评估）

    新增熔断：当 tool → reasoning → tool 循环耗尽迭代预算时，
    即使还有待执行的 tool_calls，也强制进入 critic 终止。

    Args:
        state: 当前 Agent 全局状态。

    Returns:
        ``"tool_node"``、``"critic_node"`` 或 ``"__end__"``。
    """
    last_tool_calls = state.get("last_tool_calls", [])
    iterations = state.get("iterations", 0)

    if last_tool_calls:
        if iterations >= _MAX_ITERATIONS:
            logger.warning(
                "route_after_reasoning: 迭代已达上限 %d，跳过工具调用强制进入 critic",
                _MAX_ITERATIONS,
            )
            return "critic_node"
        logger.debug(
            "route_after_reasoning: last_tool_calls=%s → tool_node",
            [tc.get("name", "?") for tc in last_tool_calls],
        )
        return "tool_node"

    query_intent = state.get("query_intent", "unknown")
    if query_intent in _FAST_PATH_INTENTS:
        logger.debug("route_after_reasoning: intent=%s → 快速通道 END", query_intent)
        return END

    logger.debug(
        "route_after_reasoning: intent=%s 无工具调用 → critic_node", query_intent
    )
    return "critic_node"


# ── 条件路由: critic → retry / END ───────────────────────────
# 我们的问题：默认在最大iterarions中，一定能找到合适的工具调用并返回具体数据吗？如果不能，是否应该允许agent在没有工具调用的情况下直接修正回复，而不是强制要求工具调用？（因为有些问题可能确实不需要工具调用，或者工具调用无法提供有用信息）


def route_after_critic(state: AgentState) -> Literal["reasoning_node", "__end__"]:
    """自省节点后的条件边路由逻辑。

    决策矩阵：

        +----------------+----------------+----------------+
        | critic_status  | iterations < 5 | iterations >= 5|
        +================+================+================+
        | PASS           | → END          | → END          |
        +----------------+----------------+----------------+
        | REVISE         | → reasoning    | → END（强制）  |
        +----------------+----------------+----------------+

    Args:
        state: 当前 Agent 全局状态。

    Returns:
        ``"reasoning_node"`` 回到推理节点重试，或 ``"__end__"`` 结束图谱。
    """
    iterations = state.get("iterations", 0)
    status = state.get("critic_status", "PENDING")

    if iterations >= _MAX_ITERATIONS:
        logger.info("迭代次数已达上限 %d，强制终止", _MAX_ITERATIONS)
        return END

    if status == "PASS":
        logger.info("自省通过 (iterations=%d)，结束图谱", iterations)
        return END

    logger.info("自省要求修正 (iterations=%d)，返回 reasoning_node", iterations)
    return "reasoning_node"


# 自省后，不要重复调用工具，是否允许agent向用户询问更多信息或直接修正回复即可。工具调用的重复最多允许一次（reasoning → tool → critic → reasoning → critic），超过两次就强制结束，避免死循环。
# 还是有更明智的解决方案？

# ── 图谱构建 ──────────────────────────────────────────────────


def build_graph(tools: list | None = None) -> StateGraph:
    """构建并编译 LangGraph 状态图。

    图谱拓扑::

                        START
                          │
                          ▼
                   reasoning_node ◄─────────────────┐
                          │                         │
                          ▼ (条件边: tool_calls?)    │
                   ┌──────┼──────────┐              │
                   │      │          │              │
               ToolNode   │      chitchat           │
                   │      │      (快速通道)          │
                   └──────┤          │              │
                          │          ▼              │
                   (回到 reasoning   END             │
                    消化工具结果)                    │
                          │                         │
                          ▼ (条件边: 无 tool_calls)  │
                     critic_node                    │
                          │                         │
                          ▼ (条件边: PASS? 超限?)    │
                   ┌──────┴──────┐                  │
                   │             │                  │
                  END     reasoning_node (REVISE) ──┘

    一轮完整工具回合: reasoning → tool → reasoning → critic → END/retry
    无工具路径:       reasoning → critic → END/retry
    快速通道:         reasoning → END

    Args:
        tools: LangChain 工具列表。None 时自动加载 ``get_agent_tools()``。
            测试时可注入 mock 工具以避免真实 API 调用。

    Returns:
        编译后的 ``StateGraph`` 实例，可直接调用 ``.invoke()``。
    """
    if tools is None:
        tools = get_agent_tools()

    graph = StateGraph(AgentState)

    # ── 注册节点 ──────────────────────────────────────────
    graph.add_node("reasoning_node", reasoning_node)
    graph.add_node("tool_node", ToolNode(tools, handle_tool_errors=True))
    graph.add_node("critic_node", critic_node)

    # ── 固定边 ────────────────────────────────────────────
    graph.add_edge(START, "reasoning_node")
    graph.add_edge("tool_node", "reasoning_node")  # 工具结果需LLM消化后再到critic

    # ── 条件边 1: reasoning → tool / critic / END ──────────
    graph.add_conditional_edges(
        "reasoning_node",
        route_after_reasoning,
        {
            "tool_node": "tool_node",
            "critic_node": "critic_node",
            END: END,
        },
    )

    # ── 条件边 2: critic → retry 或 END ───────────────────
    graph.add_conditional_edges(
        "critic_node",
        route_after_critic,
        {
            "reasoning_node": "reasoning_node",
            END: END,
        },
    )

    logger.info("Agent 图谱编译完成（%d 个工具）", len(tools))
    return graph.compile()


# ── 模块级编译实例 ──────────────────────────────────────────

agent_app = build_graph()
"""预编译的 Agent 图谱实例，可直接 ``agent_app.invoke(state)`` 调用。"""
