"""
[Phase 6] 此模块已迁移至 agent/graph.py。
保留此文件以兼容旧 import 路径，请迁移到 ``from agent.graph import agent_app, build_graph``。
"""

from agent.graph import (  # noqa: F401
    agent_app,
    build_graph,
    route_after_critic,
    route_after_reasoning as route_after_research_reasoning,
)
