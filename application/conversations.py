"""单进程轮次运行时：HTTP 只持有观察连接，应用持有执行任务。"""
import asyncio
import logging
import sqlite3
import contextlib
from core.observability import span, event
from dataclasses import dataclass
from datetime import datetime

from sessions.lifecycle import TERMINAL
from sessions.store import SessionError, now

logger = logging.getLogger('bgm-agent.lifecycle')


@dataclass
class Run:
    owner: str
    sid: str
    rid: str
    version: int
    task: asyncio.Task | None = None
    worker: asyncio.Task | None = None
    stop: str | None = None
    outcome: tuple | None = None
    storage_error: bool = False
    observation: object = None
    accepted_at: str | None = None
    committed_at: str | None = None


class Conversations:
    def __init__(self, store, execute, *, timeout_for=None, max_active=32, observer=None):
        self.observer = observer
        self.store = store
        self.execute = execute
        self.timeout_for = timeout_for or (lambda depth: 120 if depth == 'deep' else 45)
        self.max_active = max_active
        self.runs = {}
        self.closing = False

    def start(self):
        if any((run.task and not run.task.done()) or (run.worker and not run.worker.done()) for run in self.runs.values()):
            raise RuntimeError('Previous lifecycle still owns live executions')
        # 同进程重新进入生命周期也视为重启；未提交结果由持久状态标为中断。
        self.runs.clear()
        self.closing = False

    def submit(self, owner, sid, rid, version, message, depth, style):
        # 先校验归属和已登记请求，重放不受并发容量限制，也不重新执行。
        session = self.store.read(owner, sid)
        if any(turn['request_id'] == rid for turn in session['turns']):
            self.store.begin(owner, sid, rid, version, message, [depth, style])
            return self.inspect(owner, sid, rid)
        if self.closing:
            raise SessionError('shutting_down')
        if len(self.runs) >= self.max_active:
            raise SessionError('capacity')
        if any(run.sid == sid and run.owner == owner for run in self.runs.values()):
            raise SessionError('busy')
        _, reserved = self.store.begin(owner, sid, rid, version, message, [depth, style])
        run = Run(owner, sid, rid, reserved)
        self.runs[(owner, sid, rid)] = run
        run.task = asyncio.create_task(self._run(run, session, message, depth, style))
        logger.info('turn accepted request_id=%s', rid)
        run.task.add_done_callback(lambda task: self._finished(run, task))
        accepted = self.store.turn(owner, sid, rid)
        run.accepted_at = accepted['created_at']
        return accepted

    async def send(self, owner, sid, rid, version, message, depth, style):
        """兼容等待者；取消等待不取消应用持有的执行。"""
        existing = (owner, sid, rid) in self.runs
        turn = self.submit(owner, sid, rid, version, message, depth, style)
        if existing:
            return turn
        run = self.runs.get((owner, sid, rid))
        if run and run.task and not run.task.done():
            await asyncio.shield(run.task)
        return self.inspect(owner, sid, rid)

    def inspect(self, owner, sid, rid):
        turn = self.store.turn(owner, sid, rid)
        run = self.runs.get((owner, sid, rid))
        if run and run.storage_error:
            # 只重试 SQLite 提交；绝不重放模型或工具。
            self._commit(run)
            turn = self.store.turn(owner, sid, rid)
        return turn

    def cancel(self, owner, sid, rid):
        turn = self.inspect(owner, sid, rid)
        if turn['status'] in TERMINAL:
            return turn
        run = self.runs.get((owner, sid, rid))
        if run is None:
            raise SessionError('execution_unavailable')
        self.store.transition(owner, sid, rid, 'cancelling')
        if run.observation:
            run.observation.event('lifecycle.cancelling', status='cancelling')
        run.stop = 'cancelled'
        logger.info('turn cancellation requested request_id=%s', rid)
        if run.worker:
            run.worker.cancel()
        return self.store.turn(owner, sid, rid)

    def deleted(self, owner, sid):
        if self.observer:
            try:
                self.observer.delete(owner, sid)
            except Exception:
                self.observer.failed()
        for run in list(self.runs.values()):
            if (run.owner, run.sid) == (owner, sid):
                run.stop = 'deleted'
                if run.worker:
                    run.worker.cancel()
                if run.task.done():
                    self.runs.pop((owner, sid, run.rid), None)

    async def _run(self, run, session, message, depth, style):
        manager = self.observer.turn(run.owner, run.sid, run.rid, persistent=True) if self.observer else contextlib.nullcontext()
        with manager as observation:
            run.observation = observation
            if observation:
                observation.defer()
                observation.annotate(accepted_at=run.accepted_at)
                observation.event('lifecycle.accepted', status='accepted', recorded_at=run.accepted_at)
            await self._run_body(run, session, message, depth, style)

    async def _run_body(self, run, session, message, depth, style):
        response = None
        status, reason = 'interrupted', 'service_shutdown'
        try:
            if run.stop:
                status, reason = ('cancelled', 'user_cancelled') if run.stop == 'cancelled' else ('interrupted', run.stop)
            else:
                self.store.transition(run.owner, run.sid, run.rid, 'running')
                event('lifecycle.running', status='running')
                history = []
                history_references = []
                for turn in session['turns']:
                    saved = turn['response']
                    if turn['status'] == 'completed' and saved and saved.get('turn_result', {}).get('status') != 'failed':
                        history.extend([{'role': 'user', 'content': turn['message']}, {'role': 'assistant', 'content': saved['reply']}])
                        history_references.extend([{'request_id': turn['request_id'], 'role': role} for role in ('user', 'assistant')])
                event('context.assembled', history_candidates=len(session['turns']), history_selected=len(history), references=history_references, background_version=session.get('context_source') if session['context'] else 'none')
                run.worker = asyncio.create_task(self.execute(owner=run.owner, sid=run.sid, message=message, depth=depth,
                                                              style=style, history=history, context=session['context']))
                done, _ = await asyncio.wait({run.worker}, timeout=self.timeout_for(depth))
                if not done:
                    if not run.stop:
                        run.worker.cancel()
                    status, reason = 'failed', 'timeout'
                else:
                    response = run.worker.result()
                    status = 'failed' if response.get('turn_result', {}).get('status') == 'failed' else 'completed'
                    reason = ('timeout' if 'timeout' in response.get('turn_result', {}).get('missing', []) else 'execution') if status == 'failed' else None
        except asyncio.CancelledError:
            status, reason = ('cancelled', 'user_cancelled') if run.stop == 'cancelled' else ('interrupted', 'service_shutdown')
        except SessionError as error:
            if error.code == 'not_found':
                return
            status, reason = 'failed', 'state_conflict'
        except sqlite3.Error:
            status, reason = 'failed', 'storage'
        except Exception:
            status, reason = 'failed', 'execution'
        if run.stop == 'deleted':
            return
        if run.stop in {'cancelled', 'interrupted'}:
            response = None
            if run.worker and not run.worker.done():
                status, reason = 'failed', 'cancellation_timeout'
            else:
                status, reason = (run.stop, 'user_cancelled' if run.stop == 'cancelled' else 'service_shutdown')
        if response:
            response.pop('telemetry', None)
            for source in response.get('turn_result', {}).get('sources', []):
                source.setdefault('recorded_at', now())
        run.outcome = (response, status, reason)
        try:
            self._commit(run)
        except sqlite3.Error:
            logger.warning('turn persistence unavailable request_id=%s', run.rid)
        except SessionError as error:
            if error.code != 'not_found':
                logger.warning('turn commit rejected request_id=%s code=%s', run.rid, error.code)

    def _commit(self, run):
        manager = run.observation.activate() if run.observation else contextlib.nullcontext()
        with manager:
            with span('session.commit') as scope:
                if scope:
                    scope.event('commit.attempt', status=run.outcome[1], reason=run.outcome[2])
                self._commit_body(run)
                if scope:
                    scope.capture('committed_result', lambda: {'reply': (run.outcome[0] or {}).get('reply'), 'turn_result': (run.outcome[0] or {}).get('turn_result'), 'status': run.outcome[1], 'reason': run.outcome[2]})
                event('commit.confirmed', status=run.outcome[1], reason=run.outcome[2], recorded_at=run.committed_at)

    def _commit_body(self, run):
        response, status, reason = run.outcome
        try:
            persisted = self.store.turn(run.owner, run.sid, run.rid)
            # 提交可能已成功，仅后续读回/确认失败；先核对事实来源，不能再次执行或覆盖终态。
            if persisted['status'] not in TERMINAL:
                persisted = self.store.complete(run.owner, run.sid, run.rid, run.version, response, status, reason)
            if run.observation:
                run.committed_at = persisted.get('updated_at') if isinstance(persisted, dict) else None
                run.observation.annotate(committed_at=run.committed_at)
                run.observation.annotate(commit_outcome=status)
                try:
                    run.observation.annotate(accepted_to_commit_ms=(datetime.fromisoformat(run.committed_at)-datetime.fromisoformat(run.accepted_at)).total_seconds()*1000)
                except (ValueError, TypeError):
                    run.observation.annotate(accepted_to_commit_ms=None)
        except sqlite3.Error:
            run.storage_error = True
            raise
        run.storage_error = False
        logger.info('turn settled request_id=%s status=%s reason=%s', run.rid, status, reason)
        if run.task.done():
            self._finished(run, run.task)

    def _finished(self, run, task):
        if not task.cancelled():
            task.exception()  # 消费异常，避免后台任务产生无归属的异常警告。
        if run.worker and not run.worker.done():
            run.worker.add_done_callback(lambda worker: self._finished(run, task))
            return
        if run.worker and not run.worker.cancelled():
            run.worker.exception()
        if run.observation and not run.observation.is_finished:
            run.observation.event('execution.quiesced', status=run.outcome[1] if run.outcome else 'deleted')
            run.observation.finish()
        if not run.storage_error:
            self.runs.pop((run.owner, run.sid, run.rid), None)

    async def close(self):
        self.closing = True
        for run in list(self.runs.values()):
            if not run.task.done():
                run.stop = run.stop or 'interrupted'
                if run.worker:
                    run.worker.cancel()
        tasks = {run.task for run in self.runs.values() if not run.task.done()}
        if tasks:
            await asyncio.wait(tasks, timeout=2)
