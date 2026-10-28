"""本机网页接口拒绝其他网站读取或修改个人会话。"""
from fastapi import HTTPException, Request

from core.config import get_settings


def same_origin_local_request(request: Request):
    origin = request.headers.get('origin')
    expected = f'{request.url.scheme}://{request.url.netloc}'
    if origin is not None and origin != expected:
        raise HTTPException(403, detail='Cross-origin local API access is not allowed')
    hosts = {'127.0.0.1', 'localhost', '::1'}
    if get_settings().ENVIRONMENT == 'test':
        hosts.add('testserver')
    if not get_settings().AUTH_ENABLED and request.url.hostname not in hosts:
        raise HTTPException(403, detail='Anonymous local API requires a loopback host')
