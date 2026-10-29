"""生命周期故障矩阵：用事件控制竞争，不依赖真实模型、网络或数据库服务。"""
import asyncio
import sqlite3
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from application.conversations import Conversations
from sessions.lifecycle import TERMINAL
from sessions.store import SessionError, SessionStore


def setup(tmp_path, execute, **kwargs):
    store = SessionStore(tmp_path / 'lifecycle.sqlite3')
    store.initialize()
    sid = store.create('owner')['id']
    return store, sid, Conversations(store, execute, **kwargs)


def args(sid, version=0):
    return ('owner', sid, str(uuid4()), version, 'test', 'fast', None)


async def settled(service, call):
    run = service.runs.get(call[:3])
    if run:
        await asyncio.wait_for(asyncio.shield(run.task), 1)
    return service.inspect(*call[:3])


@pytest.mark.asyncio
async def test_disconnect_does_not_cancel_and_replay_does_not_execute_twice(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    async def execute(**kwargs):
        entered.set()
        await release.wait()
        return {'reply': 'saved', 'turn_result': {'status': 'partial', 'sources': []}}
    runner = AsyncMock(side_effect=execute)
    store, sid, service = setup(tmp_path, runner)
    call = args(sid)
    waiter = asyncio.create_task(service.send(*call))
    await asyncio.wait_for(entered.wait(), 1)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert service.inspect(*call[:3])['status'] == 'running'
    assert service.submit(*call)['status'] == 'running'
    release.set()
    turn = await settled(service, call)
    assert turn['status'] == 'completed'
    assert turn['response']['turn_result']['status'] == 'partial'
    assert service.submit(*call) == turn
    runner.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('before_start', [True, False])
async def test_cancel_ack_and_terminal_cancel_is_idempotent(tmp_path, before_start):
    entered = asyncio.Event()
    async def execute(**kwargs):
        entered.set()
        await asyncio.Event().wait()
    store, sid, service = setup(tmp_path, execute)
    call = args(sid)
    assert service.submit(*call)['status'] == 'accepted'
    if not before_start:
        await asyncio.wait_for(entered.wait(), 1)
    assert service.cancel(*call[:3])['status'] == 'cancelling'
    turn = await settled(service, call)
    assert turn['status'] == 'cancelled'
    assert turn['response'] is None
    assert service.cancel(*call[:3]) == turn
    assert service.submit(*call) == turn


@pytest.mark.asyncio
async def test_completion_wins_when_already_committed(tmp_path):
    store, sid, service = setup(tmp_path, AsyncMock(return_value={'reply': 'done', 'turn_result': {'status': 'success'}}))
    call = args(sid)
    await service.send(*call)
    assert service.cancel(*call[:3])['status'] == 'completed'
    with pytest.raises(SessionError):
        store.transition('owner', sid, call[2], 'running')


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,reason', [('timeout', 'timeout'), ('exception', 'execution')])
async def test_deadline_or_exception_ends_and_allows_next_turn(tmp_path, mode, reason):
    async def execute(**kwargs):
        if mode == 'exception':
            raise RuntimeError('synthetic-secret')
        await asyncio.Event().wait()
    store, sid, service = setup(tmp_path, execute, timeout_for=lambda depth: .01)
    call = args(sid)
    turn = await service.send(*call)
    assert (turn['status'], turn['reason']) == ('failed', reason)
    assert 'synthetic-secret' not in str(turn)
    await asyncio.sleep(0)
    next_call = args(sid, store.read('owner', sid)['version'])
    service.submit(*next_call)
    await service.close()


@pytest.mark.asyncio
async def test_storage_failure_retains_outcome_and_only_retries_commit(tmp_path, monkeypatch):
    runner = AsyncMock(return_value={'reply': 'generated once', 'turn_result': {'status': 'success'}})
    store, sid, service = setup(tmp_path, runner)
    original = store.complete
    monkeypatch.setattr(store, 'complete', lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError('full')))
    call = args(sid)
    with pytest.raises(sqlite3.Error):
        await service.send(*call)
    assert store.turn(*call[:3])['response'] is None
    with pytest.raises(SessionError, match='busy'):
        service.submit(*args(sid, 1))
    monkeypatch.setattr(store, 'complete', original)
    assert service.inspect(*call[:3])['response']['reply'] == 'generated once'
    runner.assert_awaited_once()
    assert not service.runs


@pytest.mark.asyncio
async def test_delete_cancels_worker_and_never_restores_content(tmp_path):
    entered = asyncio.Event()
    async def execute(**kwargs):
        entered.set()
        await asyncio.Event().wait()
    store, sid, service = setup(tmp_path, execute)
    call = args(sid)
    service.submit(*call)
    run = service.runs[call[:3]]
    await asyncio.wait_for(entered.wait(), 1)
    store.delete('owner', sid, 1)
    service.deleted('owner', sid)
    await asyncio.wait_for(asyncio.shield(run.task), 1)
    assert store.list('owner') == []
    with pytest.raises(SessionError, match='not_found'):
        service.inspect(*call[:3])


@pytest.mark.asyncio
async def test_shutdown_interrupts_and_rejects_new_admission(tmp_path):
    entered = asyncio.Event()
    async def execute(**kwargs):
        entered.set()
        await asyncio.Event().wait()
    store, sid, service = setup(tmp_path, execute)
    call = args(sid)
    service.submit(*call)
    await asyncio.wait_for(entered.wait(), 1)
    await service.close()
    assert store.turn(*call[:3])['status'] == 'interrupted'
    with pytest.raises(SessionError, match='shutting_down'):
        service.submit(*args(sid, store.read('owner', sid)['version']))


