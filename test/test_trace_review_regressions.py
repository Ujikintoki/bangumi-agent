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
