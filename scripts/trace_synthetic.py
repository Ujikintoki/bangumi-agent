"""Generate a diagnostic example through the formal collector, using only fixed doubles.

No .env, network, model provider, PostgreSQL or frozen acceptance data is accessed.
The execution is an explicitly synthetic seam exercise, not a product-quality verdict.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def generate(output):
    from core.config import Settings
    Settings.model_config['env_file'] = None
    from core.observability import Runtime, span
    from core.observability.runtime import event, capture, invoke
    from core.observability.maintenance import export
    from application.conversations import Conversations
    from sessions.store import SessionStore
    from agent.observability import ModelObserver, TOOL_OBSERVER, observe_tool_call, messages_value
    from agent.memory.short_term import manage_memory
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
    from langchain_core.tools import tool
    from langgraph.graph import StateGraph, START, END
    from agent.state import AgentState
    with tempfile.TemporaryDirectory(prefix='bgm-trace-example-') as directory:
        root = Path(directory)
        runtime = Runtime(root/'trace', secrets=['synthetic-known-secret'])
        runtime.start()
        store = SessionStore(root/'sessions.sqlite3')
        store.initialize()
        sid = store.create('demo')['id']
        runtime.diagnostic('demo',sid,60)
        @tool
        async def opinions(subject_id: int) -> dict:
            """Return two explicitly synthetic comments for the trace example."""
            return {'subject_id':subject_id, 'comments':{'items':[
                {'id':11,'author':'合成作者甲','content':'演出很散，剧情不是我批评的重点。','url':'https://example.invalid/comment/11','truncated':False},
                {'id':12,'author':'合成作者乙','content':'我喜欢剧情。','url':'https://example.invalid/comment/12','truncated':True}]},
                'api_key':'synthetic-known-secret'}
        opinions.callbacks = [TOOL_OBSERVER]
        graph = StateGraph(AgentState)
        from langgraph.prebuilt import ToolNode
        graph.add_node('tools', ToolNode([opinions], awrap_tool_call=observe_tool_call))
        graph.add_edge(START,'tools')
        graph.add_edge('tools',END)
        tools = graph.compile()
        def model(stage, message):
            message = message.model_copy(update={'usage_metadata': {'input_tokens': 20, 'output_tokens': 4, 'total_tokens': 24}})
            fixture = FakeMessagesListChatModel(responses=[message], callbacks=[ModelObserver(stage)])
            if stage.startswith('reasoning'):
                from langchain_core.utils.function_calling import convert_to_openai_tool
                return fixture.bind(tools=[convert_to_openai_tool(opinions)])
            return fixture
        async def execute(**kwargs):
            messages = [SystemMessage(content='合成系统指令：引用不代表用户立场。'),
                        HumanMessage(content='old '*700), AIMessage(content='旧助手误解了剧情。'),
                        HumanMessage(content=kwargs['message'])]
            with span('context.assemble') as scope:
                scope.capture('assembled_messages', lambda: messages_value(messages))
            with span('graph.execute'):
                with span('node.classify'):
                    await invoke(model('classify',AIMessage(content='discuss')).ainvoke, [messages[-1]])
                    event('classification.result', selected={'raw_intent':'discuss','confidence':1}, status='discuss', schema_version='synthetic-v1')
                event('route.decide', status='synthetic_reasoning', reason='fixed_fixture')
                with span('node.reasoning'):
                    call = AIMessage(content='',tool_calls=[{'name':'opinions','args':{'subject_id':100},'id':'opinion-call'}])
                    await invoke(model('reasoning#1',call).ainvoke, manage_memory(messages,max_tokens=600))
                messages.append(call)
                result = await tools.ainvoke({'messages':messages})
                messages = result['messages']
                with span('node.reasoning'):
                    selected = manage_memory(messages,max_tokens=600)
                    response = await invoke(model('reasoning#2',AIMessage(content='仅选合成评论11，保留演出与剧情的区别。')).ainvoke,selected)
                    messages.append(response)
            with span('response.render') as scope:
                scope.capture('graph_output',lambda: messages_value(messages))
                scope.event('render.materials', selected=['comment:11'], omitted=['comment:12'], reason='fixed_fixture_choice')
                prepared = [SystemMessage(content='合成 Render：只表达用户立场，不攻击作者。'),
                            HumanMessage(content='评论11：演出很散；用户纠正：不是剧情。引用不等于用户观点。')]
                rendered = await invoke(model('render', AIMessage(content='我不满意的是演出和表达，不是剧情。')).ainvoke,prepared)
                scope.capture('render_final',lambda: rendered.content)
            return {'reply':rendered.content,'turn_result':{'status':'success','sources':[{'call_id':'opinion-call','comment_id':11}]}}
        service = Conversations(store,execute,observer=runtime)
        await service.send('demo',sid,'synthetic-request',0,'C2/C3：选一条原评。C4：不是剧情，是演出。C5：不要把引用当成我的观点。','fast',None)
        await service.close()
        runtime.close()
        value = export(runtime,'demo',sid,content=True)
        # Temporary artifact paths are replaced by their immutable opaque reference in a frozen export.
        for record in value['records']:
            for artifact in record.get('artifacts',[]):
                artifact.pop('path',None)
        value['example_kind'] = 'synthetic_collector_and_lifecycle_evidence_only'
        output.parent.mkdir(parents=True,exist_ok=True)
        with open(output,'x',encoding='utf-8',opener=lambda path,flags:os.open(path,flags,0o600)) as stream:
            json.dump(value,stream,ensure_ascii=False,indent=2)
        assert value['recording_complete']
        print('Synthetic diagnostic exported; no paid calls.')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    asyncio.run(generate(args.output))