@pytest.mark.parametrize('state', ['accepted', 'running', 'cancelling', 'pending'])
def test_restart_recovers_all_unfinished_states_and_keeps_terminal(tmp_path, state):
    store, sid, _ = setup(tmp_path, None)
    call = args(sid)
    store.begin(*call[:4], call[4], [call[5], call[6]])
    with store.connection() as db:
        db.execute('UPDATE turns SET status=?', (state,))
    restarted = SessionStore(store.path)
    restarted.initialize()
    assert restarted.turn(*call[:3])['status'] == 'interrupted'
    assert restarted.turn(*call[:3])['reason'] == 'service_restart'
    assert restarted.begin(*call[:4], call[4], [call[5], call[6]])[0] is False


@pytest.mark.asyncio
async def test_cancel_suppression_cannot_publish_late_success_or_overlap(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    async def execute(**kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
        return {'reply': 'late result', 'turn_result': {'status': 'success'}}
    store, sid, service = setup(tmp_path, execute, timeout_for=lambda depth: .02)
    call = args(sid)
    service.submit(*call)
    await asyncio.wait_for(entered.wait(), 1)
    service.cancel(*call[:3])
    run = service.runs[call[:3]]
    await asyncio.wait_for(asyncio.shield(run.task), 1)
    assert store.turn(*call[:3])['status'] == 'failed'
    assert store.turn(*call[:3])['reason'] == 'cancellation_timeout'
    with pytest.raises(SessionError, match='busy'):
        service.submit(*args(sid, 2))
    release.set()
    await asyncio.wait_for(run.worker, 1)
    await asyncio.sleep(0)
    assert store.turn(*call[:3])['response'] is None


@pytest.mark.asyncio
async def test_capacity_does_not_reject_replay_or_leak_registration(tmp_path):
    store, sid, service = setup(tmp_path, AsyncMock(), max_active=1)
    call = args(sid)
    service.submit(*call)
    assert service.submit(*call)['request_id'] == call[2]
    other = store.create('owner')['id']
    with pytest.raises(SessionError, match='capacity'):
        service.submit(*args(other))
    assert store.read('owner', other)['turns'] == []
    await service.close()


def test_http_accepted_poll_cancel_and_wrong_owner(monkeypatch):
    from fastapi.testclient import TestClient
    from core.config import get_settings
    from main import app, conversations
    async def execute(**kwargs):
        await asyncio.Event().wait()
    monkeypatch.setattr(conversations, 'execute', execute)
    monkeypatch.setattr(get_settings(), 'AUTH_ENABLED', True)
    monkeypatch.setattr(get_settings(), 'API_USER_TOKENS', {'owner':'a' * 32, 'other':'b' * 32})
    headers = {'Authorization':'Bearer ' + 'a' * 32}
    wrong = {'Authorization':'Bearer ' + 'b' * 32}
    with TestClient(app) as client:
        sid = client.post('/api/sessions', headers=headers).json()['id']
        rid = str(uuid4())
        body = {'request_id':rid, 'version':0, 'message':'synthetic lifecycle'}
        response = client.post(f'/api/sessions/{sid}/turns', json=body, headers=headers)
        assert response.status_code == 202
        assert response.json()['status'] == 'accepted'
        path = f'/api/sessions/{sid}/turns/{rid}'
        assert client.post(path + '/cancel', headers=wrong).status_code == 404
        assert client.get(path, headers=wrong).status_code == 404
        assert client.post(f'/api/sessions/{sid}/turns', json=body, headers=headers).status_code == 202
        assert client.post(path + '/cancel', headers=headers).status_code == 200
        # TestClient 的下一个请求让后台取消结局完成提交。
        turn = client.get(path, headers=headers).json()
        assert turn['status'] == 'cancelled'
        assert client.post(path + '/cancel', headers=headers).json() == turn


def test_legacy_schema_migration_is_additive_and_repeatable(tmp_path):
    path = tmp_path / 'old.sqlite3'
    with sqlite3.connect(path) as db:
        db.executescript('''
        CREATE TABLE sessions(id TEXT PRIMARY KEY,owner TEXT,title TEXT,updated_at TEXT,version INTEGER,context TEXT,context_source TEXT);
        CREATE TABLE turns(session_id TEXT,request_id TEXT,message TEXT,fingerprint TEXT,status TEXT,created_at TEXT,response TEXT,PRIMARY KEY(session_id,request_id));
        INSERT INTO sessions VALUES('sid','owner','old','timestamp',1,'{}',NULL);
        INSERT INTO turns VALUES('sid','rid','old message','hash','pending','timestamp',NULL);
        ''')
    store = SessionStore(path)
    store.initialize()
    turn = store.turn('owner', 'sid', 'rid')
    assert turn['message'] == 'old message'
    assert turn['status'] == 'interrupted'
    store.initialize()
    assert store.turn('owner', 'sid', 'rid') == turn
    with store.connection() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 1
        db.execute('PRAGMA user_version=999')
    with pytest.raises(SessionError, match='unsupported_schema'):
        store.initialize()


@pytest.mark.asyncio
async def test_commit_succeeded_but_ack_failed_reconciles_without_second_execution(tmp_path, monkeypatch):
    runner = AsyncMock(return_value={'reply':'saved once','turn_result':{'status':'success'}})
    store, sid, service = setup(tmp_path, runner)
    original = store.complete
    def lost_ack(*args):
        original(*args)
        raise sqlite3.OperationalError('simulated readback failure after commit')
    monkeypatch.setattr(store, 'complete', lost_ack)
    call = args(sid)
    result = await service.send(*call)
    assert result['status'] == 'completed'
    assert result['response']['reply'] == 'saved once'
    runner.assert_awaited_once()
    assert store.read('owner', sid)['version'] == 2
