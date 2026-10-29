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


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['normal','error','cancel','timeout','commit_failure','confirmation_failure'])
async def test_lifecycle_modes_replay_and_commit(tmp_path, outcome):
    from application.conversations import Conversations
    from sessions.store import SessionStore
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from agent.observability import ModelObserver
    inputs, results = [], []
    for mode in ('off','metadata','diagnostic'):
        root = tmp_path / mode
        runtime = Runtime(root / 'trace', level='off' if mode=='off' else 'metadata')
        runtime.start()
        store = SessionStore(root/'sessions.sqlite3')
        store.initialize()
        sid = store.create('u')['id']
        if mode=='diagnostic':
            runtime.diagnostic('u',sid)
        entered = asyncio.Event()
        observed = []
        class GateModel(FakeMessagesListChatModel):
            async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
                observed.append([(m.type,m.content) for m in messages])
                entered.set()
                if outcome in {'cancel','timeout'}:
                    await asyncio.Event().wait()
                if outcome=='error':
                    raise RuntimeError('private provider body')
                return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)
        model = GateModel(responses=[AIMessage(content='synthetic answer')], callbacks=[ModelObserver('reasoning')])
        async def execute(**kwargs):
            from agent.observability import invoke_model
            message = await invoke_model(model, 'synthetic correction')
            return {'reply':message.content,'turn_result':{'status':'success'}}
        service = Conversations(store, execute, observer=runtime, timeout_for=lambda _: .02 if outcome=='timeout' else 1)
        original = store.complete
        failures = [True]
        def complete(*args, **kwargs):
            if outcome=='commit_failure' and failures and failures.pop():
                import sqlite3
                raise sqlite3.OperationalError('isolated failure')
            if outcome=='confirmation_failure' and failures and failures.pop():
                original(*args, **kwargs)
                import sqlite3
                raise sqlite3.OperationalError('confirmation failure after commit')
            return original(*args, **kwargs)
        store.complete = complete
        call = ('u',sid,'rid',0,'request','fast',None)
        service.submit(*call)
        run = service.runs[call[:3]]
        await asyncio.wait_for(entered.wait(),1)
        if outcome=='cancel':
            service.cancel(*call[:3])
        await asyncio.wait_for(asyncio.shield(run.task),2)
        if run.worker and not run.worker.done():
            await asyncio.gather(run.worker, return_exceptions=True)
            await asyncio.sleep(0)
        turn = service.inspect(*call[:3])
        assert service.submit(*call) == turn
        assert len(observed)==1
        inputs.append(observed)
        results.append((turn['status'],turn['reason'],turn['response']))
        await service.close()
        runtime.close()
        records = runtime.query('u',sid)['records']
        if mode!='off':
            assert len([r for r in records if r['name']=='turn.execute'])==1
            assert len([r for r in records if r['name']=='model.invoke'])==1
            assert all(r['status']!='running' for r in records), [(r['name'], r['status']) for r in records]
            assert any(r['name']=='session.commit' for r in records)
            if outcome in {'commit_failure','confirmation_failure'}:
                assert len([r for r in records if r['name']=='session.commit'])==2
    assert inputs[0]==inputs[1]==inputs[2]
    assert results[0]==results[1]==results[2]


def test_persistent_deletion_expiry_and_retention(tmp_path):
    from core.observability.maintenance import prune
    runtime = Runtime(tmp_path)
    runtime.start()
    runtime.diagnostic('u','s',1)
    with runtime.turn('u','s','r') as scope:
        scope.capture('content', 'synthetic body')
        runtime.scopes[('u','s')] = time.time()-1
        scope.capture('expired_scope', lambda: (_ for _ in ()).throw(AssertionError()))
    runtime.close()
    prune(runtime, now=time.time()+8*86400)
    records = runtime.query('u','s')['records']
    assert any(a['status']=='expired' for r in records for a in r.get('artifacts',[]))
    assert not list(tmp_path.glob('artifacts/*/*.json'))
    prune(runtime, now=time.time()+31*86400)
    assert runtime.query('u','s')['records']==[]
    runtime.delete('u','s')
    restarted = Runtime(tmp_path)
    restarted.start()
    with restarted.turn('u','s','late') as scope:
        scope.capture('late', 'must not return')
    restarted.close()
    assert restarted.query('u','s')['records']==[]
    with pytest.raises(ValueError):
        restarted.diagnostic('u','s')
    prune(restarted, now=time.time()+31*86400)


