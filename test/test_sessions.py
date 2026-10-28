"""真实临时 SQLite 生命周期、幂等、归属与删除竞争；不使用数据库服务。"""
import asyncio
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from application.conversations import Conversations
from core.config import get_settings
from main import app, session_store
from sessions.store import SessionError, SessionStore


def store_at(tmp_path):
    store = SessionStore(tmp_path / 'conversation.sqlite3')
    store.initialize()
    return store


def test_history_context_restart_and_delete(tmp_path):
    store = store_at(tmp_path)
    a = store.create('alice')
    b = store.create('alice')
    a = store.update_context('alice', a['id'], 0, {'corrections': '演出，不是剧情', 'progress': '第3集'})
    rid = str(uuid4())
    assert store.begin('alice', a['id'], rid, a['version'], '看看', ['fast', None]) == (True, 2)
    response = {'reply': '一起聊演出。', 'turn_result': {'status': 'success', 'sources': [{'url': 'https://bgm.tv/subject/1'}]}}
    store.complete('alice', a['id'], rid, 2, response)
    restarted = SessionStore(store.path)
    restarted.initialize()
    saved = restarted.read('alice', a['id'])
    assert saved['context']['progress'] == '第3集'
    assert saved['context_source'].startswith('user-edit:')
    assert saved['turns'][0]['response'] == response
    assert restarted.read('alice', b['id'])['turns'] == []
    with pytest.raises(SessionError, match='not_found'):
        restarted.read('bob', a['id'])
    restarted.delete('alice', a['id'], saved['version'])
    with restarted.connection() as db:
        assert db.execute('SELECT count(*) FROM turns').fetchone()[0] == 0
    with pytest.raises(SessionError, match='not_found'):
        restarted.complete('alice', a['id'], rid, 2, response)


def test_pending_restart_is_interrupted_and_not_replayed(tmp_path):
    store = store_at(tmp_path)
    sid = store.create('alice')['id']
    rid = str(uuid4())
    store.begin('alice', sid, rid, 0, '查询', ['fast', None])
    store.initialize()
    assert store.turn('alice', sid, rid)['status'] == 'interrupted'
    started, turn = store.begin('alice', sid, rid, 0, '查询', ['fast', None])
    assert not started
    assert turn['status'] == 'interrupted'


@pytest.mark.asyncio
async def test_replay_busy_cancellation_and_delete_during_execution(tmp_path):
    store = store_at(tmp_path)
    sid = store.create('alice')['id']
    started = asyncio.Event()
    finish = asyncio.Event()
    async def execute(**kwargs):
        started.set()
        await finish.wait()
        return {'reply': 'secret test material', 'turn_result': {'status': 'success'}}
    runner = AsyncMock(side_effect=execute)
    service = Conversations(store, runner)
    rid = str(uuid4())
    task = asyncio.create_task(service.send('alice', sid, rid, 0, 'original', 'fast', None))
    await asyncio.wait_for(started.wait(), 2)
    replay = await service.send('alice', sid, rid, 0, 'original', 'fast', None)
    assert replay['status'] == 'pending'
    with pytest.raises(SessionError, match='request_conflict'):
        await service.send('alice', sid, rid, 0, 'different', 'fast', None)
    with pytest.raises(SessionError, match='busy'):
        await service.send('alice', sid, str(uuid4()), 1, 'next', 'fast', None)
    store.delete('alice', sid, 1)
    finish.set()
    with pytest.raises(SessionError, match='not_found'):
        await task
    runner.assert_awaited_once()
    assert store.list('alice') == []
    sid = store.create('alice')['id']
    finish.clear()
    task = asyncio.create_task(service.send('alice', sid, str(uuid4()), 0, 'cancel', 'fast', None))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.read('alice', sid)['turns'][0]['status'] == 'interrupted'


@pytest.mark.asyncio
async def test_service_reuses_saved_history_without_tool_transcript(tmp_path):
    store = store_at(tmp_path)
    sid = store.create('alice')['id']
    runner = AsyncMock(return_value={'reply': 'first reply', 'turn_result': {'status': 'success', 'sources': [{'url': 'https://bgm.tv/subject/1'}]}})
    service = Conversations(store, runner)
    rid = str(uuid4())
    result = await service.send('alice', sid, rid, 0, 'first', 'fast', None)
    assert result['response']['turn_result']['sources'][0]['recorded_at']
    assert await service.send('alice', sid, rid, 0, 'first', 'fast', None) == result
    runner.assert_awaited_once()
    await service.send('alice', sid, str(uuid4()), 2, 'second', 'fast', None)
    assert runner.call_args.kwargs['history'] == [{'role': 'user', 'content': 'first'}, {'role': 'assistant', 'content': 'first reply'}]


