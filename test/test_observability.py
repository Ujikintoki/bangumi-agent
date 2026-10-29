import asyncio
import json
import time
import pytest
from core.observability import Runtime, span


def test_scope_privacy_and_delete(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.start()
    with runtime.turn('u', 's', 'r'):
        with span('model.invoke') as scope:
            scope.capture('input', {'content': 'private'})
    runtime.close()
    assert 'private' not in json.dumps(runtime.query('u', 's'))
    assert not runtime.query('other', 's')['records']
    runtime.delete('u', 's')
    assert not runtime.query('u', 's')['records']


def test_diagnostic_and_failure(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.start()
    runtime.diagnostic('u', 's')
    with pytest.raises(ValueError):
        with runtime.turn('u', 's', 'r') as scope:
            scope.capture('input', {'api_key': 'secret', 'content': 'Bearer abc'})
            raise ValueError('private error')
    runtime.close()
    raw = json.dumps(runtime.query('u', 's'))
    assert 'secret' not in raw and 'Bearer abc' not in raw and 'private error' not in raw
    assert 'REDACTED' in raw and 'error' in raw


@pytest.mark.asyncio
async def test_parallel_cancel(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.start()
    async def run(s):
        with runtime.turn('u', s, 'r'):
            await asyncio.sleep(0)
            with span('child'):
                if s == 'a':
                    raise asyncio.CancelledError()
    await asyncio.gather(run('a'), run('b'), return_exceptions=True)
    runtime.close()
    a = runtime.query('u', 'a')['records']
    b = runtime.query('u', 'b')['records']
    assert len({r['trace'] for r in a+b}) == 2
    assert all(r['status'] == 'cancelled' for r in a)


def test_queue_failure_restart_schema(tmp_path):
    runtime = Runtime(tmp_path, byte_limit=1)
    runtime.start()
    with runtime.turn('u', 's', 'r'):
        pass
    runtime.close()
    assert runtime.dropped
    runtime = Runtime(tmp_path)
    runtime.start()
    with runtime.connect() as db:
        db.execute('pragma user_version=99')
    runtime.close()
    with pytest.raises(ValueError):
        Runtime(tmp_path).start()


def test_late_write_and_storage_failure(tmp_path):
    runtime = Runtime(tmp_path)
    runtime.start()
    with runtime.turn('u', 's', 'r') as scope:
        runtime.delete('u', 's')
        scope.event('late')
    runtime.close()
    assert runtime.query('u', 's')['records'] == []
    runtime = Runtime(tmp_path / 'failed')
    runtime.start()
    def unavailable():
        raise OSError('private storage error')
    runtime.connect = unavailable
    with runtime.turn('u', 's', 'r'):
        pass
    runtime.close()
    assert runtime.dropped


def test_restart_and_no_import_io(tmp_path):
    runtime = Runtime(tmp_path / 'new')
    assert not runtime.path.exists()
    runtime.start()
    with runtime.turn('u', 's', 'r') as scope:
        record = dict(scope.record)
    runtime.close()
    record['status'] = 'running'
    with runtime.connect() as db:
        db.execute('update records set body=? where id=?', (json.dumps(record), record['id']))
    recovered = Runtime(runtime.path)
    recovered.start()
    recovered.close()
    result = recovered.query('u', 's')
    assert result['records'][0]['status'] == 'interrupted'
    assert not result['recording_complete']
