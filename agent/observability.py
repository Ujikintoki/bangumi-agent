"""LangChain adapters. Only plain visible messages and allowlisted parameters cross core."""
import functools
import hashlib
import json
import threading
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage
from core.observability.runtime import active, begin, event, span


def messages_value(messages):
    return [{'message_id': getattr(m, 'id', None), 'role': m.type, 'content': m.content,
             'origin_kind': {'human': 'user_statement', 'ai': 'historical_assistant', 'tool': 'unknown'}.get(m.type, 'unknown'),
             'tool_call_id': getattr(m, 'tool_call_id', None),
             'tool_calls': getattr(m, 'tool_calls', [])} for m in messages if isinstance(m, BaseMessage)]


def result_status(value):
    if isinstance(value, dict) and any(k == '_error' or k.endswith('_error') for k in value):
        return 'partial' if any(bool(v) for k, v in value.items() if not k.endswith('_error') and isinstance(v, (dict, list))) else 'failed'
    return 'success'


class ModelObserver(BaseCallbackHandler):
    raise_error = False
    run_inline = True

    def __init__(self, stage):
        self.stage = stage
        self.runs = {}
        self.lock = threading.Lock()

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        scope = begin('model.invoke', stage=self.stage, sdk_run_id=str(run_id), provider_attempts='unknown')
        if scope is None:
            return
        with self.lock:
            self.runs[run_id] = scope
        invocation = kwargs.get('invocation_params', {})
        for key in ('model', 'temperature', 'max_tokens', 'tool_choice'):
            value = invocation.get(key)
            if isinstance(value, (str, int, float, bool)) or value is None:
                scope.record['attributes'][key] = value
        tools = invocation.get('tools', [])
        scope.record['attributes']['schema_hash'] = hashlib.sha256(json.dumps(tools, sort_keys=True).encode()).hexdigest()
        scope.capture('model_input', lambda: messages_value(messages[0]))
        scope.capture('tool_definitions', lambda: tools)

    def on_llm_end(self, response, *, run_id, **kwargs):
        with self.lock:
            scope = self.runs.pop(run_id, None)
        if scope:
            try:
                message = response.generations[0][0].message
                scope.capture('model_output', lambda: messages_value([message]))
                usage = getattr(message, 'usage_metadata', None)
                if usage:
                    scope.record['usage'] = {k: usage.get(k) for k in ('input_tokens', 'output_tokens', 'total_tokens')}
                    scope.record['usage']['cache_read'] = usage.get('input_token_details', {}).get('cache_read')
                else:
                    tokens = (response.llm_output or {}).get('token_usage', {})
                    scope.record['usage'] = {'input_tokens': tokens.get('prompt_tokens'), 'output_tokens': tokens.get('completion_tokens'), 'cache_read': tokens.get('prompt_cache_hit_tokens')}
                scope.record['finish_reason'] = message.response_metadata.get('finish_reason')
            except Exception:
                scope.record['recording_complete'] = False
            finally:
                scope.finish()

    def on_llm_error(self, error, *, run_id, **kwargs):
        with self.lock:
            scope = self.runs.pop(run_id, None)
        if scope:
            scope.finish(error)


class ToolObserver(BaseCallbackHandler):
    raise_error = False
    run_inline = True

    def on_tool_error(self, error, **kwargs):
        scope = active()
        if scope and scope.record['name'] == 'tool.execute':
            validation = type(error).__name__ in {'ValidationError', 'ToolInvocationError'}
            scope.event('tool.validation', status='failed' if validation else 'passed', actual_execution=False if validation else 'unknown', error_type=type(error).__name__)
            scope.record['result_status'] = 'failed'

    def on_tool_end(self, output, **kwargs):
        scope = active()
        if scope and scope.record['name'] == 'tool.execute':
            scope.event('tool.validation', status='passed', actual_execution=True)
            value = output
            if hasattr(output, 'content'):
                try:
                    value = json.loads(output.content)
                except (ValueError, TypeError):
                    value = None
            scope.record['result_status'] = result_status(value)
            scope.capture('tool_result', lambda: value)


TOOL_OBSERVER = ToolObserver()


async def observe_tool_call(request, execute):
    call = request.tool_call
    with span('tool.execute', tool=call['name'], tool_call_id=call.get('id'), argument_keys=sorted(call.get('args', {}))) as scope:
        if scope:
            scope.capture('tool_arguments', lambda: call.get('args', {}))
            if request.tool is None:
                scope.event('tool.validation', status='failed', actual_execution=False, reason='unregistered')
        result = await execute(request)
        if scope and getattr(result, 'status', None) == 'error':
            scope.record['result_status'] = 'failed'
        return result


def observed(name):
    def decorate(function):
        @functools.wraps(function)
        async def wrapped(*args, **kwargs):
            with span(name) as scope:
                result = await function(*args, **kwargs)
                if scope and isinstance(result, dict):
                    scope.event('node.result', status=result.get('query_intent'), iteration=result.get('iterations'))
                return result
        return wrapped
    return decorate


def observed_route(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        route = function(*args, **kwargs)
        event('route.decide', stage=function.__name__, status=route, schema_version='routing-v1', reason='program_rule')
        return route
    return wrapped
