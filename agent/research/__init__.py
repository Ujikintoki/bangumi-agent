"""Research Agent — 深度研究助手。

3 节点 ReAct 拓扑 + Critic 质量自省。准确 > 速度。
"""

from agent.research.graph import agent_app, build_graph
from agent.research.state import AgentState

__all__ = ["AgentState", "agent_app", "build_graph"]
