"""人格层 — 怎么说话、什么风格。

CharacterProfile 定义角色性格，Render 层把数据回复转成角色聊天风格。
"""

from agent.persona.profiles import (  # noqa: F401
    AGENT_REGISTRY,
    BANGUMI_CHARACTER,
    CHARACTER_REGISTRY,
    COMPANION_PROFILE,
    NEUTRAL_CHARACTER,
    get_agent_profile,
    get_character,
)
from agent.persona.render import render_reply  # noqa: F401

__all__ = [
    "AGENT_REGISTRY",
    "BANGUMI_CHARACTER",
    "CHARACTER_REGISTRY",
    "COMPANION_PROFILE",
    "NEUTRAL_CHARACTER",
    "get_agent_profile",
    "get_character",
    "render_reply",
]
