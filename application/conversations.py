"""整轮会话执行；持久事实与模型窗口分开，不依赖 HTTP 类型。"""
import asyncio

from sessions.store import SessionError, now


class Conversations:
    def __init__(self, store, execute):
        self.store = store
        self.execute = execute

    async def send(self, owner, sid, rid, version, message, depth, style):
        started, state = self.store.begin(owner, sid, rid, version, message, [depth, style])
        if not started:
            return state
        session = self.store.read(owner, sid)
        history = []
        for turn in session['turns']:
            response = turn['response']
            if turn['status'] == 'completed' and response and response.get('turn_result', {}).get('status') != 'failed':
                history.extend([{'role': 'user', 'content': turn['message']}, {'role': 'assistant', 'content': response['reply']}])
        try:
            response = await self.execute(owner=owner, sid=sid, message=message, depth=depth,
                                          style=style, history=history, context=session['context'])
        except asyncio.CancelledError:
            try:
                self.store.complete(owner, sid, rid, state, None, 'interrupted')
            except SessionError:
                pass
            raise
        except Exception:
            response = {'reply': '本轮执行失败，请稍后重试。', 'session_id': sid,
                        'turn_result': {'status': 'failed', 'missing': ['execution'], 'sources': []}}
        response.pop("telemetry", None)
        for source in response.get("turn_result", {}).get("sources", []):
            source.setdefault("recorded_at", now())
        # 只有事务提交成功后才返回结果；删除或版本变化会拒绝提交。
        status = 'failed' if response.get('turn_result', {}).get('status') == 'failed' else 'completed'
        return self.store.complete(owner, sid, rid, state, response, status)
