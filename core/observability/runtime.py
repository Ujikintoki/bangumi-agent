"""Explicit OTel ownership, bounded local recording and task-local execution identity."""
import asyncio
import contextlib
import contextvars
import json
import logging
import os
from pathlib import Path
import queue
import re
import sqlite3
import threading
import time
import uuid
import weakref
import importlib.metadata
import platform
import subprocess

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry import trace

current = contextvars.ContextVar('observation', default=None)
_active_scope = contextvars.ContextVar('observation_span', default=None)
_default = None
logger = logging.getLogger('bgm-agent.trace')
_SECRET = re.compile(r'(?i)(bearer\s+\S+|sk-[a-z0-9_-]+)')
_FORBIDDEN = re.compile(r'(?i)(secret|password|authorization|api.?key|access_token|reasoning_content|hidden|headers)')
# Only program-supplied metadata can cross this boundary; arbitrary content uses capture.
_ATTRIBUTES = frozenset('stage sdk_run_id model temperature max_tokens tool_choice schema_version schema_hash prompt_version code_revision dirty python_version dependencies tool tool_call_id argument_keys status reason error_type actual_execution validation usage input_tokens output_tokens total_tokens cache_read finish_reason provider_attempts iteration history_candidates history_selected background_version selected omitted transformed references budget candidate_count selected_count origin_kind derived_from missing truncated source_id request_id attempt recorded_at'.split())


def clean(value, depth=0):
    if depth > 24:
        raise ValueError('observation nesting limit')
    if value is None or type(value) in (bool, int, float):
        return value
    if type(value) is str:
        return _SECRET.sub('[REDACTED]', value)
    if type(value) in (list, tuple):
        return [clean(v, depth+1) for v in value if not (type(v) is dict and v.get('type') in {'reasoning', 'thinking', 'analysis', 'redacted_thinking'})]
    if type(value) is dict:
        return {k: ('[ERROR BODY OMITTED]' if k.endswith('_error') else clean(v, depth+1)) for k, v in value.items() if type(k) is str and not _FORBIDDEN.search(k)}
    raise ValueError('unsupported observation value')


def metadata(value):
    return clean({k: v for k, v in value.items() if k in _ATTRIBUTES})


def configure(runtime):
    global _default
    _default = runtime


def configured():
    return _default


