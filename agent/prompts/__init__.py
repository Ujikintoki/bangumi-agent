"""Prompt 模板 — 所有 System Prompt、Scene Hint、工具配置。

Aggregator prompt 由 ``build_aggregator_prompt()`` 动态组装。
"""

from agent.prompts.aggregator import (  # noqa: F401
    TOOL_GUIDANCE,
    _LAST_CHANCE_DIGEST_HINT,
    build_aggregator_prompt,
)
from agent.prompts.pipeline import (  # noqa: F401
    _DETAIL_NODE_PROMPT,
    _PROFILE_NODE_PROMPT,
    _REALTIME_NODE_PROMPT,
    _SEARCH_NODE_PROMPT,
    _SYNTHESIZE_NODE_PROMPT,
)
from agent.prompts.scene_hints import (  # noqa: F401
    COMPANION_INTENT_PROMPTS,
    COMPANION_SCENE_HINTS,
)
from agent.prompts.scene_hints_deep import (  # noqa: F401
    DEEP_SCENE_HINTS,
)
from agent.prompts.tool_config import (  # noqa: F401
    TOOLS_BY_INTENT,
    get_tool_choice,
)

__all__ = [
    "COMPANION_INTENT_PROMPTS",
    "COMPANION_SCENE_HINTS",
    "DEEP_SCENE_HINTS",
    "TOOL_GUIDANCE",
    "TOOLS_BY_INTENT",
    "_DETAIL_NODE_PROMPT",
    "_LAST_CHANCE_DIGEST_HINT",
    "_PROFILE_NODE_PROMPT",
    "_REALTIME_NODE_PROMPT",
    "_SEARCH_NODE_PROMPT",
    "_SYNTHESIZE_NODE_PROMPT",
    "build_aggregator_prompt",
    "get_tool_choice",
]