def test_http_lifecycle_and_context_reach_agent_and_render():
    with TestClient(app) as client:
        session = client.post('/api/sessions').json()
        sid = session['id']
        context = {'object_url': 'https://bangumi.tv/subject/400602', 'corrections': '演出不是剧情', 'progress': '只看到第3集'}
        updated = client.patch(f'/api/sessions/{sid}/context', json={'version': 0, 'context': context}).json()
        assert updated['context']['object_url'] == 'https://bgm.tv/subject/400602'
        with (patch('main.agent_app.ainvoke', new=AsyncMock(return_value={'messages': [AIMessage(content='谈演出')], 'query_intent': 'discuss'})) as graph,
              patch('main.render_reply', new=AsyncMock(return_value='谈演出')) as render):
            payload = {'request_id': str(uuid4()), 'version': 1, 'message': '继续'}
            response = client.post(f'/api/sessions/{sid}/turns', json=payload)
            assert response.status_code == 200
            assert '演出不是剧情' in graph.call_args.args[0]['session_context']
            assert '第3集' in render.call_args.kwargs['conversation_context'][0]['content']
            assert client.post(f'/api/sessions/{sid}/turns', json=payload).json() == response.json()
            graph.assert_awaited_once()
        saved = client.get(f'/api/sessions/{sid}').json()
        assert saved['turns'][0]['response']['reply'] == '谈演出'
        assert client.delete(f'/api/sessions/{sid}?version=0').status_code == 409
        assert client.delete(f'/api/sessions/{sid}?version={saved["version"]}').status_code == 200
        assert client.get(f'/api/sessions/{sid}').status_code == 404


def test_http_owner_isolation(monkeypatch):
    monkeypatch.setattr(get_settings(), 'AUTH_ENABLED', True)
    monkeypatch.setattr(get_settings(), 'API_USER_TOKENS', {'alice': 'a' * 32, 'bob': 'b' * 32})
    a = {'Authorization': 'Bearer ' + 'a' * 32}
    b = {'Authorization': 'Bearer ' + 'b' * 32}
    with TestClient(app) as client:
        assert client.get('/api/sessions').status_code == 401
        sid = client.post('/api/sessions', headers=a).json()['id']
        assert client.get(f'/api/sessions/{sid}', headers=b).status_code == 404
        assert client.get('/api/sessions', headers=b).json() == []


def test_storage_commit_failure_does_not_claim_saved(monkeypatch):
    import sqlite3
    with TestClient(app) as client:
        sid = client.post('/api/sessions').json()['id']
        monkeypatch.setattr(session_store, 'complete', lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError('disk full')))
        with patch('main.agent_app.ainvoke', new=AsyncMock(return_value={'messages': [AIMessage(content='generated')], 'query_intent': 'discuss'})):
            response = client.post(f'/api/sessions/{sid}/turns', json={'request_id': str(uuid4()), 'version': 0, 'message': '保存失败探针'})
        assert response.status_code == 503
        assert 'generated' not in response.text
        assert session_store.read('anonymous', sid)['turns'][0]['status'] == 'pending'


def test_saved_context_conflict_and_supported_link_validation():
    with TestClient(app) as client:
        sid = client.post('/api/sessions').json()['id']
        assert client.patch(f'/api/sessions/{sid}/context', json={'version': 0, 'context': {'object_url': 'https://example.com/subject/1'}}).status_code == 422
        assert client.patch(f'/api/sessions/{sid}/context', json={'version': 0, 'context': {'progress': '第3集'}}).status_code == 200
        assert client.patch(f'/api/sessions/{sid}/context', json={'version': 0, 'context': {'progress': '第4集'}}).status_code == 409


def test_local_history_rejects_foreign_origin_and_rebinding_host():
    with TestClient(app) as client:
        assert client.get('/api/sessions', headers={'Origin': 'https://untrusted.example'}).status_code == 403
        assert client.post('/api/sessions', headers={'Origin': 'null'}).status_code == 403
        assert client.get('/api/sessions', headers={'Host': 'untrusted.example'}).status_code == 403
        assert client.get('/api/sessions', headers={'Origin': 'http://testserver'}).status_code == 200
