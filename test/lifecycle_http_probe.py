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


def serve(database, port):
    from core.config import Settings, get_settings
    Settings.model_config['env_file'] = None
    os.environ.update(AUTH_ENABLED='false', ENVIRONMENT='test', MEMORY_ENABLED='false', RAG_ENABLED='false',
                      SESSION_DB_PATH=database, TRACE_PATH=str(Path(database).parent / 'observability'), RATE_LIMIT_PER_MINUTE='0', DEV_MODE='false', LLM_API_KEY='offline-placeholder')
    get_settings.cache_clear()
    import asyncio
    import main
    async def execute(**kwargs):
        from core.observability.runtime import span
        with span('context.prepare') as scope:
            if scope:
                scope.capture('synthetic_input', lambda: kwargs['message'])
        await asyncio.sleep(30 if kwargs['message'] == 'slow' else .15)
        return {'reply':'isolated result', 'turn_result':{'status':'success'}}
    main.conversations.execute = execute
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
        def start():
            child = subprocess.Popen([sys.executable, __file__, 'serve', database, str(port)], cwd=ROOT,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(100):
                if child.poll() is not None:
                    raise AssertionError('isolated server failed to start')
                try:
                    if httpx.get(f'http://127.0.0.1:{port}/health', timeout=.2).status_code == 200:
                        return child
                except httpx.TransportError:
                    pass
                time.sleep(.05)
            child.kill()
            child.wait()
            raise AssertionError('isolated startup deadline exceeded')
        try:
            process = start()
            with httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=2) as client:
                sid = client.post('/api/sessions').json()['id']
                from core.observability import Runtime
                control = Runtime(Path(directory)/'observability')
                control.diagnostic('anonymous', sid, seconds=60)
                rid = str(uuid4())
                body = json.dumps({'request_id':rid, 'version':0, 'message':'disconnect'}).encode()
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
                payload = {'request_id':rid, 'version':0, 'message':'disconnect'}
                assert client.post(f'/api/sessions/{sid}/turns', json=payload).json() == response.json()
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
                assert client.post(path2 + '/cancel').status_code == 200
                for _ in range(100):
                    if client.get(path2).json()['status'] == 'cancelled':
                        break
                    time.sleep(.02)
                else:
                    raise AssertionError('cancel did not settle')
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
                restarted_trace = client.get(f'/api/observability/sessions/{sid3}').json()
                assert not restarted_trace['recording_complete']
                assert any(r['status']=='interrupted' for r in restarted_trace['records'])
                version = client.get(f'/api/sessions/{sid}').json()['version']
                assert client.delete(f'/api/sessions/{sid}?version={version}').status_code == 200
                assert client.get(f'/api/observability/sessions/{sid}').status_code == 404
                assert control.query('anonymous', sid)['records'] == []
                control.close()
            print('PASS: real disconnect, result retrieval, replay, cancellation, hard process restart, owned trace query, scoped snapshots and deletion')
        finally:
            if process and process.poll() is None:
                process.terminate()
                process.wait(timeout=5)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'serve':
        serve(*sys.argv[2:])
    else:
        probe()
