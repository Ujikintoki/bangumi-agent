"""
[Phase 6] 此模块已迁移至 agent/nodes.py。
保留此文件以兼容旧 import 路径，请迁移到 ``from agent.nodes import reasoning_node, critic_node``。
"""

from agent.nodes import (  # noqa: F401
    _extract_user_input,
    _get_last_ai_response,
    critic_node,
    reasoning_node as research_reasoning_node,
)
