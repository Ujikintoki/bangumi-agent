"""Agent 编排模块 — LangGraph 状态管理与图谱编排。

Companion Agent: 单一 graph + state，depth 参数控制模式。

四层架构
========

  编排层 (orchestrate/)   — 怎么思考、查多深
  人格层 (persona/)       — 怎么说话、什么风格
  记忆层 (memory/)        — 能记住什么
  数据层 (clients/, tools/, rag/, database/) — 能查什么

顶层: state.py (AgentState) + graph.py (StateGraph) + llm.py (LLM 工厂)
"""

from agent.graph import agent_app, build_graph  # noqa: F401
from agent.state import AgentState  # noqa: F401

__all__ = ["AgentState", "agent_app", "build_graph"]