class Runtime:
    def __init__(self, path=None, *, level='metadata', capacity=2048, byte_limit=16*1024*1024, secrets=(), artifact_limit=512*1024, turn_limit=16*1024*1024):
        if level not in {'off', 'metadata'}:
            raise ValueError('diagnostics require an explicit session scope')
        self.path = Path(path) if path is not None else None
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
        self.records = {}
        self.secrets = tuple(value for value in secrets if isinstance(value, str) and value)
        self.artifact_limit, self.turn_limit = artifact_limit, turn_limit
        self.content_bytes = {}
        self.material_refs = {}
        self.scope_paths = {}
        self.artifact_values = {}
        self.versions = {}

    def start(self):
        if self.level == 'off' or self.thread or self.path is None:
            return
        self.versions = {'python': platform.python_version(), 'dependencies': {name: importlib.metadata.version(name) for name in ('opentelemetry-sdk', 'langchain-core', 'langgraph')}}
        try:
            root = Path(__file__).resolve().parents[2]
            revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, stderr=subprocess.DEVNULL, timeout=1).decode().strip()
            dirty = bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=root, stderr=subprocess.DEVNULL, timeout=1))
            self.versions.update(code_revision=revision, dirty=dirty)
        except (OSError, subprocess.SubprocessError):
            self.versions.update(code_revision='unknown', dirty=None)
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path, 0o700)
        with self.connect() as db:
            version = db.execute('pragma user_version').fetchone()[0]
            if version not in (0, 1):
                raise ValueError('unsupported trace schema')
            db.execute('create table if not exists records (id text primary key, owner text, session text, trace text, body text)')
            db.execute('pragma user_version=1')
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
        db = sqlite3.connect(self.path / 'traces.sqlite3', timeout=.1)
        db.execute('pragma secure_delete=on')
        return db

    def failed(self):
        self.dropped += 1
        if self.dropped == 1:
            logger.warning('trace recording incomplete')

    def encode(self, value):
        raw = json.dumps(clean(value), ensure_ascii=False)
        for secret in self.secrets:
            raw = raw.replace(json.dumps(secret, ensure_ascii=False)[1:-1], '[REDACTED]')
        return raw

    def reference(self, message):
        ctx = current.get()
        scope = active()
        if not ctx or not scope:
            return None
        key = (scope.record['trace'], id(message))
        old = self.material_refs.get(key)
        if old and old[0]() is message:
            return old[1]
        value = uuid.uuid4().hex
        self.material_refs[key] = (weakref.ref(message), value)
        return value

    def emit(self, record, artifact=None):
        try:
            raw = self.encode(record)
            size = len(raw.encode()) + (len(artifact[1].encode()) if artifact else 0)
            with self.lock:
                if (record['owner'], record['session']) in self.deleted:
                    return
                if self.path is None:
                    self.records[record['id']] = json.loads(raw)
                    if artifact:
                        self.artifact_values[artifact[0]] = json.loads(artifact[1])
                    return
                if not self.thread or self.stopping.is_set() or self.pending_bytes + size > self.byte_limit:
                    self.failed()
                    return
                self.queue.put_nowait((record['owner'], record['session'], record['id'], record['trace'], raw, size, artifact))
                self.pending_bytes += size
        except Exception:
            self.failed()

    def _write(self):
        while not self.stopping.is_set() or not self.queue.empty():
            try:
                owner, session, rid, tid, raw, size, artifact = self.queue.get(timeout=.02)
            except queue.Empty:
                continue
            try:
                with self.lock:
                    if (owner, session) not in self.deleted:
                        if artifact:
                            relative, payload = artifact
                            target = self.path / relative
                            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                            with open(target, 'x', encoding='utf-8', opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
                                stream.write(payload)
                        with self.connect() as db:
                            db.execute('insert or replace into records values (?,?,?,?,?)', (rid, owner, session, tid, raw))
            except Exception:
                self.failed()
                if artifact:
                    try:
                        (self.path / artifact[0]).unlink(missing_ok=True)
                    except OSError:
                        pass
            finally:
                with self.lock:
                    self.pending_bytes -= size
                self.queue.task_done()

    def flush(self, timeout=1):
        until = time.monotonic()+timeout
        while self.queue.unfinished_tasks and time.monotonic() < until:
            time.sleep(.001)
        return not self.queue.unfinished_tasks

    def close(self, timeout=1):
        self.stopping.set()
        if self.thread:
            self.thread.join(timeout)
        if self.pending_bytes:
            self.failed()
        self.provider.shutdown()

    @contextlib.contextmanager
    def turn(self, owner, session, request):
        if self.level == 'off':
            yield None
            return
        token = current.set((self, owner, session, request))
        try:
            with span('turn.execute', attempt=1) as scope:
                if scope:
                    scope.record['versions'] = self.versions
                yield scope
        finally:
            current.reset(token)

    def diagnostic(self, owner, session, seconds=300):
        if not 0 < seconds <= 3600:
            raise ValueError('diagnostic scope must expire within one hour')
        self.scopes[(owner, session)] = time.time() + seconds

    def delete(self, owner, session):
        with self.lock:
            self.deleted.add((owner, session))
            self.scopes.pop((owner, session), None)
            matching = list(self.query(owner, session)['records'])
            for record in matching:
                for artifact in record.get('artifacts', []):
                    relative = artifact.get('path')
                    self.artifact_values.pop(relative, None)
                    if self.path and relative:
                        (self.path / relative).unlink(missing_ok=True)
            self.records = {k: r for k, r in self.records.items() if (r['owner'], r['session']) != (owner, session)}
            if self.path:
                with self.connect() as db:
                    db.execute('delete from records where owner=? and session=?', (owner, session))

    def query(self, owner, session, *, content=True):
        if self.level == 'off':
            return {'schema_version': 1, 'recording_complete': False, 'dropped': self.dropped, 'records': [], 'reason': 'disabled'}
        if self.path is None:
            records = [json.loads(json.dumps(r)) for r in self.records.values() if (r['owner'], r['session']) == (owner, session)]
        else:
            with self.connect() as db:
                records = [json.loads(row[0]) for row in db.execute('select body from records where owner=? and session=?', (owner, session))]
        for record in records:
            for artifact in record.get('artifacts', []):
                if artifact['status'] != 'captured':
                    continue
                try:
                    if self.path is not None and not (self.path / artifact['path']).is_file():
                        raise FileNotFoundError()
                    if content:
                        artifact['value'] = self.artifact_values[artifact['path']] if self.path is None else json.loads((self.path / artifact['path']).read_text())
                except (OSError, ValueError, KeyError):
                    artifact['status'] = 'unavailable'
                    record['recording_complete'] = False
        return {'schema_version': 1, 'recording_complete': bool(records) and not self.dropped and all(r.get('recording_complete', True) for r in records),
                'dropped': self.dropped, 'records': records}


class Scope:
    def __init__(self, runtime, record, native=None):
        self.runtime, self.record, self.native = runtime, record, native
        self.started = time.monotonic()
        self.ended = False

    def event(self, name, **attributes):
        try:
            event = {'id': uuid.uuid4().hex, 'time': time.time(), 'name': name, 'attributes': metadata(attributes)}
            if len(json.dumps(event).encode()) > 16384 or len(self.record['events']) >= 256:
                self.record['recording_complete'] = False
                self.runtime.failed()
                return
            self.record['events'].append(event)
            self.runtime.emit(self.record)
        except Exception:
            self.record['recording_complete'] = False
            self.runtime.failed()

    def capture(self, role, value):
        r = self.record
        reference = {'id': uuid.uuid4().hex, 'role': role, 'status': 'disabled'}
        r.setdefault('artifacts', []).append(reference)
        if self.runtime.scopes.get((r['owner'], r['session']), 0) <= time.time():
            self.runtime.emit(r)
            return reference['id']
        try:
            raw = self.runtime.encode(value() if callable(value) else value)
            size = len(raw.encode())
            with self.runtime.lock:
                consumed = self.runtime.content_bytes.get(r['trace'], 0)
                if size > self.runtime.artifact_limit or consumed + size > self.runtime.turn_limit:
                    r['recording_complete'] = False
                    reference.update(status='truncated', reason='content_limit', original_bytes=size)
                    self.runtime.emit(r)
                else:
                    namespace = self.runtime.scope_paths.setdefault((r['owner'], r['session']), uuid.uuid4().hex)
                    relative = f"artifacts/{namespace}/{reference['id']}.json"
                    reference.update(status='captured', path=relative, bytes=size)
                    self.runtime.content_bytes[r['trace']] = consumed + size
                    self.runtime.emit(r, (relative, raw))
        except Exception:
            r['recording_complete'] = False
            reference.update(status='unavailable', reason='serialization')
            self.runtime.emit(r)
        return reference['id']

    def finish(self, error=None):
        if self.ended:
            return
        self.ended = True
        r = self.record
        r['elapsed_ms'] = (time.monotonic()-self.started)*1000
        r['status'] = 'cancelled' if isinstance(error, asyncio.CancelledError) else ('error' if error else 'success')
        if error:
            r['error_type'] = type(error).__name__
        self.runtime.emit(r)
        if self.native:
            self.native.end()
        if r['name'] == 'turn.execute':
            self.runtime.material_refs = {k: v for k, v in self.runtime.material_refs.items() if k[0] != r['trace']}
            self.runtime.content_bytes.pop(r['trace'], None)

    @contextlib.contextmanager
    def activate(self):
        identity_token = current.set((self.runtime, self.record["owner"], self.record["session"], self.record["request"]))
        token = _active_scope.set(self)
        with trace.use_span(self.native, end_on_exit=False, record_exception=False, set_status_on_exception=False):
            try:
                yield self
            finally:
                _active_scope.reset(token)
                current.reset(identity_token)


def begin(name, **attributes):
    ctx = current.get()
    if ctx is None:
        return None
    runtime, owner, session, request = ctx
    try:
        parent = trace.get_current_span().get_span_context()
        native = runtime.tracer.start_span(name)
        identity = native.get_span_context()
        record = dict(schema_version=1, id=f'{identity.span_id:016x}', trace=f'{identity.trace_id:032x}',
                      parent=f'{parent.span_id:016x}' if parent.is_valid else None, name=name,
                      owner=owner, session=session, request=request, instance=runtime.instance,
                      time=time.time(), status='running', attributes=metadata(attributes), events=[], recording_complete=True)
        scope = Scope(runtime, record, native)
        runtime.emit(record)
        return scope
    except Exception:
        runtime.failed()
        return None


@contextlib.contextmanager
def span(name, **attributes):
    scope = begin(name, **attributes)
    if scope is None:
        yield None
        return
    with scope.activate():
        try:
            yield scope
        except BaseException as exc:
            scope.finish(exc)
            raise
        else:
            if not getattr(scope, "deferred", False):
                scope.finish()


def active():
    return _active_scope.get()


def event(name, **attributes):
    scope = active()
    if scope:
        scope.event(name, **attributes)


def summary(records):
    models = [r for r in records if r['name'] == 'model.invoke']
    tools = [r for r in records if r['name'] == 'tool.execute']
    return {'elapsed_ms': max((r.get('elapsed_ms', 0) for r in records if r['name'] == 'turn.execute'), default=0),
            'llm_calls': [{'label': r['attributes'].get('stage'), 'elapsed_ms': r.get('elapsed_ms'),
                           'prompt_tokens': (r.get('usage') or {}).get('input_tokens'),
                           'completion_tokens': (r.get('usage') or {}).get('output_tokens')} for r in models],
            'tool_calls': [{'tool': r['attributes'].get('tool'), 'elapsed_ms': r.get('elapsed_ms'),
                            'status': r.get('result_status', r['status']), 'argument_keys': r['attributes'].get('argument_keys', []),
                            'error_type': r.get('error_type')} for r in tools],
            'node_timings': [{'node': r['name'][5:], 'elapsed_ms': r.get('elapsed_ms')} for r in records if r['name'].startswith('node.')]}


class CorrelationFilter(logging.Filter):
    def filter(self, record):
        identity = trace.get_current_span().get_span_context()
        record.trace_id = f'{identity.trace_id:032x}' if identity.is_valid else '-'
        record.span_id = f'{identity.span_id:016x}' if identity.is_valid else '-'
        ctx = current.get()
        record.request_id = ctx[3] if ctx else '-'
        return True


def reference(value):
    scope = active()
    return scope.runtime.reference(value) if scope else None


def capture(role, value):
    scope = active()
    return scope.capture(role, value) if scope else None
