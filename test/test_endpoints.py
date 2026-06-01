"""
FastAPI 端点测试

使用 TestClient 发起 HTTP 请求，验证状态码与响应结构。
"""

from __future__ import annotations


class TestHealthEndpoint:
    """健康检查端点 /health 测试。"""

    def test_health_returns_200_and_valid_json(self, test_client):
        """GET /health → 200，返回 status/version/environment。"""
        response = test_client.get("/health")
        assert response.status_code == 200

        data = response.json()
        assert data["status"] == "ok"
        assert "version" in data
        assert "environment" in data

    def test_health_environment_matches_config(self, test_client):
        """environment 字段应与配置一致。"""
        response = test_client.get("/health")
        assert response.json()["environment"] == "development"

    def test_health_response_has_expected_keys(self, test_client):
        """响应 JSON 应恰好包含 status, environment, version 三个键。"""
        response = test_client.get("/health")
        keys = set(response.json().keys())
        assert keys == {"status", "environment", "version"}
