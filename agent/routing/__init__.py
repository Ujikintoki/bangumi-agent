"""条件边路由函数。"""

from agent.routing.routes import (  # noqa: F401
    route_after_classify,
    route_after_reasoning,
    route_after_tool,
)

__all__ = [
    "route_after_classify",
    "route_after_reasoning",
    "route_after_tool",
]
