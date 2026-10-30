"""Regressions for independently reproduced trace contract gaps."""
import io
import logging
import pytest
from core.observability import Runtime, CorrelationFilter


@pytest.mark.parametrize('kind', ['exception', 'chain', 'cached', 'stack'])
def test_exception_channels_are_redacted(kind):
    secrets = ['SYNTHETIC_KNOWN_CREDENTIAL', 'Bearer SYNTHETIC_AUTH', 'sk-synthetic-credential']
    runtime = Runtime(secrets=[secrets[0]])
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(CorrelationFilter())
    handler.setFormatter(logging.Formatter('%(trace_id)s %(request_id)s %(message)s'))
    logger = logging.Logger('trace-regression')
    logger.addHandler(handler)
    try:
        with runtime.turn('u', 's', 'request') as scope:
            if kind in {'cached', 'stack'}:
                record = logging.LogRecord(logger.name, logging.ERROR, 'fixture.py', 1, 'safe', (), None)
                if kind == 'cached':
                    record.exc_text = 'ValueError: ' + ' '.join(secrets)
                else:
                    record.stack_info = 'Stack fixture ' + ' '.join(secrets)
                logger.handle(record)
            else:
                try:
                    try:
                        raise ValueError(' '.join(secrets))
                    except ValueError as cause:
                        if kind == 'chain':
                            raise RuntimeError('outer') from cause
                        raise
                except Exception:
                    logger.exception('known %s', secrets[0])
            result = stream.getvalue()
            assert all(secret not in result for secret in secrets)
            assert '[REDACTED]' in result and 'request' in result
            if kind != 'stack':
                assert 'ValueError' in result
            if kind == 'chain':
                assert 'RuntimeError: outer' in result
    finally:
        handler.close()
        runtime.close()


@pytest.mark.parametrize('gap', ['incomplete', 'running', 'expired'])
def test_request_completeness_is_scoped_before_aggregation(tmp_path, gap):
    import time
    from core.observability.maintenance import export, prune
    from sessions.store import SessionStore
    from application.conversations import Conversations
    from api.observability import create_router
    from core.auth import authenticated_user
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    store = SessionStore(tmp_path/'sessions.sqlite3')
    store.initialize()
    sid = store.create('u')['id']
    runtime = Runtime(tmp_path/'trace')
    runtime.start()
    manager = runtime.turn('u', sid, 'bad')
    bad = manager.__enter__()
    try:
        if gap == 'incomplete':
            bad.incomplete('fixture_gap')
        elif gap == 'expired':
            runtime.diagnostic('u', sid)
            bad.capture('fixture', 'synthetic content')
        if gap != 'running':
            manager.__exit__(None, None, None)
        with runtime.turn('u', sid, 'good'):
            pass
        runtime.flush()
        if gap == 'expired':
            prune(runtime, now=time.time()+8*86400)
        service = Conversations(store, None, observer=runtime)
        app = FastAPI()
        app.include_router(create_router(service))
        app.dependency_overrides[authenticated_user] = lambda: 'u'
        with TestClient(app) as client:
            path = f'/api/observability/sessions/{sid}'
            assert client.get(path).json()['recording_complete'] is False
            assert client.get(path+'?request_id=good').json()['recording_complete'] is True
            assert client.get(path+'?request_id=missing').json()['recording_complete'] is False
            assert export(runtime, 'u', sid, request='good')['recording_complete'] is True
            assert export(runtime, 'u', sid, request='bad')['recording_complete'] is False
            runtime.failed()
            assert client.get(path+'?request_id=good').json()['recording_complete'] is False
            assert export(runtime, 'u', sid, request='good')['recording_complete'] is False
    finally:
        if gap == 'running':
            manager.__exit__(None, None, None)
        runtime.close()
