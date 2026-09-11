"""
限流中间件测试

覆盖：429 触发、429 响应携带 CORS 头（中间件顺序回归）、滑动窗口过期恢复、
计数表回收（防无界增长）。

注意：conftest 的 ``_disable_rate_limit_for_tests`` 全局关限流，
本文件测试自行开启并恢复（fixture teardown 会还原）。
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from core.config import get_settings
from main import app
from middleware import _requests, _WINDOW_SECONDS

client = TestClient(app)

# 限流测试只关心中间件，不关心 agent。每次 /chat 若不 mock graph，
# 就是一次完整的真实调用（classify + reasoning + render 三次付费 LLM）。
_STUB_STATE = {
    "messages": [AIMessage(content="（限流测试桩）")],
    "iterations": 0,
    "query_intent": "chat",
}


@pytest.fixture(autouse=True)
def _stub_agent_graph():
    """把 /chat 背后的 agent 换成固定返回，避免限流测试打真实 LLM。"""
    with patch("main.agent_app.ainvoke", return_value=_STUB_STATE):
        yield


def _enable_rate_limit(limit: int) -> None:
    """开启限流并清空计数表。"""
    get_settings().RATE_LIMIT_PER_MINUTE = limit
    _requests.clear()


class TestRateLimit:
    def test_rate_limited_returns_429(self):
        """窗口内超过上限 → 429 + 错误体"""
        _enable_rate_limit(2)
        for _ in range(2):
            assert client.post("/chat", json={"message": "你好"}).status_code == 200
        r = client.post("/chat", json={"message": "你好"})
        assert r.status_code == 429
        assert r.json()["error"] == "rate_limited"
        assert r.headers.get("retry-after") is not None

    def test_rate_limit_429_has_cors_headers(self):
        """回归：429 响应必须带 CORS 头（中间件顺序修复）。

        rate_limit_middleware 曾注册在 CORS 外层 → 429 不经过 CORS。
        """
        _enable_rate_limit(1)
        assert client.post("/chat", json={"message": "你好"}).status_code == 200
        r = client.post("/chat", json={"message": "你好"})
        assert r.status_code == 429
        assert r.headers.get("access-control-allow-origin") == "*"

    def test_rate_limit_window_expires(self):
        """窗口过期后恢复放行"""
        _enable_rate_limit(1)
        assert client.post("/chat", json={"message": "你好"}).status_code == 200
        assert client.post("/chat", json={"message": "你好"}).status_code == 429
        _requests.clear()  # 模拟 60s 后窗口失效
        assert client.post("/chat", json={"message": "你好"}).status_code == 200

    def test_health_exempt_from_rate_limit(self):
        """非对话端点豁免限流"""
        _enable_rate_limit(1)
        assert client.post("/chat", json={"message": "你好"}).status_code == 200
        assert client.post("/chat", json={"message": "你好"}).status_code == 429
        assert client.get("/health").status_code == 200


class TestRateLimitTableRecycling:
    """计数表回收（防每唯一 IP 残留空条目导致无界增长）"""

    def test_sweep_removes_stale_entries(self):
        from middleware import _sweep_stale

        _requests.clear()
        import time

        now = time.monotonic()
        # 活跃条目（1 分钟窗口内）应保留
        _requests["active-ip"] = __import__("collections").deque([now - 5])
        # 过期条目（窗口外）应被清除
        _requests["stale-ip"] = __import__("collections").deque(
            [now - _WINDOW_SECONDS - 1]
        )
        _sweep_stale(now)
        assert "active-ip" in _requests
        assert "stale-ip" not in _requests