def test_bounded_queue_shutdown_and_sdk_failure(tmp_path, monkeypatch):
    runtime = Runtime(tmp_path, capacity=2)
    runtime.start()
    with runtime.storage_lock:
        with runtime.turn('u','s','r') as scope:
            for _ in range(20):
                scope.event('synthetic', status='success')
        runtime.close(timeout=.001)
        assert runtime.dropped and runtime.pending_bytes
    runtime.thread.join(1)
    runtime.close()
    assert not runtime.query('u','s')['recording_complete']
    memory = Runtime()
    def failure(*args, **kwargs):
        raise RuntimeError('SDK failure')
    monkeypatch.setattr(memory.tracer,'start_span',failure)
    with memory.turn('u','s','r'):
        pass
    assert memory.dropped


def test_owned_http_query_and_missing_records(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.observability import create_router
    from core.auth import authenticated_user
    from application.conversations import Conversations
    from sessions.store import SessionStore
    store = SessionStore(tmp_path/'sessions.sqlite3')
    store.initialize()
    sid = store.create('u')['id']
    runtime = Runtime(tmp_path/'trace')
    runtime.start()
    runtime.diagnostic('u',sid)
    with runtime.turn('u',sid,'rid') as scope:
        scope.capture('content','synthetic private content')
    runtime.flush()
    service = Conversations(store,None,observer=runtime)
    app = FastAPI()
    app.include_router(create_router(service))
    identity = ['u']
    app.dependency_overrides[authenticated_user] = lambda: identity[0]
    with TestClient(app) as client:
        path=f'/api/observability/sessions/{sid}'
        metadata=client.get(path)
        assert metadata.status_code==200
        assert 'synthetic private content' not in metadata.text
        detailed=client.get(path+'?content=true')
        assert 'synthetic private content' in detailed.text
        assert client.get(path,headers={'Origin':'https://untrusted.invalid'}).status_code==403
        identity[0]='other'
        assert client.get(path+'?content=true').status_code==404
        identity[0]='u'
        missing=client.get(path+'?request_id=missing').json()
        assert not missing['recording_complete'] and missing['reason']=='not_recorded'
    runtime.close()


@pytest.mark.asyncio
async def test_bound_model_callback_deduplication(tmp_path):
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from agent.observability import ModelObserver, invoke_model
    from core.observability.runtime import summary
    runtime=Runtime(tmp_path)
    runtime.start()
    model=FakeMessagesListChatModel(responses=[AIMessage(content='visible',usage_metadata={'input_tokens':100,'output_tokens':5,'total_tokens':105,'input_token_details':{'cache_read':80}})],callbacks=[ModelObserver('pipeline#1')])
    bound=model.bind(tools=[{'type':'function','function':{'name':'synthetic','parameters':{'type':'object'}}}],tool_choice='auto')
    runtime.diagnostic('u','s')
    with runtime.turn('u','s','r'):
        await invoke_model(bound,'question')
    runtime.close()
    records=runtime.query('u','s')['records']
    models=[r for r in records if r['name']=='model.invoke']
    assert len(models)==1 and models[0]['usage']['input_tokens']==100 and models[0]['usage']['cache_read']==80
    assert summary(records)['llm_calls'][0]['prompt_tokens']==100
    assert any(a['role']=='tool_definitions' and a['value'][0]['function']['name']=='synthetic' for a in models[0]['artifacts'])


@pytest.mark.asyncio
async def test_parallel_tools_and_shared_model_are_isolated(tmp_path):
    from agent.observability import ModelObserver, TOOL_OBSERVER, observe_tool_call, invoke_model
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.tools import tool
    from langgraph.prebuilt import ToolNode
    from langgraph.graph import StateGraph, START, END
    from agent.state import AgentState
    runtime=Runtime(tmp_path)
    runtime.start()
    model=FakeMessagesListChatModel(responses=[AIMessage(content='ok')],callbacks=[ModelObserver('reasoning')])
    entered=set()
    both=asyncio.Event()
    @tool
    async def parallel(value:int)->dict:
        """Synthetic parallel tool."""
        entered.add(value)
        if len(entered)==2:
            both.set()
        await both.wait()
        return {'value':value}
    parallel.callbacks=[TOOL_OBSERVER]
    graph=StateGraph(AgentState)
    graph.add_node('tools',ToolNode([parallel],awrap_tool_call=observe_tool_call))
    graph.add_edge(START,'tools')
    graph.add_edge('tools',END)
    tools=graph.compile()
    async def turn(session):
        with runtime.turn('u',session,'r'):
            await invoke_model(model,'fixed input')
            if session=='a':
                await tools.ainvoke({'messages':[AIMessage(content='',tool_calls=[{'name':'parallel','args':{'value':i},'id':f'call-{i}'} for i in (1,2)])]})
    await asyncio.wait_for(asyncio.gather(turn('a'),turn('b')),2)
    runtime.close()
    a=runtime.query('u','a')['records']
    b=runtime.query('u','b')['records']
    assert {r['trace'] for r in a}.isdisjoint({r['trace'] for r in b})
    calls=[r for r in a if r['name']=='tool.execute']
    assert len(calls)==2 and len({r['parent'] for r in calls})==1
    assert len([r for r in a+b if r['name']=='model.invoke'])==2


def test_total_quota_and_failed_deletion_visibility(tmp_path, monkeypatch):
    runtime=Runtime(tmp_path)
    runtime.start()
    runtime.total_limit=1
    with runtime.turn('u','full','r'):
        pass
    runtime.flush()
    assert runtime.dropped
    runtime.total_limit=256*1024*1024
    runtime.diagnostic('u','s')
    with runtime.turn('u','s','r') as scope:
        scope.capture('content','private synthetic text')
    runtime.flush()
    original=runtime._read_records
    def failure(*args):
        raise OSError('deletion I/O failed')
    monkeypatch.setattr(runtime,'_read_records',failure)
    with pytest.raises(OSError):
        runtime.delete('u','s')
    assert runtime.query('u','s')['reason']=='deleted'
    monkeypatch.setattr(runtime,'_read_records',original)
    runtime.close()
    recovered=Runtime(tmp_path)
    recovered.start()
    recovered.close()
    assert recovered.query('u','s')['records']==[]
    assert not list(tmp_path.glob('artifacts/*/*.json'))


@pytest.mark.asyncio
async def test_dev_mode_keeps_ordinary_graph_semantics(tmp_path, monkeypatch):
    import main
    import core.observability.runtime as observation
    from unittest.mock import AsyncMock
    outputs, inputs = [], []
    async def graph(state, **kwargs):
        inputs.append([(m.type,m.content) for m in state['messages']])
        return {**state,'query_intent':'chat','messages':list(state['messages'])}
    monkeypatch.setattr(main.agent_app,'ainvoke',AsyncMock(side_effect=graph))
    monkeypatch.setattr(main,'_run_with_telemetry',AsyncMock(side_effect=AssertionError('must use ordinary graph path')))
    monkeypatch.setattr(main,'render_reply',AsyncMock(return_value='synthetic visible reply'))
    for dev in (False,True):
        runtime=Runtime(tmp_path/str(dev))
        runtime.start()
        monkeypatch.setattr(observation,'_default',runtime)
        monkeypatch.setattr(main.settings,'DEV_MODE',dev)
        result=await main._chat(main.ChatRequest(message='synthetic',session_id='same',user_id='u'),history=[],managed=True)
        outputs.append(result.reply)
        if dev:
            assert result.telemetry['elapsed_ms']>0
        runtime.close()
    assert outputs[0]==outputs[1] and inputs[0]==inputs[1]
    assert main.agent_app.ainvoke.await_count==2
    main._run_with_telemetry.assert_not_awaited()


def test_log_filter_and_hidden_content_are_explicit(tmp_path):
    import logging
    from core.observability.runtime import CorrelationFilter
    runtime=Runtime(tmp_path,secrets=['synthetic-private'])
    runtime.start()
    runtime.diagnostic('u','s')
    with runtime.turn('u','s','r') as scope:
        scope.capture('visible',{'content':[{'type':'thinking','text':'hidden reasoning should never persist'},{'type':'text','text':'visible text'}], 'token':'synthetic-private'})
        record=logging.LogRecord('synthetic',logging.INFO,'',1,'credential %s',('synthetic-private',),None)
        CorrelationFilter().filter(record)
        assert record.trace_id!='-' and record.getMessage()=='credential [REDACTED]'
    runtime.close()
    value=runtime.query('u','s')
    raw=json.dumps(value)
    assert 'hidden reasoning should never persist' not in raw and 'synthetic-private' not in raw
    assert value['records'][0]['artifacts'][0]['redacted']


@pytest.mark.asyncio
async def test_commit_precedes_physical_quiescence(tmp_path):
    from application.conversations import Conversations
    from sessions.store import SessionStore
    runtime=Runtime(tmp_path/'trace')
    runtime.start()
    store=SessionStore(tmp_path/'sessions.sqlite3')
    store.initialize()
    sid=store.create('u')['id']
    release=asyncio.Event()
    async def execute(**kwargs):
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return {'reply':'late must never commit','turn_result':{'status':'success'}}
    service=Conversations(store,execute,observer=runtime,timeout_for=lambda _: .01)
    call=('u',sid,'r',0,'fixed','fast',None)
    service.submit(*call)
    run=service.runs[call[:3]]
    await asyncio.wait_for(run.task,1)
    assert store.turn(*call[:3])['status']=='failed'
    assert call[:3] in service.runs and not run.worker.done()
    runtime.flush()
    active_records=runtime.query('u',sid)['records']
    assert next(r for r in active_records if r['name']=='turn.execute')['status']=='running'
    assert next(r for r in active_records if r['name']=='session.commit')['status']=='success'
    release.set()
    await run.worker
    await asyncio.sleep(0)
    runtime.close()
    root=next(r for r in runtime.query('u',sid)['records'] if r['name']=='turn.execute')
    assert root['status']=='success' and root['commit_outcome']=='failed'
    assert len([e for e in root['events'] if e['name']=='execution.quiesced'])==1
    assert store.turn(*call[:3])['response'] is None


def test_processor_failure_and_per_turn_content_budget(tmp_path, monkeypatch):
    runtime=Runtime(tmp_path,artifact_limit=64,turn_limit=50)
    runtime.start()
    runtime.diagnostic('u','s')
    def broken_end(*args, **kwargs):
        raise RuntimeError('SDK processor fault')
    monkeypatch.setattr(runtime.processor,'on_end',broken_end)
    with runtime.turn('u','s','r') as scope:
        scope.capture('one','a'*30)
        scope.capture('two','b'*30)
    runtime.close()
    value=runtime.query('u','s')
    assert not value['recording_complete']
    assert [a['status'] for r in value['records'] for a in r['artifacts']]==['captured','truncated']
    assert not runtime.processor.active_records


@pytest.mark.asyncio
async def test_legacy_tool_wrapping_remains_single_count(tmp_path):
    from agent.devtools import instrument_tools
    from agent.graph import build_graph
    from agent.observability import TOOL_OBSERVER, observe_tool_call
    from langchain_core.tools import tool
    from langchain_core.messages import AIMessage
    from langgraph.prebuilt import ToolNode
    from langgraph.graph import StateGraph, START, END
    from agent.state import AgentState
    calls=[]
    @tool
    async def lookup(value:int)->dict:
        """Synthetic legacy compatibility tool."""
        calls.append(value)
        return {'value':value}
    lookup.callbacks=[TOOL_OBSERVER]
    wrapped=instrument_tools([lookup])[0]
    assert instrument_tools([wrapped])[0] is wrapped
    graph=StateGraph(AgentState)
    graph.add_node('tools',ToolNode([wrapped],awrap_tool_call=observe_tool_call))
    graph.add_edge(START,'tools')
    graph.add_edge('tools',END)
    runtime=Runtime(tmp_path)
    runtime.start()
    with runtime.turn('u','s','r'):
        await graph.compile().ainvoke({'messages':[AIMessage(content='',tool_calls=[{'name':lookup.name,'args':{'value':1},'id':'call'}])]})
    runtime.close()
    records=runtime.query('u','s')['records']
    assert calls==[1]
    assert len([r for r in records if r['name']=='tool.execute'])==1


@pytest.mark.asyncio
async def test_existing_callback_manager_and_legacy_model_view(tmp_path):
    from langchain_core.callbacks import BaseCallbackHandler
    from langchain_core.callbacks.manager import CallbackManager
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from agent.devtools import RequestTelemetry, TelemetryLLMWrapper, set_current_telemetry
    from agent.observability import invoke_model
    counts=[]
    class Counter(BaseCallbackHandler):
        def on_llm_end(self,*args,**kwargs):
            counts.append(1)
    runtime=Runtime(tmp_path)
    runtime.start()
    with runtime.turn('u','s','r'):
        view=RequestTelemetry()
        set_current_telemetry(view)
        model=FakeMessagesListChatModel(responses=[AIMessage(content='visible')],callbacks=CallbackManager([Counter()]))
        wrapped=TelemetryLLMWrapper(model,'reasoning',view)
        await invoke_model(wrapped,'input')
        assert len(view.llm_calls)==1 and view.llm_calls[0].label=='reasoning'
        set_current_telemetry(None)
    runtime.close()
    assert counts==[1]


def test_second_writer_cannot_recover_live_spans(tmp_path):
    import sqlite3
    runtime=Runtime(tmp_path)
    runtime.start()
    with runtime.turn('u','s','r'):
        runtime.flush()
        other=Runtime(tmp_path)
        with pytest.raises(sqlite3.OperationalError):
            other.start()
        other.close()
        assert runtime.query('u','s')['records'][0]['status']=='running'
    runtime.close()


def test_new_execution_does_not_inherit_another_turn_identity():
    runtime=Runtime()
    with runtime.turn('u','first','r1') as first:
        with runtime.turn('u','second','r2') as second:
            assert first.record['trace'] != second.record['trace']
            assert second.record['parent'] is None
    runtime.close()
