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
from .maintenance import artifact_target, disk_bytes, prune

from opentelemetry.sdk.trace import TracerProvider, SpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry import trace

current = contextvars.ContextVar('observation', default=None)
_active_scope = contextvars.ContextVar('observation_span', default=None)
_default = None
logger = logging.getLogger('bgm-agent.trace')
_SECRET = re.compile(r'(?i)(bearer\s+\S+|sk-[a-z0-9_-]+)')
_FORBIDDEN = re.compile(r'(?i)(secret|password|authorization|api.?key|access_token|reasoning_content|hidden|headers|(^|_)(token|credential|credentials|key|auth|cookie)$)')
# Only program-supplied metadata can cross this boundary; arbitrary content uses capture.
_ATTRIBUTES = frozenset('session_kind stage sdk_run_id model temperature max_tokens tool_choice schema_version schema_hash prompt_version code_revision dirty python_version dependencies tool tool_call_id argument_keys status reason error_type actual_execution validation usage input_tokens output_tokens total_tokens cache_read finish_reason provider_attempts iteration history_candidates history_selected background_version selected omitted transformed references budget candidate_count selected_count origin_kind derived_from missing truncated source_id request_id attempt recorded_at'.split())


def clean(value, depth=0, redactions=None):
    marks = redactions if redactions is not None else [0]
    if depth > 24:
        raise ValueError('observation nesting limit')
    if value is None or type(value) in (bool, int, float):
        return value
    if type(value) is str:
        result, count = _SECRET.subn('[REDACTED]', value)
        marks[0] += count
        return result
    if type(value) in (list, tuple):
        result = []
        for item in value:
            if type(item) is dict and item.get('type') in {'reasoning', 'thinking', 'analysis', 'redacted_thinking'}:
                marks[0] += 1
            else:
                result.append(clean(item, depth+1, marks))
        return result
    if type(value) is dict:
        result = {}
        for key, item in value.items():
            if type(key) is not str or _FORBIDDEN.search(key):
                marks[0] += 1
            elif key.endswith('_error'):
                marks[0] += 1
                result[key] = '[ERROR BODY OMITTED]'
            else:
                result[key] = clean(item, depth+1, marks)
        return result
    raise ValueError('unsupported observation value')


def oversized(value, limit, depth=0):
    """Bound preprocessing before allocating diagnostic copies; reject unknown objects."""
    if depth > 24:
        raise ValueError('observation nesting limit')
    if type(value) is str:
        return len(value)
    if value is None or type(value) in (int, float, bool):
        return 4
    total = 2
    if type(value) is dict:
        items = value.items()
    elif type(value) in (tuple, list):
        items = ((None, item) for item in value)
    else:
        raise ValueError('unsupported observation value')
    for key, item in items:
        total += (len(key) if type(key) is str else 0) + oversized(item, limit-total, depth+1) + 1
        if total > limit:
            break
    return total


def metadata(value):
    return clean({k: v for k, v in value.items() if k in _ATTRIBUTES})


def configure(runtime):
    global _default
    _default = runtime


def configured():
    return _default


