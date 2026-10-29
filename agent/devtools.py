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


@dataclasses.dataclass
class ToolCallRecord:
    """单次工具边界记录；只保留参数名，不保留参数值。"""

    tool: str
    elapsed_ms: int
    status: str
    argument_keys: list[str]
    error_type: str | None = None

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


class RequestTelemetry:
    """Compatibility view of the current trace; owns no separate counters."""
    def __init__(self):
        from core.observability.runtime import current, Runtime
        self.t_start = time.monotonic()
        self.runtime = current.get()[0] if current.get() else Runtime()
        self.identity = current.get()
        self.context = None

    def to_dict(self):
        from core.observability.runtime import summary
        if not self.identity:
            return summary([])
        runtime, owner, session, request = self.identity
        runtime.flush()
        records = runtime.query(owner, session, content=False)['records']
        return summary([r for r in records if r['request'] == request])

    @property
    def llm_calls(self):
        return self.to_dict()['llm_calls']

    @property
    def tool_calls(self):
        return self.to_dict()['tool_calls']

    @property
    def node_timings(self):
        return self.to_dict()['node_timings']

    def add_llm_call(self, record):
        from core.observability.runtime import span
        with span('model.invoke', stage=record.label) as scope:
            if scope:
                scope.record['elapsed_ms'] = record.elapsed_ms
                scope.record['usage'] = {'input_tokens': record.prompt_tokens, 'output_tokens': record.completion_tokens}

    def add_tool_call(self, record):
        from core.observability.runtime import span
        with span('tool.execute', tool=record.tool, argument_keys=record.argument_keys) as scope:
            if scope:
                scope.record['result_status'] = record.status

    def add_node_timing(self, timing):
        # Interval estimates are not execution spans and cannot become a second timing source.
        from core.observability.runtime import event
        event('legacy.timing_omitted', reason='interval_estimate')


_current_telemetry = ContextVar('telemetry_compatibility', default=None)


def get_current_telemetry():
    return _current_telemetry.get()


def set_current_telemetry(value):
    from core.observability.runtime import current
    previous = _current_telemetry.get()
    if previous and previous.context:
        previous.context.__exit__(None, None, None)
        previous.context = None
    _current_telemetry.set(value)
    if value and current.get() is None:
        import uuid
        value.context = value.runtime.turn('legacy', uuid.uuid4().hex, uuid.uuid4().hex)
        value.context.__enter__()
    if value:
        value.identity = current.get()


def instrument_tools(tools):
    """Legacy direct-call adapter. Graph uses the ToolNode seam instead."""
    from langchain_core.tools import StructuredTool
    from agent.observability import result_status
    from core.observability.runtime import span
    wrapped = []
    for original in tools:
        if getattr(original, '_trace_compatibility', False):
            wrapped.append(original)
            continue
        async def invoke(_tool=original, **kwargs):
            with span('tool.execute', tool=_tool.name, argument_keys=sorted(kwargs)) as scope:
                result = await _tool.ainvoke(kwargs)
                if scope:
                    scope.record['result_status'] = result_status(result)
                return result
        tool = StructuredTool(name=original.name, description=original.description,
                              args_schema=original.args_schema, coroutine=invoke,
                              return_direct=getattr(original, 'return_direct', False))
        object.__setattr__(tool, '_trace_compatibility', True)
        wrapped.append(tool)
    return wrapped


class TelemetryLLMWrapper:
    """Legacy SDK adapter; observation is installed once on the underlying model."""
    def __init__(self, llm, label, telemetry):
        from agent.observability import ModelObserver
        self._llm, self._label, self._telemetry = llm, label, telemetry
        callbacks = list(llm.callbacks or [])
        if not any(isinstance(c, ModelObserver) for c in callbacks):
            llm.callbacks = [*callbacks, ModelObserver(label)]

    async def ainvoke(self, *args, **kwargs):
        return await self._llm.ainvoke(*args, **kwargs)

    def bind_tools(self, tools, **kwargs):
        return self._llm.bind_tools(tools, **kwargs)

    def __getattr__(self, name):
        return getattr(self._llm, name)
