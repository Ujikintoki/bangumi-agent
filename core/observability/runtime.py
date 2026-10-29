"""Explicitly owned OTel provider and bounded, fail-open local writer."""
import asyncio
import contextlib
import contextvars
import json
import os
from pathlib import Path
import queue
import re
import sqlite3
import threading
import time
import uuid

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry import trace

current = contextvars.ContextVar('observation', default=None)
_SECRET = re.compile(r'(?i)(bearer\s+\S+|sk-[a-z0-9_-]+)')
_FORBIDDEN = re.compile(r'(?i)(secret|password|authorization|api.?key|token$|reasoning_content|hidden)')


def clean(value):
    if value is None or type(value) in (bool, int, float):
        return value
    if type(value) is str:
        return _SECRET.sub('[REDACTED]', value)
    if type(value) in (list, tuple):
        return [clean(v) for v in value]
    if type(value) is dict:
        return {k: clean(v) for k, v in value.items() if type(k) is str and not _FORBIDDEN.search(k)}
    raise ValueError('unsupported observation value')


class Runtime:
    def __init__(self, path, *, level='metadata', capacity=2048, byte_limit=16*1024*1024):
        if level not in {'off', 'metadata'}:
            raise ValueError('diagnostics require an explicit session scope')
        self.path = Path(path)
        self.level = level
        self.queue = queue.Queue(capacity)
        self.byte_limit = byte_limit
        self.pending_bytes = 0
        self.lock = threading.RLock()
        self.dropped = 0
        self.deleted = set()
        self.scopes = {}
        self.provider = TracerProvider()
        self.tracer = self.provider.get_tracer('bgm-agent', '1')
        self.thread = None
        self.stopping = threading.Event()
        self.instance = uuid.uuid4().hex

    def start(self):
        if self.level == 'off' or self.thread:
            return
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path, 0o700)
        with self.connect() as db:
            version = db.execute('pragma user_version').fetchone()[0]
            if version not in (0, 1):
                raise ValueError('unsupported trace schema')
            db.execute('create table if not exists records (id text primary key, owner text, session text, trace text, body text)')
            db.execute('pragma user_version=1')
            # Previous process cannot finish these spans; never infer business status.
            for rid, body in db.execute('select id, body from records').fetchall():
                record = json.loads(body)
                if record.get('status') == 'running':
                    record['status'] = 'interrupted'
                    record['recording_complete'] = False
                    db.execute('update records set body=? where id=?', (json.dumps(record), rid))
        os.chmod(self.path / 'traces.sqlite3', 0o600)
        self.thread = threading.Thread(target=self._write, daemon=True)
        self.thread.start()

    def connect(self):
        return sqlite3.connect(self.path / 'traces.sqlite3', timeout=.1)

    def emit(self, record):
        try:
            raw = json.dumps(clean(record), ensure_ascii=False)
            size = len(raw.encode())
            with self.lock:
                if (record['owner'], record['session']) in self.deleted:
                    return
                if not self.thread or self.stopping.is_set() or self.pending_bytes + size > self.byte_limit:
                    self.dropped += 1
                    return
                self.queue.put_nowait((record, raw, size))
                self.pending_bytes += size
        except Exception:
            self.dropped += 1

    def _write(self):
        while not self.stopping.is_set() or not self.queue.empty():
            try:
                record, raw, size = self.queue.get(timeout=.02)
            except queue.Empty:
                continue
            try:
                with self.lock:
                    if (record['owner'], record['session']) not in self.deleted:
                        with self.connect() as db:
                            db.execute('insert or replace into records values (?,?,?,?,?)',
                                       (record['id'], record['owner'], record['session'], record['trace'], raw))
            except Exception:
                self.dropped += 1
            finally:
                with self.lock:
                    self.pending_bytes -= size
                self.queue.task_done()

    def close(self, timeout=1):
        self.stopping.set()
        if self.thread:
            self.thread.join(timeout)
        if self.pending_bytes:
            self.dropped += 1
        self.provider.shutdown()

    @contextlib.contextmanager
    def turn(self, owner, session, request):
        if self.level == 'off':
            yield None
            return
        token = current.set((self, owner, session, request))
        try:
            with span('turn.execute') as scope:
                yield scope
        finally:
            current.reset(token)

    def diagnostic(self, owner, session, seconds=300):
        if not 0 < seconds <= 3600:
            raise ValueError('diagnostic scope must expire within one hour')
        self.scopes[(owner, session)] = time.monotonic() + seconds

    def delete(self, owner, session):
        with self.lock:
            self.deleted.add((owner, session))
            self.scopes.pop((owner, session), None)
            with self.connect() as db:
                db.execute('delete from records where owner=? and session=?', (owner, session))

    def query(self, owner, session):
        with self.connect() as db:
            records = [json.loads(row[0]) for row in db.execute('select body from records where owner=? and session=?', (owner, session))]
        return {'schema_version': 1, 'recording_complete': not self.dropped and all(r.get('recording_complete', True) for r in records),
                'dropped': self.dropped, 'records': records}


class Scope:
    def __init__(self, runtime, record):
        self.runtime, self.record = runtime, record

    def event(self, name, **attributes):
        try:
            event = {'id': uuid.uuid4().hex, 'name': name, 'attributes': clean(attributes)}
            if len(json.dumps(event).encode()) > 512*1024 or len(self.record['events']) >= 256:
                self.record['recording_complete'] = False
                self.runtime.dropped += 1
                return
            self.record['events'].append(event)
            self.runtime.emit(dict(self.record))
        except Exception:
            self.record['recording_complete'] = False
            self.runtime.dropped += 1

    def capture(self, role, value):
        r = self.record
        if self.runtime.scopes.get((r['owner'], r['session']), 0) <= time.monotonic():
            self.event('capture', role=role, status='disabled')
            return
        try:
            raw = json.dumps(clean(value), ensure_ascii=False)
            if len(raw.encode()) > 512*1024:
                r['recording_complete'] = False
                self.event('capture', role=role, status='truncated')
            else:
                self.event('capture', role=role, status='captured', value=json.loads(raw))
        except Exception:
            r['recording_complete'] = False
            self.event('capture', role=role, status='unavailable')


@contextlib.contextmanager
def span(name, **attributes):
    ctx = current.get()
    if ctx is None:
        yield None
        return
    runtime, owner, session, request = ctx
    parent = trace.get_current_span().get_span_context()
    with runtime.tracer.start_as_current_span(name, record_exception=False, set_status_on_exception=False) as native:
        identity = native.get_span_context()
        record = dict(schema_version=1, id=f'{identity.span_id:016x}', trace=f'{identity.trace_id:032x}',
                      parent=f'{parent.span_id:016x}' if parent.is_valid else None, name=name,
                      owner=owner, session=session, request=request, instance=runtime.instance,
                      time=time.time(), status='running', attributes=clean(attributes), events=[], recording_complete=True)
        scope = Scope(runtime, record)
        runtime.emit(dict(record))
        start = time.monotonic()
        try:
            yield scope
        except BaseException as exc:
            record['status'] = 'cancelled' if isinstance(exc, asyncio.CancelledError) else 'error'
            record['error_type'] = type(exc).__name__
            raise
        else:
            record['status'] = 'success'
        finally:
            record['elapsed_ms'] = (time.monotonic()-start)*1000
            runtime.emit(record)
