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


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['classify', 'reasoning#1', 'pipeline#2', 'render'])
async def test_factory_records_public_prompt_version_independent_of_input(stage):
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from agent.llm import _maybe_wrap_telemetry
    from core.observability import invoke
    runtime = Runtime()
    model = _maybe_wrap_telemetry(FakeMessagesListChatModel(responses=[AIMessage(content='fixture')]), stage)
    try:
        with runtime.turn('u', 's', 'r'):
            for text in ('first private input', 'second different private input'):
                await invoke(model.ainvoke, text)
        models = [r for r in runtime.query('u', 's')['records'] if r['name'] == 'model.invoke']
        assert len(models) == 2
        versions = [r['attributes'].get('prompt_version') for r in models]
        assert versions[0] and versions[0] == versions[1]
        assert all(r['attributes'].get('prompt_version_status') == 'available' for r in models)
    finally:
        runtime.close()


def test_prompt_version_unavailable_is_explicit():
    from agent.observability import ModelObserver
    runtime = Runtime()
    try:
        with runtime.turn('u', 's', 'r'):
            observer = ModelObserver('external-template')
            observer.on_chat_model_start({}, [[]], run_id='fixture')
            observer.on_llm_error(ValueError('fixture'), run_id='fixture')
        model = next(r for r in runtime.query('u', 's')['records'] if r['name'] == 'model.invoke')
        assert model['attributes'].get('prompt_version') is None
        assert model['attributes'].get('prompt_version_status') == 'unavailable'
        assert model['attributes'].get('prompt_version_reason')
    finally:
        runtime.close()


def test_public_source_change_changes_version_and_missing_source_is_explicit(tmp_path, monkeypatch):
    from agent import prompt_versions as versions
    monkeypatch.setattr(versions, '_ROOT', tmp_path)
    monkeypatch.setattr(versions, '_TEMPLATE_SOURCES', {'classify': ('template.py',)})
    source = tmp_path/'template.py'
    try:
        source.write_text('PUBLIC_TEMPLATE = "first"')
        versions._bundle_version.cache_clear()
        first = versions.public_prompt_metadata('classify')['prompt_version']
        source.write_text('PUBLIC_TEMPLATE = "second"')
        # Simulate a fresh process loading changed public templates.
        versions._bundle_version.cache_clear()
        second = versions.public_prompt_metadata('classify')['prompt_version']
        assert first and second and first != second
        source.unlink()
        versions._bundle_version.cache_clear()
        missing = versions.public_prompt_metadata('classify')
        assert missing['prompt_version'] is None
        assert missing['prompt_version_reason'] == 'public_source_unavailable'
    finally:
        versions._bundle_version.cache_clear()


@pytest.mark.asyncio
async def test_missing_template_source_does_not_break_model_call(tmp_path, monkeypatch):
    from agent import prompt_versions as versions
    from agent.llm import _maybe_wrap_telemetry
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from core.observability import invoke
    monkeypatch.setattr(versions, '_ROOT', tmp_path)
    versions._bundle_version.cache_clear()
    runtime = Runtime()
    try:
        model = _maybe_wrap_telemetry(FakeMessagesListChatModel(responses=[AIMessage(content='fixture result')]), 'classify')
        with runtime.turn('u', 's', 'r'):
            result = await invoke(model.ainvoke, 'private input')
        assert result.content == 'fixture result'
        value = runtime.query('u', 's')
        assert value['recording_complete'] is False
        model_span = next(r for r in value['records'] if r['name'] == 'model.invoke')
        assert model_span['attributes']['prompt_version_reason'] == 'public_source_unavailable'
    finally:
        runtime.close()
        versions._bundle_version.cache_clear()
