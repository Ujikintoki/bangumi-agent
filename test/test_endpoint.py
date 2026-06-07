"""
/chat 端点测试（mock LLM）

验证请求/响应模型、状态初始化、响应提取、流式输出。
可独立运行: python -m pytest test/test_endpoint.py -v
"""

from __future__ import annotations

import json
from unittest.mock import patch

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from main import app, ChatResponse
from test.conftest import MOCK_TOOLS, make_mock_llm

client = TestClient(app)


class TestHealthEndpoint:
    """/health 端点"""

    def test_health_returns_ok(self):
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert "version" in data


class TestChatEndpoint:
    """POST /chat"""

    @patch("main.agent_app.invoke")
    def test_returns_chat_response_structure(self, mock_invoke):
        """响应包含所有必需字段"""
        mock_invoke.return_value = {
            "messages": [
                SystemMessage(content="..."),
                HumanMessage(content="你好"),
                AIMessage(content="你好！有什么可以帮你的？"),
            ],
            "iterations": 1,
            "query_intent": "chitchat",
            "critic_status": "PASS",
        }

        response = client.post("/chat", json={
            "message": "你好", "session_id": "s1", "user_id": "u1",
        })
        assert response.status_code == 200
        data = response.json()
        assert "reply" in data
        assert "iterations" in data
        assert "tools_used" in data
        assert "query_intent" in data

    @patch("main.agent_app.invoke")
    def test_extracts_final_ai_reply(self, mock_invoke):
        """提取最后一条有内容的 AIMessage"""
        mock_invoke.return_value = {
            "messages": [
                SystemMessage(content="..."),
                HumanMessage(content="什么是三集定律"),
                AIMessage(content="三集定律是指新番播出三集后..."),
            ],
            "iterations": 1,
            "query_intent": "factual",
        }

        response = client.post("/chat", json={"message": "什么是三集定律"})
        data = response.json()
        assert "三集定律" in data["reply"]

    @patch("main.agent_app.invoke")
    def test_skips_tool_call_aimessages(self, mock_invoke):
        """跳过纯 tool_call 的 AIMessage（content 为空）"""
        mock_invoke.return_value = {
            "messages": [
                SystemMessage(content="..."),
                HumanMessage(content="搜巨人"),
                AIMessage(content="", tool_calls=[{"name": "search", "args": {}, "id": "c1"}]),
                ToolMessage(content="结果", tool_call_id="c1", name="search"),
                AIMessage(content="进击的巨人评分 8.5。"),
            ],
            "iterations": 2,
            "query_intent": "lookup",
        }

        response = client.post("/chat", json={"message": "搜进击的巨人"})
        data = response.json()
        assert "8.5" in data["reply"]

    @patch("main.agent_app.invoke")
    def test_fallback_to_aimessage_with_tool_calls(self, mock_invoke):
        """降级策略：找不到干净 AIMessage 时，退而求其次用带 tool_calls 的"""
        mock_invoke.return_value = {
            "messages": [
                SystemMessage(content="..."),
                HumanMessage(content="攻壳机动队评分？"),
                AIMessage(
                    content="让我帮你搜索攻壳机动队的信息。",
                    tool_calls=[{"name": "search", "args": {"keyword": "攻壳机动队"}, "id": "c1"}],
                ),
                ToolMessage(content="找到 3 个结果", tool_call_id="c1", name="search"),
            ],
            "iterations": 1,
            "query_intent": "lookup",
        }

        response = client.post("/chat", json={"message": "攻壳机动队评分？"})
        data = response.json()
        # 降级：找不到无 tool_calls 的 AIMessage，退而求其次
        assert "搜索" in data["reply"]
        assert data["iterations"] == 1

    @patch("main.agent_app.invoke")
    def test_extracts_tools_used(self, mock_invoke):
        """提取并去重工具名称"""
        mock_invoke.return_value = {
            "messages": [
                SystemMessage(content="..."),
                HumanMessage(content="搜巨人"),
                AIMessage(content="", tool_calls=[{"name": "search", "args": {}, "id": "c1"}]),
                ToolMessage(content="结果", tool_call_id="c1", name="search_bangumi_subject"),
                AIMessage(content="", tool_calls=[{"name": "detail", "args": {}, "id": "c2"}]),
                ToolMessage(content="详情", tool_call_id="c2", name="get_bangumi_subject_detail"),
                AIMessage(content="完整分析..."),
            ],
            "iterations": 3,
            "query_intent": "lookup",
        }

        response = client.post("/chat", json={"message": "搜进击的巨人"})
        data = response.json()
        assert "search_bangumi_subject" in data["tools_used"]
        assert "get_bangumi_subject_detail" in data["tools_used"]

    @patch("main.agent_app.invoke")
    def test_handles_agent_exception(self, mock_invoke):
        """Agent 异常时返回错误消息而不是 500"""
        mock_invoke.side_effect = RuntimeError("模拟的 Agent 崩溃")

        response = client.post("/chat", json={"message": "测试"})
        assert response.status_code == 200
        data = response.json()
        assert "异常" in data["reply"]
        assert data["iterations"] == 0

    def test_rejects_empty_message(self):
        """拒绝空消息（Pydantic 校验）"""
        response = client.post("/chat", json={"message": ""})
        assert response.status_code == 422  # validation error

    def test_message_field_required(self):
        """message 字段必填"""
        response = client.post("/chat", json={})
        assert response.status_code == 422

    def test_default_session_and_user(self):
        """session_id 和 user_id 有默认值"""
        response = client.post("/chat", json={"message": "你好"})
        assert response.status_code == 200  # 不因缺失而 422


class TestChatStreamEndpoint:
    """POST /chat/stream"""

    @patch("main.agent_app.astream")
    def test_stream_returns_sse_format(self, mock_astream):
        """流式响应包含 SSE 格式的 data: 前缀和 [DONE]"""
        async def mock_stream(state):
            yield {"reasoning_node": {"query_intent": "chitchat", "last_tool_calls": []}}
            yield {"critic_node": {"critic_status": "PASS", "critic_feedback": ""}}

        mock_astream.return_value = mock_stream(0)

        with client.stream("POST", "/chat/stream", json={"message": "你好"}) as response:
            assert response.status_code == 200
            assert "text/event-stream" in response.headers["content-type"]
            body = response.read().decode()
            assert "data:" in body
            assert "[DONE]" in body

    @patch("main.agent_app.astream")
    def test_stream_includes_reasoning_event(self, mock_astream):
        """reasoning_node 事件包含 intent 和 tool_calls"""
        async def mock_stream(state):
            yield {"reasoning_node": {
                "query_intent": "lookup",
                "last_tool_calls": [{"name": "search_bangumi_subject"}],
            }}

        mock_astream.return_value = mock_stream(0)

        with client.stream("POST", "/chat/stream", json={"message": "搜巨人"}) as response:
            body = response.read().decode()
            assert "reasoning" in body
            assert "lookup" in body
            assert "search_bangumi_subject" in body

    @patch("main.agent_app.astream")
    def test_stream_handles_error(self, mock_astream):
        """流式异常也被捕获为 SSE 事件"""
        async def mock_stream(state):
            raise RuntimeError("模拟流式异常")
            yield  # noqa

        mock_astream.return_value = mock_stream(0)

        with client.stream("POST", "/chat/stream", json={"message": "测试"}) as response:
            body = response.read().decode()
            assert "error" in body or "[DONE]" in body
