"""C1–C5 单次真实验收与只读调用跟踪。会产生模型费用与少量 Bangumi 只读请求。

serve 在独立进程中安装观察钩子，不改应用策略；run 经真实持久会话 HTTP 连续提交原题。
完整输入/输出含公开社区文本及系统提示，必须保存到 Git 排除的本机目录，不能输出配置秘密。
"""
from __future__ import annotations

import argparse
import asyncio
from contextvars import ContextVar
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
CASE = ContextVar('acceptance_case', default=None)
DEPTH = ContextVar('acceptance_tool_depth', default=0)


def questions():
    text = (ROOT / 'docs/product/fixed-agent-scenarios.md').read_text()
    return dict(re.findall(r'^### (C[1-5])[^\n]*\n\n> ([^\n]+)', text, re.M))


def serve(output, port):
    os.environ.update(MEMORY_ENABLED='false', RAG_ENABLED='false', DEV_MODE='false',
                      AUTH_ENABLED='false', RATE_LIMIT_PER_MINUTE='0')
    from core.config import get_settings
    settings = get_settings()
    secrets = [settings.LLM_API_KEY, settings.ZHIPU_API_KEY, settings.BANGUMI_ACCESS_TOKEN,
               settings.BANGUMI_APP_SECRET, *settings.API_USER_TOKENS.values()]
    secrets.extend(os.environ.get(key, '') for key in ['OPENAI_API_KEY', 'AZURE_OPENAI_API_KEY'])
    def clean(value):
        if isinstance(value, str):
            for secret in secrets:
                if secret:
                    value = value.replace(secret, '<REDACTED>')
            return value
        if isinstance(value, dict):
            return {str(key): '<REDACTED>' if str(key).lower() in {'api_key','authorization','access_token','client_secret'} else clean(item) for key,item in value.items()}
        if isinstance(value, (list,tuple)):
            return [clean(item) for item in value]
        return value
    def pack(value):
        if hasattr(value, 'content') and hasattr(value, 'type'):
            return {key:pack(getattr(value,key,None)) for key in ['type','content','name','tool_calls','tool_call_id'] if getattr(value,key,None) is not None}
        if isinstance(value, dict):
            return {str(key):pack(item) for key,item in value.items()}
        if isinstance(value,(list,tuple)):
            return [pack(item) for item in value]
        if value is None or isinstance(value,(str,int,float,bool)):
            return value
        return str(type(value).__name__)
    count = 0
    def record(kind, **data):
        nonlocal count
        case = CASE.get()
        if not case:
            return
        count += 1
        event = clean({'event':count,'case':case,'kind':kind,'at':datetime.now(timezone.utc).isoformat(),**pack(data)})
        directory = output / case
        directory.mkdir(exist_ok=True, mode=0o700)
        with (directory/'events.jsonl').open('a',encoding='utf-8') as handle:
            handle.write(json.dumps(event,ensure_ascii=False)+'\n')
    # 工厂仅记录对象与原有标签的对应关系，返回的仍是原对象。
    import agent.llm as factory
    original_factory = factory.create_llm
    labels = {}
    def observed_factory(*args, **kwargs):
        model = original_factory(*args, **kwargs)
        labels[id(model)] = kwargs.get('_telemetry_label') or 'classifier'
        return model
    factory.create_llm = observed_factory
    from langchain_core.language_models.chat_models import BaseChatModel
    original_llm = BaseChatModel.ainvoke
    async def observed_llm(self, input, *args, **kwargs):
        stage = labels.get(id(self), 'unknown')
        call = str(uuid4())
        prepared = pack(input)
        record('model_input',call=call,stage=stage,messages=prepared,
               tools=kwargs.get('tools',[]),tool_choice=kwargs.get('tool_choice'),
               input_sha256=sha256(json.dumps(prepared,ensure_ascii=False,sort_keys=True).encode()).hexdigest())
        started=time.monotonic()
        try:
            result = await original_llm(self,input,*args,**kwargs)
        except BaseException as error:
            record('model_error',call=call,stage=stage,error_type=type(error).__name__,elapsed_ms=round((time.monotonic()-started)*1000))
            raise
        record('model_output',call=call,stage=stage,message=result,
               usage=getattr(result,'usage_metadata',None),
               finish_reason=getattr(result,'response_metadata',{}).get('finish_reason'),
               elapsed_ms=round((time.monotonic()-started)*1000))
        return result
    BaseChatModel.ainvoke = observed_llm
    from langchain_core.tools import BaseTool
    original_tool = BaseTool.ainvoke
    async def observed_tool(self, input, *args, **kwargs):
        depth=DEPTH.get(); token=DEPTH.set(depth+1); call=str(uuid4())
        record('tool_input',call=call,tool=self.name,depth=depth,input=input)
        started=time.monotonic()
        try:
            result=await original_tool(self,input,*args,**kwargs)
            record('tool_output',call=call,tool=self.name,depth=depth,output=result,elapsed_ms=round((time.monotonic()-started)*1000))
            return result
        except BaseException as error:
            record('tool_error',call=call,tool=self.name,depth=depth,error_type=type(error).__name__)
            raise
        finally:
            DEPTH.reset(token)
    BaseTool.ainvoke = observed_tool
    from langgraph.graph.state import CompiledStateGraph
    original_graph = CompiledStateGraph.ainvoke
    async def observed_graph(self, input, *args, **kwargs):
        call=str(uuid4()); record('graph_input',call=call,state=input)
        try:
            result=await original_graph(self,input,*args,**kwargs)
            record('graph_output',call=call,state=result)
            return result
        except BaseException as error:
            record('graph_error',call=call,error_type=type(error).__name__)
            raise
    CompiledStateGraph.ainvoke=observed_graph
    import main
    import logging
    logging.getLogger('bgm-agent').handlers.clear()
    logging.getLogger('bgm-agent').addHandler(logging.NullHandler())
    original_execute=main.conversations.execute
    fixed=questions()
    async def observed_execute(**kwargs):
        cid=next((case for case,text in fixed.items() if text==kwargs['message']),None)
        token=CASE.set(cid)
        record('application_input',**kwargs)
        try:
            result=await original_execute(**kwargs)
            record('application_output',result=result)
            return result
        finally:
            CASE.reset(token)
    main.conversations.execute=observed_execute
    manifest={'started_at':datetime.now(timezone.utc).isoformat(),'model':settings.LLM_MODEL,
              'depth':'fast','output_style':'bangumi','dev_mode':False,'rag':False,'l2':False,
              'hooks':['BaseChatModel.ainvoke','BaseTool.ainvoke','CompiledStateGraph.ainvoke','model factory identity','application execute'],
              'scope':'Only C1-C5 exact inputs; delegates original arguments and outputs; no hidden reasoning or HTTP headers captured',
              'questions':fixed}
    (output/'manifest.json').write_text(json.dumps(clean(manifest),ensure_ascii=False,indent=2))
    import uvicorn
    uvicorn.run(main.app,host='127.0.0.1',port=port,log_level='warning',access_log=False)


