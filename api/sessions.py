"""有归属、版本和幂等请求 ID 的会话 HTTP 合同。"""
import sqlite3
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from api.access import same_origin_local_request
from core.auth import authenticated_user
from sessions.models import SessionContext
from sessions.store import SessionError


class TurnRequest(BaseModel):
    request_id: UUID
    version: int = Field(ge=0)
    message: str = Field(min_length=1, max_length=20000)
    depth: Literal['fast', 'deep'] = 'fast'
    output_style: Literal['neutral', 'bangumi', 'bangumi_kawaii'] | None = None


class ContextRequest(BaseModel):
    version: int = Field(ge=0)
    context: SessionContext


def create_router(service, clear_cache):
    router = APIRouter(prefix='/api/sessions', dependencies=[Depends(same_origin_local_request)])
    store = service.store

    def run(action, *args):
        try:
            return action(*args)
        except SessionError as error:
            raise HTTPException(404 if error.code == 'not_found' else 409, detail=error.code) from None
        except sqlite3.Error:
            raise HTTPException(503, detail='会话存储不可用，未确认保存；请检查数据目录或磁盘空间。') from None

    @router.post('')
    async def create(owner: str = Depends(authenticated_user)):
        return run(store.create, owner)

    @router.get('')
    async def listing(owner: str = Depends(authenticated_user)):
        return run(store.list, owner)

    @router.get('/{sid}')
    async def read(sid: str, owner: str = Depends(authenticated_user)):
        return run(store.read, owner, sid)

    @router.patch('/{sid}/context')
    async def update(sid: str, body: ContextRequest, owner: str = Depends(authenticated_user)):
        return run(store.update_context, owner, sid, body.version, body.context.snapshot())

    @router.delete('/{sid}')
    async def delete(sid: str, version: int, owner: str = Depends(authenticated_user)):
        run(store.delete, owner, sid, version)
        service.deleted(owner, sid)
        await clear_cache(owner, sid)
        return {'deleted': True}

    @router.get('/{sid}/turns/{rid}')
    async def turn(sid: str, rid: str, owner: str = Depends(authenticated_user)):
        return run(service.inspect, owner, sid, rid)

    @router.post('/{sid}/turns/{rid}/cancel')
    async def cancel(sid: str, rid: str, owner: str = Depends(authenticated_user)):
        return run(service.cancel, owner, sid, rid)

    @router.post('/{sid}/turns')
    async def send(sid: str, body: TurnRequest, response: Response, wait: bool = False,
                   owner: str = Depends(authenticated_user)):
        args = (owner, sid, str(body.request_id), body.version, body.message, body.depth, body.output_style)
        if wait:
            try:
                return await service.send(*args)
            except SessionError as error:
                raise HTTPException(404 if error.code == 'not_found' else 409, detail=error.code) from None
            except sqlite3.Error:
                raise HTTPException(503, detail='会话存储不可用，未确认保存；请检查数据目录或磁盘空间。') from None
        turn = run(service.submit, *args)
        if turn['status'] in {'accepted', 'running', 'cancelling'}:
            response.status_code = 202
        return turn

    return router
