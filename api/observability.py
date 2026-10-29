"""Owned, same-origin trace access. Diagnostic scopes are granted locally, never by HTTP."""
import sqlite3
from fastapi import APIRouter, Depends, HTTPException
from api.access import same_origin_local_request
from core.auth import authenticated_user
from core.observability import summary
from sessions.store import SessionError


def create_router(service):
    router = APIRouter(prefix='/api/observability', dependencies=[Depends(same_origin_local_request)])

    @router.get('/health')
    async def health(owner: str = Depends(authenticated_user)):
        return service.observer.health() if service.observer else {'level': 'unavailable', 'writer_running': False}

    @router.get('/sessions/{sid}')
    async def inspect(sid: str, request_id: str | None = None, content: bool = False,
                      owner: str = Depends(authenticated_user)):
        try:
            session = service.store.read(owner, sid)
        except SessionError:
            raise HTTPException(404, detail='not_found') from None
        observer = service.observer
        if observer is None:
            return {'recording_complete': False, 'reason': 'unavailable', 'records': []}
        try:
            result = observer.query(owner, sid, content=content)
        except (sqlite3.Error, OSError, ValueError):
            return {'recording_complete': False, 'reason': 'storage_unavailable', 'records': []}
        records = [r for r in result['records'] if request_id is None or r['request'] == request_id]
        result['collection_level'] = observer.level
        result['records'] = records
        result['recording_complete'] = bool(records) and result['recording_complete']
        result['summary'] = summary(records)
        result['turns'] = [{'request_id': t['request_id'], 'status': t['status'], 'reason': t.get('reason')} for t in session['turns'] if request_id is None or t['request_id'] == request_id]
        if not records:
            result['reason'] = 'disabled' if observer.level == 'off' else 'not_recorded'
        return result

    return router
