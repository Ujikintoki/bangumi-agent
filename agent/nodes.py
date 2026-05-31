"""
LangGraph 占位节点函数

所有节点均不调用真实 LLM 或外部 API，仅对 AgentState 做确定性修改，
用于验证图谱编排逻辑与控制流。

关键设计：reasoning_node 通过 ``needs_tool`` 字段自主决定是否进入
工具节点，避免"你好"类无工具意图的请求也被迫执行一次 Bangumi 查询。
"""

from __future__ import annotations

import logging

from agent.state import AgentState

logger = logging.getLogger("bgm-agent.nodes")

# ── 工具意图关键词（无头骨架的占位判定逻辑） ────────────────
# 正式接入 LLM 后，此硬编码列表将被 LLM 的 function-calling 决策替代。
_TOOL_INTENT_KEYWORDS: list[str] = [
    "搜索",
    "找",
    "推荐",
    "查",
    "帮我",
    "有没有",
    "哪些",
    "什么",
    "怎么",
    "search",
    "find",
    "recommend",
    "lookup",
]


def _detect_tool_intent(state: AgentState) -> bool:
    """检测消息历史中是否包含工具调用意图。

    无头骨架阶段使用关键词匹配作为占位判定。
    扫描**全部**消息（而非仅最后一条），确保跨轮次后意图信号不丢失。
    后续将由 LLM 的 function-calling 决策替代。

    Args:
        state: 当前 Agent 全局状态。

    Returns:
        ``True`` 表示需要调用工具，``False`` 表示可直接回复。
    """
    messages = state.get("messages", [])
    if not messages:
        return False
    # 扫描全部消息以防 tool/critic 消息淹没用户原始意图
    return any(any(kw in str(msg) for kw in _TOOL_INTENT_KEYWORDS) for msg in messages)


# ── 推理节点 ──────────────────────────────────────────────────


def reasoning_node(state: AgentState) -> dict:
    """推理节点（占位）：追加占位消息、递增迭代计数、判定工具意图。

    优雅降级：若 ``error_flag`` 已置位，仅追加兜底系统消息，
    不递增 iterations，不设置 needs_tool。

    工具路由：通过 ``_detect_tool_intent`` 判定 ``needs_tool``，
    下游条件边据此决定是否进入 tool_node。

    Args:
        state: 当前 Agent 全局状态。

    Returns:
        包含 messages、iterations、needs_tool 更新的字典。
    """
    if state.get("error_flag", False):
        logger.warning("reasoning_node: error_flag=True，进入兜底模式")
        return {
            "messages": ["[System] System busy, fallback triggered."],
        }

    new_iterations = state.get("iterations", 0) + 1
    needs_tool = _detect_tool_intent(state)

    logger.debug(
        "reasoning_node: iterations %d → %d, needs_tool=%s",
        state.get("iterations", 0),
        new_iterations,
        needs_tool,
    )
    return {
        "messages": [f"[Reasoning #{new_iterations}] Thinking..."],
        "iterations": new_iterations,
        "needs_tool": needs_tool,
    }


# ── 工具执行节点 ──────────────────────────────────────────────


def tool_node(state: AgentState) -> dict:
    """工具执行节点（占位）：追加工具调用成功占位消息。

    Args:
        state: 当前 Agent 全局状态。

    Returns:
        仅包含 ``messages`` 更新的字典。
    """
    logger.debug("tool_node: 追加 Tool execution successful.")
    return {
        "messages": ["[Tool] Tool execution successful."],
    }


# ── 自省节点 ──────────────────────────────────────────────────


def critic_node(state: AgentState) -> dict:
    """自省节点（占位）：依据迭代次数判定输出是否合格。

    规则（按优先级排序）：
        1. **熔断防御**：若 ``iterations >= 3``，强制通过并设置
           ``error_flag = True``，通知下游进入优雅降级。
        2. **常规逻辑**：``iterations < 2`` → ``"REVISE"``（需重试）；
           否则 → ``"PASS"``（合格）。

    Args:
        state: 当前 Agent 全局状态。

    Returns:
        包含 ``critic_status`` 和可能的 ``error_flag`` 更新的字典。
    """
    iterations = state.get("iterations", 0)

    # ── 熔断防御：超限时强制 PASS + 置 error_flag ─────────
    if iterations >= 3:
        logger.warning(
            "critic_node: 迭代次数=%d 已达上限，强制 PASS 并置 error_flag=True",
            iterations,
        )
        return {"critic_status": "PASS", "error_flag": True}

    # ── 常规逻辑 ──────────────────────────────────────────
    if iterations < 2:
        status = "REVISE"
    else:
        status = "PASS"
    logger.debug("critic_node: iterations=%d → %s", iterations, status)
    return {"critic_status": status}
