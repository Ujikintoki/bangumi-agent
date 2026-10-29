"""LangChain adapters. Only plain visible messages and allowlisted parameters cross core."""
import functools
import hashlib
import json
import threading
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage
from core.observability import active, begin, event, span, reference, register_invocation_finalizer, invoke


def install_callback(target, observer):
    """Preserve caller callbacks and manager semantics while installing one observer."""
    existing = target.callbacks
    handlers = list(existing.handlers) if hasattr(existing, 'handlers') else list(existing or [])
    if any(type(handler) is type(observer) for handler in handlers):
        return
    if hasattr(existing, 'handlers'):
        manager = existing.copy()
        manager.add_handler(observer)
        target.callbacks = manager
    else:
        target.callbacks = [*handlers, observer]


def messages_value(messages):
    return [{'message_id': reference(m), 'sdk_message_id': getattr(m, 'id', None), 'role': m.type, 'content': visible_content(m.content),
             'origin_kind': {'human': 'unknown', 'ai': 'historical_assistant', 'tool': 'unknown'}.get(m.type, 'unknown'),
             'tool_call_id': getattr(m, 'tool_call_id', None),
             'tool_calls': getattr(m, 'tool_calls', []), 'invalid_tool_calls': getattr(m, 'invalid_tool_calls', [])} for m in messages if isinstance(m, BaseMessage)]


def visible_content(content):
    if isinstance(content, list):
        return [part for part in content if not isinstance(part, dict) or part.get('type') not in {'reasoning', 'thinking', 'analysis', 'redacted_thinking'}]
    return content


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
        register_invocation_finalizer(lambda error: self.close_pending(run_id, error))
        try:
            invocation = kwargs.get('invocation_params', {})
            for key in ('model', 'temperature', 'max_tokens', 'tool_choice'):
                value = invocation.get(key)
                if isinstance(value, (str, int, float, bool)) or value is None:
                    scope.set_attributes(**{key: value})
            tools = invocation.get('tools', [])
            scope.set_attributes(schema_hash=hashlib.sha256(json.dumps(tools, sort_keys=True).encode()).hexdigest())
            scope.capture('model_input', lambda: messages_value(messages[0]))
            scope.capture('tool_definitions', lambda: tools)
            scope.capture('model_parameters', lambda: {key: invocation.get(key) for key in ('model','temperature','max_tokens','max_completion_tokens','tool_choice','parallel_tool_calls','response_format','stop','seed','stream')})
        except Exception:
            scope.incomplete('adapter_conversion')
            scope.event('model.preparation_unavailable', reason='callback_conversion')

    def on_llm_end(self, response, *, run_id, **kwargs):
        with self.lock:
            scope = self.runs.pop(run_id, None)
        if scope:
            try:
                message = response.generations[0][0].message
                scope.capture('model_output', lambda: messages_value([message]))
                usage = getattr(message, 'usage_metadata', None)
                if usage:
                    scope.annotate(usage={**{k: usage.get(k) for k in ('input_tokens', 'output_tokens', 'total_tokens')}, 'cache_read': usage.get('input_token_details', {}).get('cache_read')})
                else:
                    tokens = (response.llm_output or {}).get('token_usage', {})
                    scope.annotate(usage={'input_tokens': tokens.get('prompt_tokens'), 'output_tokens': tokens.get('completion_tokens'), 'cache_read': tokens.get('prompt_cache_hit_tokens')})
                scope.annotate(finish_reason=message.response_metadata.get('finish_reason'))
                scope.annotate(response_model=message.response_metadata.get('model_name'))
            except Exception:
                scope.incomplete('adapter_conversion')
            finally:
                scope.finish()

    def close_pending(self, run_id, error):
        with self.lock:
            scope = self.runs.pop(run_id, None)
        if scope:
            if error is None:
                scope.incomplete('adapter_conversion')
                scope.event('model.callback_missing', reason='terminal_callback_absent')
            scope.finish(error, status='unavailable' if error is None else None)

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
        if scope and scope.name == 'tool.execute' and not getattr(scope, 'tool_terminal_recorded', False):
            scope.tool_terminal_recorded = True
            validation = type(error).__name__ in {'ValidationError', 'ToolInvocationError'}
            scope.event('tool.validation', status='failed' if validation else 'passed', actual_execution=False if validation else 'unknown', error_type=type(error).__name__)
            scope.annotate(result_status='failed')
            scope.annotate(error_type=type(error).__name__)

    def on_tool_end(self, output, **kwargs):
        scope = active()
        if scope and scope.name == 'tool.execute' and not getattr(scope, 'tool_terminal_recorded', False):
            scope.tool_terminal_recorded = True
            scope.event('tool.validation', status='passed', actual_execution=True)
            value = output
            if hasattr(output, 'content'):
                try:
                    value = json.loads(output.content)
                except (ValueError, TypeError):
                    value = None
            outcome = result_status(value)
            scope.annotate(result_status=outcome)
            if outcome in {'failed', 'partial'}:
                scope.annotate(error_type='structured_tool_error')
            scope.capture('tool_result', lambda: value)


TOOL_OBSERVER = ToolObserver()


async def observe_tool_call(request, execute):
    call = request.tool_call
    with span('tool.execute', tool=call['name'], tool_call_id=call.get('id'), argument_keys=sorted(call.get('args', {}))) as scope:
        if scope:
            if request.tool is not None:
                try:
                    schema = request.tool.get_input_schema().model_json_schema()
                    scope.set_attributes(schema_hash=hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest())
                    scope.capture('tool_schema', lambda: schema)
                except Exception:
                    scope.incomplete('adapter_conversion')
                    scope.event('tool.schema_unavailable', reason='schema_conversion')
            scope.capture('tool_arguments', lambda: call.get('args', {}))
            if request.tool is None:
                scope.event('tool.validation', status='failed', actual_execution=False, reason='unregistered')
        try:
            result = await execute(request)
        except BaseException as error:
            if scope:
                scope.event('tool.interrupted', status='cancelled' if type(error).__name__=='CancelledError' else 'error', actual_execution='unknown', error_type=type(error).__name__)
            raise
        if scope and getattr(result, 'status', None) == 'error':
            scope.annotate(result_status='failed')
        return result


def observed(name):
    def decorate(function):
        @functools.wraps(function)
        async def wrapped(*args, **kwargs):
            with span(name) as scope:
                if scope and name == 'response.render':
                    source = kwargs.get('turn_result') or {}
                    scope.capture('graph_output', lambda: messages_value(kwargs.get('messages', args[0] if args else [])))
                    scope.capture('render_sources', lambda: source)
                    scope.event('render.materials', references=[{'call_id': item.get('call_id'), 'tool': item.get('tool'), 'outcome': item.get('outcome')} for item in source.get('sources', [])])
                result = await function(*args, **kwargs)
                if scope and isinstance(result, dict):
                    scope.event('node.result', status=result.get('query_intent'), iteration=result.get('iterations'))
                    if 'messages' in result:
                        scope.capture('node_output', lambda: messages_value(result['messages']))
                if scope and name == 'response.render':
                    scope.capture('render_final', lambda: {'messages': messages_value(result[0]), 'reply': result[1]})
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


async def invoke_model(model, messages, *args, **kwargs):
    """Compatibility adapter for callers holding a model rather than a callable."""
    return await invoke(model.ainvoke, messages, *args, **kwargs)
