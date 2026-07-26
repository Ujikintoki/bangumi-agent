"""Research Skill — 仅 depth=="deep" 时激活。

深度意图策略 + Critic 评估 prompt。
"""

from agent.research.prompts import (  # noqa: F401
    CRITIC_SYSTEM_PROMPT,
    INTENT_PROMPTS,
    TOOL_DEPENDENCY_CONSTRAINT,
    build_system_prompt,
)

__all__ = [
    "build_system_prompt",
    "CRITIC_SYSTEM_PROMPT",
    "INTENT_PROMPTS",
    "TOOL_DEPENDENCY_CONSTRAINT",
]
