"""
[Phase 6] 此模块已迁移至 agent/state.py。
保留此文件以兼容旧 import 路径，请迁移到 ``from agent.state import AgentState, get_max_iterations``。
"""

from agent.state import (  # noqa: F401
    _MAX_ITERATIONS_DEEP as _MAX_ITERATIONS,
    AgentState,
    Depth,
    get_max_iterations,
)
