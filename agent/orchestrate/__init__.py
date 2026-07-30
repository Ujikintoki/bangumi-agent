"""编排层 — 怎么思考、查多深、用什么工具。

Companion Agent 的推理核心：reasoning node、intent strategies、prompt 组装。
Phase 10: critic_node 已移除。
"""

from agent.orchestrate.nodes import reasoning_node  # noqa: F401
from agent.orchestrate.strategies import COMPANION_INTENT_PROMPTS  # noqa: F401

__all__ = [
    "COMPANION_INTENT_PROMPTS",
    "reasoning_node",
]