def run(output, port):
    if any((output/case/'turn.json').exists() for case in questions()):
        raise RuntimeError('Existing results found; refusing to repeat or overwrite this run')
    import httpx
    with httpx.Client(base_url=f'http://127.0.0.1:{port}',timeout=10) as client:
        ready=client.get('/api/readiness');ready.raise_for_status()
        if not ready.json()['ready']:
            raise RuntimeError('Model configuration is not ready')
        session=client.post('/api/sessions');session.raise_for_status();sid=session.json()['id']
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
        saved=client.get(f'/api/sessions/{sid}');saved.raise_for_status()
        (output/'session.json').write_text(json.dumps(saved.json(),ensure_ascii=False,indent=2))
        print('C1-C5 complete; full local trace captured; no reruns',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['serve','run'])
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--port',type=int,default=8000)
    args=parser.parse_args()
    args.output=args.output.resolve()
    allowed=(ROOT/'.local-data').resolve()
    if allowed not in args.output.parents:
        parser.error('trace output must be below the Git-excluded .local-data directory')
    args.output.mkdir(parents=True,exist_ok=True,mode=0o700)
    if args.mode == 'serve' and (args.output/'manifest.json').exists():
        parser.error('Existing manifest found; use a new run directory')
    (serve if args.mode=='serve' else run)(args.output,args.port)