class LocalProcessor(SpanProcessor):
    """OTel span lifecycle feeds the same immediate-event/local-writer record."""
    def __init__(self, runtime):
        self.runtime = runtime
        self.active_records = {}

    def on_start(self, native, parent_context=None):
        identity = current.get()
        if identity is None or identity[0] is not self.runtime:
            return
        _, owner, session, request = identity
        context = native.get_span_context()
        record = dict(schema_version=1, id=f'{context.span_id:016x}', trace=f'{context.trace_id:032x}',
                      parent=f'{native.parent.span_id:016x}' if native.parent else None, name=native.name,
                      owner=owner, session=session, request=request, instance=self.runtime.instance,
                      time=native.start_time/1e9, status='running', attributes={}, events=[], recording_complete=True)
        self.active_records[context.span_id] = record
        self.runtime.emit(record)

    def on_end(self, native):
        record = self.active_records.pop(native.context.span_id, None)
        if record:
            if record['status'] == 'running':
                record.update(status='unavailable', recording_complete=False)
            self.runtime.emit(record)

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis=1000):
        return self.runtime.flush(timeout_millis/1000)


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
        self.storage_lock = threading.RLock()
        self.dropped = 0
        self.deleted = set()
        self.scopes = {}
        self.provider = TracerProvider(sampler=ALWAYS_ON, shutdown_on_exit=False)
        self.tracer = self.provider.get_tracer('bgm-agent', '1')
        self.thread = None
        self.writer_lock = None
        self.stopping = threading.Event()
        self.instance = uuid.uuid4().hex
        self.processor = LocalProcessor(self)
        self.provider.add_span_processor(self.processor)
        self.records = {}
        self.secrets = tuple(value for value in secrets if isinstance(value, str) and value)
        self.artifact_limit, self.turn_limit = artifact_limit, turn_limit
        self.content_bytes = {}
        self.material_refs = {}
        self.scope_paths = {}
        self.total_limit = 256*1024*1024
        self.artifact_values = {}
        self.versions = {}

    def start(self):
        try:
            self._start()
        except BaseException:
            self._release_writer_lock()
            raise

    def _start(self):
        if self.level == 'off' or self.thread or self.path is None:
            return
        self.versions = {'python': platform.python_version(), 'dependencies': {name: importlib.metadata.version(name) for name in ('opentelemetry-api', 'opentelemetry-sdk', 'langchain-core', 'langchain-openai', 'langgraph', 'langgraph-prebuilt', 'pydantic')}}
        try:
            root = Path(__file__).resolve().parents[2]
            revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, stderr=subprocess.DEVNULL, timeout=1).decode().strip()
            dirty = bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=root, stderr=subprocess.DEVNULL, timeout=1))
            self.versions.update(code_revision=revision, dirty=dirty)
        except (OSError, subprocess.SubprocessError):
            self.versions.update(code_revision='unknown', dirty=None)
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path, 0o700)
        os.close(os.open(self.path / '.writer.lock', os.O_CREAT | os.O_RDWR, 0o600))
        self.writer_lock = sqlite3.connect(self.path / '.writer.lock', timeout=0, check_same_thread=False)
        self.writer_lock.execute('BEGIN EXCLUSIVE')
        with self.connect() as db:
            version = db.execute('pragma user_version').fetchone()[0]
            if version not in (0, 1):
                raise ValueError('unsupported trace schema')
            db.execute('create table if not exists records (id text primary key, owner text, session text, trace text, body text)')
            db.execute('create table if not exists tombstones (owner text, session text, primary key(owner, session))')
            db.execute('create table if not exists diagnostic_scopes (owner text, session text, expires real, primary key(owner, session))')
            self.deleted.update(db.execute('select owner,session from tombstones').fetchall())
            db.execute('pragma user_version=1')
            for rid, body in db.execute('select id, body from records').fetchall():
                record = json.loads(body)
                if record.get('status') == 'running':
                    record['status'] = 'interrupted'
                    record['recording_complete'] = False
                    db.execute('update records set body=? where id=?', (json.dumps(record), rid))
        os.chmod(self.path / 'traces.sqlite3', 0o600)
        for owner, session in list(self.deleted):
            self.delete(owner, session)
        prune(self, target_bytes=self.total_limit)
        self.thread = threading.Thread(target=self._write, daemon=True)
        self.thread.start()

    @contextlib.contextmanager
    def connect(self):
        with contextlib.closing(sqlite3.connect(self.path / 'traces.sqlite3', timeout=.1)) as db:
            db.execute('pragma secure_delete=on')
            with db:
                yield db

    def failed(self):
        self.dropped += 1
        if self.dropped == 1:
            logger.warning('trace recording incomplete')

    def encode(self, value, redactions=None):
        marks = redactions if redactions is not None else [0]
        raw = json.dumps(clean(value, redactions=marks), ensure_ascii=False)
        for secret in self.secrets:
            encoded = json.dumps(secret, ensure_ascii=False)[1:-1]
            marks[0] += raw.count(encoded)
            raw = raw.replace(encoded, '[REDACTED]')
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
        last_prune = time.monotonic()
        while not self.stopping.is_set() or not self.queue.empty():
            try:
                owner, session, rid, tid, raw, size, artifact = self.queue.get(timeout=.02)
            except queue.Empty:
                if time.monotonic()-last_prune > 60:
                    try:
                        prune(self, target_bytes=self.total_limit)
                    except Exception:
                        self.failed()
                    last_prune = time.monotonic()
                continue
            try:
                with self.storage_lock:
                    if (owner, session) not in self.deleted:
                        incoming = len(raw.encode()) + (len(artifact[1].encode()) if artifact else 0) + 8192
                        if disk_bytes(self) + incoming > self.total_limit:
                            prune(self, target_bytes=max(0, self.total_limit-incoming))
                            if disk_bytes(self) + incoming > self.total_limit:
                                self.failed()
                                continue
                        if artifact:
                            relative, payload = artifact
                            target = artifact_target(self.path, relative)
                            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                            with open(target, 'x', encoding='utf-8', opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
                                stream.write(payload)
                        with self.connect() as db:
                            db.execute('insert or replace into records values (?,?,?,?,?)', (rid, owner, session, tid, raw))
            except Exception:
                self.failed()
                if artifact:
                    try:
                        artifact_target(self.path, artifact[0]).unlink(missing_ok=True)
                    except OSError:
                        pass
            finally:
                with self.lock:
                    self.pending_bytes -= size
                self.queue.task_done()

        self._release_writer_lock()

    def _release_writer_lock(self):
        with self.lock:
            lock, self.writer_lock = self.writer_lock, None
        if lock is not None:
            lock.close()

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
        try:
            self.provider.shutdown()
        except Exception:
            self.failed()
        if self.writer_lock is not None and (self.thread is None or not self.thread.is_alive()):
            self._release_writer_lock()

    def detached_turn(self, owner, session, request):
        with self.turn(owner, session, request) as scope:
            if scope:
                scope.deferred = True
            return scope

    def sessions(self):
        if self.path is None or not (self.path / 'traces.sqlite3').exists():
            return []
        with self.connect() as db:
            return list({(r['owner'], r['session']) for row in db.execute('select body from records')
                         for r in [json.loads(row[0])] if r.get('attributes', {}).get('session_kind') == 'persistent'})

    @contextlib.contextmanager
    def turn(self, owner, session, request, *, persistent=False):
        if self.level == 'off':
            yield None
            return
        if self.path and self.thread:
            try:
                with self.connect() as db:
                    row = db.execute('select expires from diagnostic_scopes where owner=? and session=?', (owner, session)).fetchone()
                if row:
                    self.scopes[(owner, session)] = row[0]
            except Exception:
                self.failed()
        token = current.set((self, owner, session, request))
        try:
            with trace.use_span(trace.INVALID_SPAN, end_on_exit=False):
                with span('turn.execute', attempt=1, session_kind='persistent' if persistent else 'transient') as scope:
                    if scope:
                        scope.record['versions'] = self.versions
                    yield scope
        finally:
            current.reset(token)

    def diagnostic(self, owner, session, seconds=300):
        if not 0 < seconds <= 3600:
            raise ValueError('diagnostic scope must expire within one hour')
        if (owner, session) in self.deleted:
            raise ValueError('session was deleted')
        if self.path:
            with self.connect() as db:
                if db.execute('select 1 from tombstones where owner=? and session=?', (owner, session)).fetchone():
                    raise ValueError('session was deleted')
        self.scopes[(owner, session)] = time.time() + seconds
        if self.path:
            with self.connect() as db:
                db.execute('insert or replace into diagnostic_scopes values (?,?,?)', (owner, session, self.scopes[(owner, session)]))

    def delete(self, owner, session):
        with self.lock:
            self.deleted.add((owner, session))
        with self.storage_lock:
            if self.path and not (self.path / 'traces.sqlite3').exists():
                return
            if self.path:
                with self.connect() as db:
                    db.execute('insert or ignore into tombstones values (?,?)', (owner, session))
            self.scopes.pop((owner, session), None)
            matching = self._read_records(owner, session)
            for record in matching:
                for artifact in record.get('artifacts', []):
                    relative = artifact.get('path')
                    self.artifact_values.pop(relative, None)
                    if self.path and relative:
                        artifact_target(self.path, relative).unlink(missing_ok=True)
            self.records = {k: r for k, r in self.records.items() if (r['owner'], r['session']) != (owner, session)}
            if self.path:
                with self.connect() as db:
                    db.execute('insert or ignore into tombstones values (?,?)', (owner, session))
                    db.execute('delete from diagnostic_scopes where owner=? and session=?', (owner, session))
                    db.execute('delete from records where owner=? and session=?', (owner, session))

    def _read_records(self, owner, session):
        if self.path is None:
            return [json.loads(json.dumps(r)) for r in self.records.values() if (r['owner'], r['session']) == (owner, session)]
        with self.connect() as db:
            if db.execute('pragma user_version').fetchone()[0] != 1:
                raise ValueError('unsupported trace schema')
            return [json.loads(row[0]) for row in db.execute('select body from records where owner=? and session=?', (owner, session))]

    def health(self):
        return {'level': self.level, 'writer_running': bool(self.thread and self.thread.is_alive()),
                'dropped': self.dropped, 'queued_records': self.queue.qsize(), 'queued_bytes': self.pending_bytes}

    def query(self, owner, session, *, content=True):
        if self.path and (self.path / 'traces.sqlite3').exists():
            with self.connect() as db:
                if db.execute('pragma user_version').fetchone()[0] != 1:
                    raise ValueError('unsupported trace schema')
                deleted = db.execute('select 1 from tombstones where owner=? and session=?', (owner, session)).fetchone()
        else:
            deleted = False
        if deleted or (owner, session) in self.deleted:
            return {'schema_version': 1, 'recording_complete': False, 'dropped': self.dropped, 'records': [], 'reason': 'deleted'}
        if self.level == 'off' and (self.path is None or not (self.path / 'traces.sqlite3').exists()):
            return {'schema_version': 1, 'recording_complete': False, 'dropped': self.dropped, 'records': [], 'reason': 'disabled'}
        records = self._read_records(owner, session)
        for record in records:
            for artifact in record.get('artifacts', []):
                if artifact['status'] != 'captured':
                    continue
                try:
                    if self.path is not None and not artifact_target(self.path, artifact['path']).is_file():
                        raise FileNotFoundError()
                    if content:
                        artifact['value'] = self.artifact_values[artifact['path']] if self.path is None else json.loads(artifact_target(self.path, artifact['path']).read_text())
                except (OSError, ValueError, KeyError):
                    artifact['status'] = 'unavailable'
                    record['recording_complete'] = False
        return {'schema_version': 1, 'recording_complete': bool(records) and not self.dropped and all(r.get('recording_complete', True) and r['status'] != 'running' for r in records),
                'dropped': self.dropped, 'records': records}


class Scope:
    def __init__(self, runtime, record, native=None):
        self.runtime, self.record, self.native = runtime, record, native
        self.started = time.monotonic()
        self.ended = False

    @property
    def name(self):
        return self.record['name']

    @property
    def is_finished(self):
        return self.ended

    @property
    def elapsed_ms(self):
        return self.record.get('elapsed_ms')

    def defer(self):
        self.deferred = True

    def annotate(self, **fields):
        allowed = {'usage', 'finish_reason', 'response_model', 'accepted_at', 'committed_at',
                   'accepted_to_commit_ms', 'commit_outcome', 'result_status', 'error_type'}
        try:
            self.record.update(clean({key: value for key, value in fields.items() if key in allowed}))
            self.runtime.emit(self.record)
        except Exception:
            self.incomplete('annotation_conversion')

    def set_attributes(self, **fields):
        try:
            self.record['attributes'].update(metadata(fields))
            self.runtime.emit(self.record)
        except Exception:
            self.incomplete('attribute_conversion')

    def incomplete(self, reason):
        self.record['recording_complete'] = False
        gaps = self.record.setdefault('recording_gaps', [])
        if reason not in gaps and len(gaps) < 16:
            gaps.append(reason)
        self.runtime.emit(self.record)

    def event(self, name, **attributes):
        try:
            event = {'id': uuid.uuid4().hex, 'time': time.time(), 'name': name, 'attributes': metadata(attributes)}
            if len(json.dumps(event).encode()) > 16384 or len(self.record['events']) >= 256:
                self.record['recording_complete'] = False
                gaps = self.record.setdefault('recording_gaps', [])
                if 'event_limit' not in gaps:
                    gaps.append('event_limit')
                self.runtime.failed()
                self.runtime.emit(self.record)
                return
            self.record['events'].append(event)
            self.runtime.emit(self.record)
        except Exception:
            self.record['recording_complete'] = False
            self.runtime.failed()

    def capture(self, role, value):
        r = self.record
        if len(r.get('artifacts', [])) >= 128:
            r['recording_complete'] = False
            gaps = r.setdefault('recording_gaps', [])
            if 'artifact_count_limit' not in gaps:
                gaps.append('artifact_count_limit')
            self.runtime.failed()
            self.runtime.emit(r)
            return None
        reference = {'id': uuid.uuid4().hex, 'role': role, 'status': 'disabled'}
        r.setdefault('artifacts', []).append(reference)
        if self.runtime.scopes.get((r['owner'], r['session']), 0) <= time.time():
            self.runtime.emit(r)
            return reference['id']
        try:
            prepared = value() if callable(value) else value
            if oversized(prepared, self.runtime.artifact_limit) > self.runtime.artifact_limit:
                r['recording_complete'] = False
                reference.update(status='truncated', reason='content_limit', original_bytes=None)
                self.runtime.emit(r)
                return reference['id']
            redactions = [0]
            raw = self.runtime.encode(prepared, redactions)
            reference.update(redacted=bool(redactions[0]), redaction_count=redactions[0])
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

    def finish(self, error=None, *, status=None):
        if self.ended:
            return
        self.ended = True
        r = self.record
        if self.runtime.dropped:
            r['recording_complete'] = False
        r['elapsed_ms'] = (time.monotonic()-self.started)*1000
        r['status'] = status if status in {'unavailable', 'interrupted'} else ('cancelled' if isinstance(error, asyncio.CancelledError) else ('error' if error else 'success'))
        if error:
            r['error_type'] = type(error).__name__
        if self.native:
            try:
                self.native.end()
            except Exception:
                self.runtime.failed()
                r['recording_complete'] = False
                r.setdefault('recording_gaps', []).append('sdk_end_failure')
                self.runtime.processor.active_records.pop(self.native.get_span_context().span_id, None)
                self.runtime.emit(r)
        else:
            self.runtime.emit(r)
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
        native = runtime.tracer.start_span(name)
        identity = native.get_span_context()
        record = runtime.processor.active_records[identity.span_id]
        record['attributes'] = metadata(attributes)
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
    return {'elapsed_ms': max((r['elapsed_ms'] for r in records if r['name'] == 'turn.execute' and r.get('elapsed_ms') is not None), default=None),
            'turn_timings': [{'trace': r['trace'], 'execution_ms': r.get('elapsed_ms'), 'accepted_to_commit_ms': r.get('accepted_to_commit_ms'), 'commit_outcome': r.get('commit_outcome')} for r in records if r['name']=='turn.execute'],
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
        try:
            runtime = ctx[0] if ctx else configured()
            def redact(value):
                value = _SECRET.sub('[REDACTED]', value)
                for secret in runtime.secrets if runtime else ():
                    value = value.replace(secret, '[REDACTED]')
                return value
            record.msg, record.args = redact(record.getMessage()), ()
            # Formatter appends these channels after getMessage(). Render once, then
            # remove the raw exception tuple so no later handler can reconstruct it.
            if record.exc_text:
                record.exc_text = redact(record.exc_text)
            elif record.exc_info:
                record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
            if record.stack_info:
                record.stack_info = redact(record.stack_info)
        except Exception:
            record.msg, record.args = 'log content unavailable', ()
            record.exc_info, record.exc_text, record.stack_info = None, None, None
        return True


def reference(value):
    scope = active()
    try:
        return scope.runtime.reference(value) if scope else None
    except Exception:
        if scope:
            scope.runtime.failed()
        return None


def capture(role, value):
    scope = active()
    return scope.capture(role, value) if scope else None


pending_invocation = contextvars.ContextVar('observation_invocation', default=None)


async def invoke(call, *args, **kwargs):
    """Await exactly once; let framework adapters close their pending observations."""
    pending = []
    token = pending_invocation.set(pending)
    error = None
    try:
        return await call(*args, **kwargs)
    except BaseException as caught:
        error = caught
        raise
    finally:
        for finish in pending:
            try:
                finish(error)
            except Exception:
                ctx = current.get()
                if ctx:
                    ctx[0].failed()
        pending_invocation.reset(token)


def observe_sync(name):
    import functools
    def decorate(function):
        @functools.wraps(function)
        def wrapped(*args, **kwargs):
            with span(name):
                return function(*args, **kwargs)
        return wrapped
    return decorate


def register_invocation_finalizer(callback):
    """Framework adapters register completion repair without owning invocation context."""
    pending = pending_invocation.get()
    if pending is not None:
        pending.append(callback)
