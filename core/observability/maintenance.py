"""Local retention and export operations; never touch the conversation database."""
import json
import time
from pathlib import Path


def artifact_target(root, relative):
    path = (root / relative).resolve()
    if root.resolve().joinpath('artifacts') not in path.parents:
        raise ValueError('invalid artifact reference')
    return path


def disk_bytes(runtime):
    return sum(p.stat().st_size for p in runtime.path.rglob('*') if p.is_file())


def prune(runtime, *, now=None, metadata_days=30, artifact_days=7, target_bytes=256*1024*1024):
    if runtime.path is None:
        return
    instant = time.time() if now is None else now
    with runtime.storage_lock:
        with runtime.connect() as db:
            rows = [(rid, json.loads(body)) for rid, body in db.execute('select id,body from records').fetchall()]
            groups = {}
            for rid, record in rows:
                groups.setdefault(record['trace'], []).append((rid, record))
            ordered = sorted(groups.values(), key=lambda group: min(r['time'] for _, r in group))
            estimated = disk_bytes(runtime)
            for group in ordered:
                active = any(r['status'] == 'running' for _, r in group)
                expired = max(r['time'] for _, r in group) < instant-metadata_days*86400
                over = estimated > target_bytes
                remove = expired or (over and not active)
                for rid, record in group:
                    if remove:
                        estimated -= len(json.dumps(record, ensure_ascii=False).encode())
                    changed = False
                    for artifact in record.get('artifacts', []):
                        if artifact.get('path') and (remove or record['time'] < instant-artifact_days*86400):
                            target = artifact_target(runtime.path, artifact['path'])
                            if target.exists():
                                estimated -= target.stat().st_size
                            target.unlink(missing_ok=True)
                            artifact.update(status='expired', reason='retention')
                            changed = True
                    if remove:
                        db.execute('delete from records where id=?', (rid,))
                    elif changed:
                        record['recording_complete'] = False
                        db.execute('update records set body=? where id=?', (json.dumps(record, ensure_ascii=False), rid))
            db.execute('delete from diagnostic_scopes where expires<?', (instant,))
        # Reclaim securely deleted SQLite pages, and remove unindexed artifact orphans.
        with runtime.connect() as db:
            referenced = {a['path'] for row in db.execute('select body from records') for a in json.loads(row[0]).get('artifacts', []) if a.get('path') and a['status']=='captured'}
        for path in runtime.path.glob('artifacts/*/*.json'):
            if str(path.relative_to(runtime.path)) not in referenced:
                artifact_target(runtime.path, str(path.relative_to(runtime.path))).unlink(missing_ok=True)
        with runtime.connect() as db:
            db.execute('vacuum')


def export(runtime, owner, session, *, content=False, request=None):
    from .runtime import summary
    result = runtime.query(owner, session, content=content)
    result['records'] = [r for r in result['records'] if request is None or r['request']==request]
    result['recording_complete'] = bool(result['records']) and result['recording_complete']
    result['summary'] = summary(result['records'])
    # Identity aliases remove reusable session and user identifiers from exported metadata.
    requests = {}
    for record in result['records']:
        record['owner'] = 'local-user'
        record['session'] = 'session-1'
        record['request'] = requests.setdefault(record['request'], f'request-{len(requests)+1}')
    def aliases(value):
        if isinstance(value, list):
            return [aliases(item) for item in value]
        if isinstance(value, dict):
            return {key: requests.setdefault(item, f'request-{len(requests)+1}') if key in {'request_id', 'source_request_id'} and isinstance(item, str) else aliases(item) for key, item in value.items()}
        return value
    for record in result['records']:
        record['attributes'] = aliases(record['attributes'])
        record['events'] = aliases(record['events'])
    return result
