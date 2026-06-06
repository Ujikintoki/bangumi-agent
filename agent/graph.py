"""
LangGraph 图谱编排

核心拓扑：reasoning → (条件边) → tool/critic/END → (条件边) → END/retry

决策矩阵：
    - tool_calls 非空 → ToolNode → critic → END/retry
    - intent = chitchat → END（快速通道，跳过 critic）
    - 其他无工具调用 → critic → END/retry

Phase 3 Step 3 升级：tool_node 从占位实现切换为 LangGraph 内置 ``ToolNode``。
快速通道：chitchat 直接结束，不经过质量自省。factual 仍然经过 critic。
"""

from __future__ import annotations

import logging
from typing import Literal

from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from agent.nodes import critic_node, reasoning_node
from agent.state import AgentState
from tools.bgm_tools import get_agent_tools

logger = logging.getLogger("bgm-agent.graph")

# 最大允许的迭代轮次，超过此值强制终止以防无限递归
_MAX_ITERATIONS = 5


# ── 条件路由: reasoning → tool / critic ──────────────────────


# 快速通道意图：纯闲聊跳过 Critic，直接结束。
# 注意：factual 不走快速通道——"什么是三集定律"和"什么是麻辣仙人"
# 对 LLM 的难度不同，Critic 有真实价值。
_FAST_PATH_INTENTS = frozenset({"chitchat"})


def route_after_reasoning(state: AgentState) -> Literal["tool_node", "critic_node", "__end__"]:
    """reasoning_node 之后的条件边。

    三级路由决策：
        1. last_tool_calls 非空 → tool_node（执行工具后进 critic）
        2. query_intent = chitchat 且无工具调用 → END（快速通道，跳过 critic）
        3. 其他无工具调用 → critic_node（质量评估）

    快速通道的设计理由：闲聊和常识问答不需要质量检查。
    对于"你好"→"你好！"这类直接回复，Critic 没有可验证的维度。

    Args:
        state: 当前 Agent 全局状态。

    Returns:
        ``"tool_node"``、``"critic_node"`` 或 ``"__end__"``。
    """
    last_tool_calls = state.get("last_tool_calls", [])
    if last_tool_calls:
        logger.debug(
            "route_after_reasoning: last_tool_calls=%s → tool_node",
            [tc.get("name", "?") for tc in last_tool_calls],
        )
        return "tool_node"

    query_intent = state.get("query_intent", "unknown")
    if query_intent in _FAST_PATH_INTENTS:
        logger.debug("route_after_reasoning: intent=%s → 快速通道 END", query_intent)
        return END

    logger.debug("route_after_reasoning: intent=%s 无工具调用 → critic_node", query_intent)
    return "critic_node"


# ── 条件路由: critic → retry / END ───────────────────────────


def route_after_critic(state: AgentState) -> Literal["reasoning_node", "__end__"]:
    """自省节点后的条件边路由逻辑。

    决策矩阵：

        +----------------+----------------+----------------+
        | critic_status  | iterations < 3 | iterations >= 3|
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


# ── 图谱构建 ──────────────────────────────────────────────────


def build_graph(tools: list | None = None) -> StateGraph:
    """构建并编译 LangGraph 状态图。

    图谱拓扑::

                    START
                      │
                      ▼
               reasoning_node
                      │
                      ▼ (条件边: last_tool_calls? intent?)
               ┌──────┼──────────┐
               │      │          │
            ToolNode  │    chitchat/factual
               │      │     (快速通道)
               │   critic_node     │
               │      │            │
               │      ▼            │
               │  (PASS? 超限?)    │
               │   ┌──┴──┐        │
               │  END  reasoning   │
               │       (REVISE)    │
               └──────┴────────────┘
                      ▼
                     END

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
    graph.add_edge("tool_node", "critic_node")

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
