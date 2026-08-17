"""HTTP 中间件：IP 级滑动窗口限流。

预览期保护 LLM 成本：每 IP 每分钟最多 ``RATE_LIMIT_PER_MINUTE`` 个对话请求。
``RATE_LIMIT_PER_MINUTE <= 0`` 时完全关闭限流（测试环境 / 运维开关）。
"""

import logging
import time
from collections import deque

from fastapi import Request
from fastapi.responses import JSONResponse

from core.config import get_settings

logger = logging.getLogger("bgm-agent.middleware")

_WINDOW_SECONDS = 60.0  # 滑动窗口长度
_MAX_TRACKED_IPS = 4096  # 计数表条目上限，超过触发一次全表清扫

# {ip: deque[时间戳]}，只保留窗口内的请求时间戳。
# 单进程内存态：重启清零。多实例部署时需换成 Redis 方案。
_requests: dict[str, deque[float]] = {}


def _sweep_stale(now: float) -> None:
    """全表清扫：删除窗口已完全过期的 IP 条目。

    单 IP 的惰性清理只回收"再次被访问"的条目；从未再访问的 IP
    会残留一个过期 deque，长期运行后 _requests 无界增长。
    超过 _MAX_TRACKED_IPS 时触发一次全表清扫兜底。
    """
    stale = [
        ip
        for ip, window in _requests.items()
        if now - window[-1] > _WINDOW_SECONDS
    ]
    for ip in stale:
        del _requests[ip]


async def rate_limit_middleware(request: Request, call_next):
    # 只限制对话端点；/health 与文档页豁免（否则监控会误报）
    if request.url.path not in ("/chat", "/chat/stream"):
        return await call_next(request)

    limit = get_settings().RATE_LIMIT_PER_MINUTE
    if limit <= 0:
        return await call_next(request)

    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()

    # 清理窗口外时间戳；全部过期则删除条目，顺带回收词典空间
    window = _requests.get(ip)
    if window is not None:
        while window and now - window[0] > _WINDOW_SECONDS:
            window.popleft()
        if not window:
            del _requests[ip]
            window = None
    if window is None:
        window = _requests[ip] = deque()

    if len(window) >= limit:
        logger.warning("限流触发 (ip=%s, count=%d)", ip, len(window))
        response = JSONResponse(
            status_code=429,
            content={"error": "rate_limited", "message": "太快了，休息一下再来。"},
            headers={"Retry-After": str(int(_WINDOW_SECONDS))},
        )
        # BaseHTTPMiddleware 直接返回响应时（不调 call_next），响应不经过外层
        # CORSMiddleware → 429 收不到 CORS 头，浏览器无法读取错误详情。
        # 此处自补 CORS 头，与中间件注册顺序解耦。
        response.headers.setdefault("Access-Control-Allow-Origin", "*")
        response.headers.setdefault("Access-Control-Allow-Methods", "*")
        response.headers.setdefault("Access-Control-Allow-Headers", "*")
        return response
    window.append(now)
    # 计数表超过阈值时全表清扫，防无界增长。
    # 当前 IP 刚 append 最新时间戳，不可能被误删。
    if len(_requests) > _MAX_TRACKED_IPS:
        _sweep_stale(now)
    return await call_next(request)
