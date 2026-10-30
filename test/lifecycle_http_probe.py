"""隔离的真实 HTTP/进程验收。直接运行，不属于禁网络单元测试集。

只绑定回环随机端口、使用临时 SQLite 和替身执行；不读 .env，不访问模型或数据库服务。
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def serve(database, port, mode='configured'):
    from core.config import Settings, get_settings
    Settings.model_config['env_file'] = None
    os.environ.update(AUTH_ENABLED='false', ENVIRONMENT='test', MEMORY_ENABLED='false', RAG_ENABLED='false',
                      SESSION_DB_PATH=database, TRACE_PATH=str(Path(database).parent / 'observability'), RATE_LIMIT_PER_MINUTE='0', DEV_MODE='false', LLM_API_KEY='' if mode == 'unconfigured' else 'offline-placeholder')
    get_settings.cache_clear()
    import asyncio
    import main
    async def execute(**kwargs):
        with (Path(database).parent / 'executions.jsonl').open('a') as stream:
            stream.write(json.dumps({'sid': kwargs['sid'], 'message': kwargs['message']}) + '\n')
        from core.observability.runtime import span
        with span('context.prepare') as scope:
            if scope:
                scope.capture('synthetic_input', lambda: kwargs['message'])
        if kwargs['message'] == 'late':
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                await asyncio.sleep(.2)
        else:
            await asyncio.sleep(30 if kwargs['message'] == 'slow' else .15)
        if kwargs['message'] == 'error':
            raise RuntimeError('private provider error')
        return {'reply':'isolated result', 'accepted_context':kwargs['context'], 'turn_result':{'status':'success'}}
    main.conversations.execute = execute
    main.conversations.timeout_for = lambda depth: 30 if mode == 'interactive' else 1
    import uvicorn
    uvicorn.run(main.app, host='127.0.0.1', port=int(port), log_level='warning')


def probe():
    import httpx
    with tempfile.TemporaryDirectory(prefix='bgm-lifecycle-') as directory:
        database = str(Path(directory) / 'sessions.sqlite3')
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        process = None
        def start(mode='configured'):
            child = subprocess.Popen([sys.executable, __file__, 'serve', database, str(port), mode], cwd=ROOT,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(100):
                if child.poll() is not None:
                    raise AssertionError('isolated server failed to start')
                try:
                    if httpx.get(f'http://127.0.0.1:{port}/health', timeout=.2, trust_env=False).status_code == 200:
                        return child
                except httpx.TransportError:
                    pass
                time.sleep(.05)
            child.kill()
            child.wait()
            raise AssertionError('isolated startup deadline exceeded')
        try:
            process = start()
            with httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=2, trust_env=False) as client:
                def settle(path, expected):
                    for _ in range(150):
                        turn = client.get(path).json()
                        if turn['status'] not in {'accepted', 'running', 'cancelling'}:
                            assert turn['status'] == expected, turn
                            return turn
                        time.sleep(.02)
                    raise AssertionError(f'turn did not settle: {path}')
                assert client.get('/api/readiness').json()['ready']
                assert client.get('/').headers['cache-control'] == 'no-store'
                assert client.get('/web/chat.js').headers['cache-control'] == 'no-store'
                assert client.get('/api/sessions', headers={'Origin':'https://external.invalid'}).status_code == 403
                assert client.get('/api/sessions', headers={'Host':'external.invalid'}).status_code == 403
                sid = client.post('/api/sessions').json()['id']
                context = {'object_url':'https://bangumi.tv/subject/400602', 'progress':'第3集', 'corrections':'演出，不是剧情'}
                assert client.patch(f'/api/sessions/{sid}/context', json={'version':0,'context':{'object_url':'https://external.invalid/1'}}).status_code == 422
                saved = client.patch(f'/api/sessions/{sid}/context', json={'version':0,'context':context}).json()
                assert saved['context']['object']['entity_id'] == 400602
                assert client.patch(f'/api/sessions/{sid}/context', json={'version':0,'context':context}).status_code == 409
                from core.observability import Runtime
                control = Runtime(Path(directory)/'observability')
                control.diagnostic('anonymous', sid, seconds=60)
                rid = str(uuid4())
                body = json.dumps({'request_id':rid, 'version':1, 'message':'disconnect'}).encode()
                # 在取得响应前断开真实 TCP 连接。
                with socket.create_connection(('127.0.0.1', port)) as connection:
                    request = f'POST /api/sessions/{sid}/turns HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n'.encode() + body
                    connection.sendall(request)
                path = f'/api/sessions/{sid}/turns/{rid}'
                for _ in range(100):
                    response = client.get(path)
                    if response.status_code == 200 and response.json()['status'] == 'completed':
                        break
                    time.sleep(.03)
                else:
                    raise AssertionError('disconnected request did not complete')
                payload = {'request_id':rid, 'version':1, 'message':'disconnect'}
                assert client.post(f'/api/sessions/{sid}/turns', json=payload).json() == response.json()
                assert response.json()['response']['accepted_context'] == saved['context']
                assert client.post(f'/api/sessions/{sid}/turns', json={**payload,'message':'changed'}).status_code == 409
                assert client.post(f'/api/sessions/{sid}/turns', json={**payload,'request_id':str(uuid4()),'message':''}).status_code == 422
                for _ in range(100):
                    trace_result = client.get(f'/api/observability/sessions/{sid}?content=true').json()
                    if trace_result.get('recording_complete'):
                        break
                    time.sleep(.02)
                assert trace_result['recording_complete']
                assert len([r for r in trace_result['records'] if r['name']=='turn.execute']) == 1
                assert any(a.get('value')=='disconnect' for r in trace_result['records'] for a in r.get('artifacts', []))
                sid2 = client.post('/api/sessions').json()['id']
                rid2 = str(uuid4())
                assert client.post(f'/api/sessions/{sid2}/turns', json={'request_id':rid2,'version':0,'message':'slow'}).status_code == 202
                path2 = f'/api/sessions/{sid2}/turns/{rid2}'
                replay = client.post(f'/api/sessions/{sid2}/turns', json={'request_id':rid2,'version':0,'message':'slow'})
                assert replay.status_code == 202
                assert client.post(f'/api/sessions/{sid2}/turns', json={'request_id':str(uuid4()),'version':1,'message':'overlap'}).status_code == 409
                assert client.patch(f'/api/sessions/{sid2}/context', json={'version':1,'context':context}).status_code == 409
                other = client.post('/api/sessions').json()['id']
                other_rid = str(uuid4())
                client.post(f'/api/sessions/{other}/turns', json={'request_id':other_rid,'version':0,'message':'parallel'})
                parallel = settle(f'/api/sessions/{other}/turns/{other_rid}', 'completed')
                assert parallel['response']['accepted_context'] == {}
                assert client.post(path2 + '/cancel').status_code == 200
                for _ in range(100):
                    if client.get(path2).json()['status'] == 'cancelled':
                        break
                    time.sleep(.02)
                else:
                    raise AssertionError('cancel did not settle')
                assert client.post(path2 + '/cancel').json()['status'] == 'cancelled'
                for message, reason in [('slow','timeout'), ('error','execution')]:
                    state = client.get(f'/api/sessions/{sid2}').json()
                    next_id = str(uuid4())
                    client.post(f'/api/sessions/{sid2}/turns', json={'request_id':next_id,'version':state['version'],'message':message})
                    failed = settle(f'/api/sessions/{sid2}/turns/{next_id}', 'failed')
                    assert failed['reason'] == reason
                    assert 'private provider error' not in json.dumps(failed)
                deleted = client.post('/api/sessions').json()['id']
                delete_rid = str(uuid4())
                client.post(f'/api/sessions/{deleted}/turns', json={'request_id':delete_rid,'version':0,'message':'late'})
                time.sleep(.05)
                assert client.delete(f'/api/sessions/{deleted}?version=0').status_code == 409
                assert client.delete(f'/api/sessions/{deleted}?version=1').status_code == 200
                time.sleep(.3)
                assert client.get(f'/api/sessions/{deleted}').status_code == 404
                assert client.get(f'/api/sessions/{deleted}/turns/{delete_rid}').status_code == 404
                sid3 = client.post('/api/sessions').json()['id']
                rid3 = str(uuid4())
                client.post(f'/api/sessions/{sid3}/turns', json={'request_id':rid3,'version':0,'message':'slow'})
                for _ in range(100):
                    if client.get(f'/api/observability/sessions/{sid3}').json().get('records'):
                        break
                    time.sleep(.02)
                process.kill()
                process.wait(timeout=5)
                process = start()
                recovered = client.get(f'/api/sessions/{sid3}/turns/{rid3}').json()
                assert recovered['status'] == 'interrupted'
                assert recovered['reason'] == 'service_restart'
                assert client.get(path).json()['status'] == 'completed'
                restored = client.get(f'/api/sessions/{sid}').json()
                assert restored['context'] == saved['context']
                assert client.post(f'/api/sessions/{sid}/turns', json=payload).json() == response.json()
                executions = [json.loads(line) for line in (Path(directory)/'executions.jsonl').read_text().splitlines()]
                assert sum(e['sid'] == sid for e in executions) == 1
                assert sum(e['sid'] == sid3 for e in executions) == 1
                restarted_trace = client.get(f'/api/observability/sessions/{sid3}').json()
                assert not restarted_trace['recording_complete']
                assert any(r['status']=='interrupted' for r in restarted_trace['records'])
                version = client.get(f'/api/sessions/{sid}').json()['version']
                assert client.delete(f'/api/sessions/{sid}?version={version}').status_code == 200
                assert client.get(f'/api/observability/sessions/{sid}').status_code == 404
                assert control.query('anonymous', sid)['records'] == []
                control.close()
                # 启动失败必须退出，不能把原有健康实例当作新实例启动成功。
                collision = subprocess.run([sys.executable, __file__, 'serve', str(Path(directory)/'collision.sqlite3'), str(port)],
                                           cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                assert collision.returncode != 0
                assert client.get('/health').status_code == 200
                invalid_database = Path(directory)/'directory-not-database'
                invalid_database.mkdir()
                bad_storage = subprocess.run([sys.executable, __file__, 'serve', str(invalid_database), str(port)],
                                             cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                assert bad_storage.returncode != 0
                graceful_id = str(uuid4())
                other_version = client.get(f'/api/sessions/{other}').json()['version']
                client.post(f'/api/sessions/{other}/turns', json={'request_id':graceful_id,'version':other_version,'message':'slow'})
                graceful_path = f'/api/sessions/{other}/turns/{graceful_id}'
                for _ in range(100):
                    if client.get(graceful_path).json()['status'] == 'running':
                        break
                    time.sleep(.01)
                else:
                    raise AssertionError('graceful shutdown probe never started')
                process.terminate()
                process.wait(timeout=5)
                process = start('unconfigured')
                graceful = client.get(graceful_path).json()
                assert graceful['status'] == 'interrupted' and graceful['reason'] == 'service_shutdown'
                readiness = client.get('/api/readiness').json()
                assert not readiness['ready'] and 'LLM_API_KEY' in readiness['missing']
                assert 'offline-placeholder' not in json.dumps(readiness)
                assert client.get(f'/api/sessions/{sid2}').status_code == 200
                version = client.get(f'/api/sessions/{sid2}').json()['version']
                assert client.delete(f'/api/sessions/{sid2}?version={version}').status_code == 200
            print('PASS: startup/assets, port/storage startup failures, missing configuration with history access, input/origin/host/version guards, context transport/isolation/restart, disconnect, replay/conflict, parallel sessions, busy protection, cancellation, timeout/error, late deletion, graceful/hard restart without replay and trace deletion')
        finally:
            if process and process.poll() is None:
                process.terminate()
                process.wait(timeout=5)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'serve':
        serve(*sys.argv[2:])
    else:
        probe()
