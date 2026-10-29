import asyncio
import json
import time
import pytest
from core.observability import Runtime, span


def test_scope_privacy_and_delete(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.start()
    with runtime.turn('u', 's', 'r'):
        with span('model.invoke') as scope:
            scope.capture('input', {'content': 'private'})
    runtime.close()
    assert 'private' not in json.dumps(runtime.query('u', 's'))
    assert not runtime.query('other', 's')['records']
    runtime.delete('u', 's')
    assert not runtime.query('u', 's')['records']


def test_diagnostic_and_failure(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.start()
    runtime.diagnostic('u', 's')
    with pytest.raises(ValueError):
        with runtime.turn('u', 's', 'r') as scope:
            scope.capture('input', {'api_key': 'secret', 'content': 'Bearer abc'})
            raise ValueError('private error')
    runtime.close()
    raw = json.dumps(runtime.query('u', 's'))
    assert 'secret' not in raw and 'Bearer abc' not in raw and 'private error' not in raw
    assert 'REDACTED' in raw and 'error' in raw


@pytest.mark.asyncio
async def test_parallel_cancel(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.start()
    async def run(s):
        with runtime.turn('u', s, 'r'):
            await asyncio.sleep(0)
            with span('child'):
                if s == 'a':
                    raise asyncio.CancelledError()
    await asyncio.gather(run('a'), run('b'), return_exceptions=True)
    runtime.close()
    a = runtime.query('u', 'a')['records']
    b = runtime.query('u', 'b')['records']
    assert len({r['trace'] for r in a+b}) == 2
    assert all(r['status'] == 'cancelled' for r in a)


def test_queue_failure_restart_schema(tmp_path):
    runtime = Runtime(tmp_path, byte_limit=1)
    runtime.start()
    with runtime.turn('u', 's', 'r'):
        pass
    runtime.close()
    assert runtime.dropped
    runtime = Runtime(tmp_path)
    runtime.start()
    with runtime.connect() as db:
        db.execute('pragma user_version=99')
    runtime.close()
    with pytest.raises(ValueError):
        Runtime(tmp_path).start()


def test_late_write_and_storage_failure(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.start()
    with runtime.turn('u', 's', 'r') as scope:
        runtime.delete('u', 's')
        scope.event('late')
    runtime.close()
    assert runtime.query('u', 's')['records'] == []
    runtime = Runtime(tmp_path / 'failed')
    runtime.start()
    def unavailable():
        raise OSError('private storage error')
    runtime.connect = unavailable
    with runtime.turn('u', 's', 'r'):
        pass
    runtime.close()
    assert runtime.dropped


def test_restart_and_no_import_io(tmp_path):
    runtime = Runtime(tmp_path / 'new')
    assert not runtime.path.exists()
    runtime.start()
    with runtime.turn('u', 's', 'r') as scope:
        record = dict(scope.record)
    runtime.close()
    record['status'] = 'running'
    with runtime.connect() as db:
        db.execute('update records set body=? where id=?', (json.dumps(record), record['id']))
    recovered = Runtime(runtime.path)
    recovered.start()
    recovered.close()
    result = recovered.query('u', 's')
    assert result['records'][0]['status'] == 'interrupted'
    assert not result['recording_complete']


@pytest.mark.asyncio
async def test_sdk_model_and_tool_seam(tmp_path):
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.tools import tool
    from langgraph.prebuilt import ToolNode
    from agent.observability import ModelObserver, TOOL_OBSERVER, observe_tool_call
    from core.observability.runtime import summary
    runtime = Runtime(tmp_path)
    runtime.start()
    @tool
    async def lookup(value: int) -> dict:
        """Synthetic lookup."""
        return {'value': value}
    lookup.callbacks = [TOOL_OBSERVER]
    model = FakeMessagesListChatModel(responses=[AIMessage(content='visible', usage_metadata={'input_tokens': 12, 'output_tokens': 2, 'total_tokens': 14})], callbacks=[ModelObserver('reasoning')])
    node = ToolNode([lookup], awrap_tool_call=observe_tool_call, handle_tool_errors=True)
    from langgraph.graph import StateGraph, START, END
    from agent.state import AgentState
    graph = StateGraph(AgentState)
    graph.add_node('tools', node)
    graph.add_edge(START, 'tools')
    graph.add_edge('tools', END)
    node = graph.compile()
    with runtime.turn('u', 's', 'r'):
        await model.ainvoke('question')
        await node.ainvoke({'messages': [AIMessage(content='', tool_calls=[{'name':'lookup','args':{'value': 1},'id':'tc1'}])]})
        await node.ainvoke({'messages': [AIMessage(content='', tool_calls=[{'name':'lookup','args':{'value': 'invalid'},'id':'tc2'}])]})
    runtime.close()
    records = runtime.query('u','s')['records']
    view = summary(records)
    assert len(view['llm_calls']) == 1
    assert view['llm_calls'][0]['prompt_tokens'] == 12
    assert len(view['tool_calls']) == 2
    failed = next(r for r in records if r['attributes'].get('tool_call_id') == 'tc2')
    assert failed['result_status'] == 'failed'
    assert any(e['attributes'].get('actual_execution') is False for e in failed['events'])
    assert all(r['parent'] for r in records if r['name'] != 'turn.execute')


@pytest.mark.asyncio
async def test_sdk_error_and_cancel_closes(tmp_path):
    from agent.observability import ModelObserver
    runtime = Runtime(tmp_path)
    runtime.start()
    observer = ModelObserver('classify')
    with runtime.turn('u','s','r'):
        observer.on_chat_model_start({}, [[]], run_id='one')
        observer.on_llm_error(ValueError('private'), run_id='one')
        observer.on_llm_error(ValueError(), run_id='one')
        observer.on_chat_model_start({}, [[]], run_id='two')
        observer.on_llm_error(asyncio.CancelledError(), run_id='two')
    runtime.close()
    models = [r for r in runtime.query('u','s')['records'] if r['name']=='model.invoke']
    assert len(models) == 2
    assert {r['status'] for r in models} == {'error','cancelled'}
