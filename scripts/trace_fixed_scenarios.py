"""C1–C5 单次真实验收与只读调用跟踪。会产生模型费用与少量 Bangumi 只读请求。

serve 使用正式观测链路及隔离会话库；run 通过限时会话 scope 采集，必须显式 --allow-paid。
完整输入/输出含公开社区文本及系统提示，必须保存到 Git 排除的本机目录，不能输出配置秘密。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def questions():
    text = (ROOT / 'docs/product/fixed-agent-scenarios.md').read_text()
    return dict(re.findall(r'^### (C[1-5])[^\n]*\n\n> ([^\n]+)', text, re.M))


def serve(output, port):
    os.environ.update(MEMORY_ENABLED='false', RAG_ENABLED='false', DEV_MODE='false',
                      AUTH_ENABLED='false', RATE_LIMIT_PER_MINUTE='0', TRACE_LEVEL='metadata',
                      SESSION_DB_PATH=str(output/'sessions.sqlite3'), TRACE_PATH=str(output/'observability'))
    import main
    manifest = {'started_at': datetime.now(timezone.utc).isoformat(),
                'collector': 'core.observability schema v1', 'questions': questions(),
                'scope': 'Explicit local session scope, limited to 600 seconds; no SDK patches'}
    with open(output/'manifest.json', 'x', encoding='utf-8', opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
    import uvicorn
    uvicorn.run(main.app, host='127.0.0.1', port=port, log_level='warning', access_log=False)


def run(output, port):
    if any((output/case/'turn.json').exists() for case in questions()):
        raise RuntimeError('Existing results found; refusing to repeat or overwrite this run')
    import httpx
    with httpx.Client(base_url=f'http://127.0.0.1:{port}',timeout=10) as client:
        ready=client.get('/api/readiness');ready.raise_for_status()
        if not ready.json()['ready']:
            raise RuntimeError('Model configuration is not ready')
        health=client.get('/api/observability/health');health.raise_for_status()
        if not health.json().get('writer_running'):
            raise RuntimeError('Formal trace writer is unavailable; no model requests made')
        session=client.post('/api/sessions');session.raise_for_status();sid=session.json()['id']
        from core.observability import Runtime
        from core.observability.maintenance import export
        observer = Runtime(output/'observability')
        observer.diagnostic('anonymous', sid, seconds=600)
        for case,message in questions().items():
            state=client.get(f'/api/sessions/{sid}');state.raise_for_status()
            rid=str(uuid4()); started=time.monotonic()
            payload={'request_id':rid,'version':state.json()['version'],'message':message,'depth':'fast','output_style':'bangumi'}
            accepted=client.post(f'/api/sessions/{sid}/turns',json=payload);accepted.raise_for_status()
            for _ in range(180):
                response=client.get(f'/api/sessions/{sid}/turns/{rid}');response.raise_for_status();turn=response.json()
                if turn['status'] not in {'accepted','running','cancelling'}:
                    break
                time.sleep(.5)
            else:
                raise RuntimeError(f'{case} did not settle within observation deadline; no replay attempted')
            directory=output/case;directory.mkdir(exist_ok=True,mode=0o700)
            (directory/'turn.json').write_text(json.dumps({'case':case,'input':message,'accept_http':accepted.status_code,
                'elapsed_ms':round((time.monotonic()-started)*1000),'turn':turn},ensure_ascii=False,indent=2))
            print(f'{case}: {turn["status"]}; {round(time.monotonic()-started,2)}s',flush=True)
        for _ in range(50):
            observed = client.get(f'/api/observability/sessions/{sid}')
            observed.raise_for_status()
            if observed.json().get('recording_complete'):
                break
            time.sleep(.1)
        with open(output/'trace.json', 'x', encoding='utf-8', opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
            json.dump(export(observer, 'anonymous', sid, content=True), stream, ensure_ascii=False, indent=2)
        observer.close()
        saved=client.get(f'/api/sessions/{sid}');saved.raise_for_status()
        (output/'session.json').write_text(json.dumps(saved.json(),ensure_ascii=False,indent=2))
        print('C1-C5 complete; full local trace captured; no reruns',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['serve','run'])
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--allow-paid', action='store_true', help='Explicitly authorize the real scenario requests')
    parser.add_argument('--port',type=int,default=8000)
    args=parser.parse_args()
    if args.mode == 'run' and not args.allow_paid:
        parser.error('run makes paid model calls; explicit --allow-paid is required')
    args.output=args.output.resolve()
    allowed=(ROOT/'.local-data').resolve()
    if allowed not in args.output.parents:
        parser.error('trace output must be below the Git-excluded .local-data directory')
    args.output.mkdir(parents=True,exist_ok=True,mode=0o700)
    if args.mode == 'serve' and (args.output/'manifest.json').exists():
        parser.error('Existing manifest found; use a new run directory')
    (serve if args.mode=='serve' else run)(args.output,args.port)
