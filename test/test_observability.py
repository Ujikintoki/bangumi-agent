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


@pytest.mark.asyncio
async def test_modes_preserve_materials_and_actual_model_input(tmp_path):
    from agent.memory.short_term import manage_memory
    from agent.observability import ModelObserver
    from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolMessage
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    actual = []
    exported = None
    for mode in ('off', 'metadata', 'diagnostic'):
        runtime = Runtime(tmp_path / mode, level='off' if mode == 'off' else 'metadata')
        runtime.start()
        if mode == 'diagnostic':
            runtime.diagnostic('u', 's')
        messages = [SystemMessage(content='system invariant'), HumanMessage(content='旧历史。'*3000),
                    AIMessage(content='旧助手推断'), HumanMessage(content='C2/C3：从甲乙两条原评选一条；C4：不是剧情，是演出；C5：引用不等于我的观点。'),
                    AIMessage(content='', tool_calls=[{'name': 'opinions', 'args': {}, 'id': 'comments'}]),
                    ToolMessage(content=json.dumps({'subject_id': 100, 'comments': {'items': [
                        {'id': 11, 'author': '甲', 'content': '演出松散', 'truncated': False},
                        {'id': 12, 'author': '乙', 'content': '我喜欢剧情', 'truncated': True}]}}, ensure_ascii=False), tool_call_id='comments')]
        with runtime.turn('u','s','r'):
            selected = manage_memory(messages, max_tokens=600)
            model = FakeMessagesListChatModel(responses=[AIMessage(content='visible answer')], callbacks=[ModelObserver('reasoning')])
            await model.ainvoke(selected)
        actual.append([(m.type, m.content) for m in selected])
        runtime.close()
        export = runtime.query('u','s')
        if mode == 'metadata':
            assert '演出松散' not in json.dumps(export, ensure_ascii=False)
        if mode == 'diagnostic':
            exported = export
            inputs = [a['value'] for r in export['records'] for a in r.get('artifacts', []) if a['role']=='model_input']
            assert [(m['role'],m['content']) for m in inputs[0]] == actual[-1]
    assert actual[0] == actual[1] == actual[2]
    assert any(e['attributes'].get('omitted') for r in exported['records'] for e in r['events'])
    assert all(r['recording_complete'] for r in exported['records'])


def test_artifact_limits_and_lazy_capture(tmp_path):
    runtime = Runtime(tmp_path, artifact_limit=40, turn_limit=60, secrets=['synthetic-private-key'])
    runtime.start()
    with runtime.turn('u','s','r') as scope:
        scope.capture('disabled', lambda: (_ for _ in ()).throw(AssertionError('must stay lazy')))
        runtime.diagnostic('u','s')
        scope.capture('small', {'text': 'synthetic-private-key'})
        scope.capture('large', 'x'*80)
        scope.capture('object', object())
    runtime.close()
    exported = runtime.query('u','s')
    assert not exported['recording_complete']
    assert 'synthetic-private-key' not in json.dumps(exported)
    statuses = {a['status'] for r in exported['records'] for a in r.get('artifacts',[])}
    assert statuses == {'disabled','captured','truncated','unavailable'}
    files = list((tmp_path/'artifacts').rglob('*.json'))
    assert files and all((f.stat().st_mode & 0o777) == 0o600 for f in files)
    runtime.delete('u','s')
    assert not list((tmp_path/'artifacts').rglob('*.json'))
