"""
开发者可观测性模块 — Token 统计 + 节点计时

通过 ``DEV_MODE=true`` 环境变量控制。开启后自动记录每次 /chat 请求的：
- 每次 LLM 调用的 prompt_tokens / completion_tokens / 耗时
- 每个 graph 节点的起止时间

输出为 JSON 友好的 dict，零侵入——调用方无感知，包装层透明拦截。
"""

from __future__ import annotations

import dataclasses
import logging
import time
from contextvars import ContextVar
from typing import Any

from langchain_core.messages import BaseMessage

logger = logging.getLogger("bgm-agent.devtools")

# ═══════════════════════════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════════════════════════


@dataclasses.dataclass
class LLMCallRecord:
    """单次 LLM 调用记录。"""

    label: str  # "reasoning#1", "render"
    elapsed_ms: int
    prompt_tokens: int
    completion_tokens: int

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "elapsed_ms": self.elapsed_ms,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }


@dataclasses.dataclass
class NodeTiming:
    """单个 graph 节点耗时。"""

    node: str  # "reasoning_node", "tool_node", "render_node", ...
    elapsed_ms: int

    def to_dict(self) -> dict:
        return {"node": self.node, "elapsed_ms": self.elapsed_ms}


class RequestTelemetry:
    """单次 /chat 请求的可观测性数据收集器。"""

    def __init__(self) -> None:
        self.t_start: float = time.monotonic()
        self.llm_calls: list[LLMCallRecord] = []
        self.node_timings: list[NodeTiming] = []

    def add_llm_call(self, record: LLMCallRecord) -> None:
        self.llm_calls.append(record)

    def add_node_timing(self, timing: NodeTiming) -> None:
        self.node_timings.append(timing)

    def to_dict(self) -> dict:
        return {
            "elapsed_ms": int((time.monotonic() - self.t_start) * 1000),
            "llm_calls": [c.to_dict() for c in self.llm_calls],
            "node_timings": [t.to_dict() for t in self.node_timings],
        }


# ═══════════════════════════════════════════════════════════════════════════
# ContextVar — per-request 线程/协程安全
# ═══════════════════════════════════════════════════════════════════════════

_current_telemetry: ContextVar[RequestTelemetry | None] = ContextVar(
    "telemetry", default=None
)


def get_current_telemetry() -> RequestTelemetry | None:
    """返回当前请求的 telemetry 实例，未启用时返回 None。"""
    return _current_telemetry.get()


def set_current_telemetry(t: RequestTelemetry | None) -> None:
    """设置当前请求的 telemetry 实例。"""
    _current_telemetry.set(t)


# ═══════════════════════════════════════════════════════════════════════════
# LLM Wrapper — 透明拦截 ainvoke()
# ═══════════════════════════════════════════════════════════════════════════


class TelemetryLLMWrapper:
    """包装 ChatOpenAI，拦截 ``ainvoke()`` 记录 token 消耗。

    ``bind_tools()`` 返回的绑定对象也会被自动包装，确保工具调用场景不漏记。
    所有其他方法透传给被包装的 LLM。
    """

    def __init__(self, llm: Any, label: str, telemetry: RequestTelemetry) -> None:
        self._llm = llm
        self._label = label
        self._telemetry = telemetry

    async def ainvoke(
        self, messages: list[BaseMessage], *args: Any, **kwargs: Any
    ) -> Any:
        t0 = time.monotonic()
        response = await self._llm.ainvoke(messages, *args, **kwargs)
        elapsed = (time.monotonic() - t0) * 1000

        usage = {}
        if hasattr(response, "response_metadata"):
            usage = response.response_metadata.get("token_usage", {})
            if not isinstance(usage, dict):
                usage = {}

        self._telemetry.add_llm_call(
            LLMCallRecord(
                label=self._label,
                elapsed_ms=int(elapsed),
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
            )
        )
        return response

    def bind_tools(
        self, tools: list, **kwargs: Any
    ) -> "TelemetryLLMWrapper":
        """返回包装后的绑定对象，确保工具调用场景的 token 统计不丢失。"""
        bound = self._llm.bind_tools(tools, **kwargs)
        return TelemetryLLMWrapper(bound, self._label, self._telemetry)

    def __getattr__(self, name: str) -> Any:
        """透传所有其他方法到被包装的 LLM。"""
        return getattr(self._llm, name)
