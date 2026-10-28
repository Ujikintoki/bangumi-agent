"""本机网页、配置脱敏及基础模式边界。"""
import json
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage

from core.config import get_settings
from main import app, _tool_payload_kind
from agent.guardrails import format_tool_error
from tools.bgm_tools import get_agent_tools


def test_local_page_and_missing_configuration(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "LLM_API_KEY", "")
    monkeypatch.setattr(settings, "MEMORY_ENABLED", False)
    monkeypatch.setattr(settings, "RAG_ENABLED", False)
    with TestClient(app) as client:
        assert client.get('/').status_code == 200
        assert client.get('/web/chat.js').status_code == 200
        result = client.get('/api/readiness').json()
        assert result['missing'] == ['LLM_API_KEY']
        assert not result['ready']
    assert 'search_local_bangumi' not in {tool.name for tool in get_agent_tools()}


def test_readiness_never_exposes_key(monkeypatch):
    monkeypatch.setattr(get_settings(), 'LLM_API_KEY', 'synthetic-secret')
    response = TestClient(app).get('/api/readiness')
    assert response.json()['ready']
    assert 'synthetic-secret' not in response.text


def test_web_chat_uses_agent_and_returns_safe_failure():
    client = TestClient(app)
    result = {'messages': [HumanMessage(content='聊聊演出'), AIMessage(content='可以从节奏聊起。')], 'query_intent': 'discuss'}
    with (
        patch('main.agent_app.ainvoke', new=AsyncMock(return_value=result)) as invoke,
        patch('main.render_reply', new=AsyncMock(return_value='可以从节奏聊起。')),
    ):
        response = client.post('/chat', json={'message': '聊聊演出'})
        assert response.status_code == 200
        assert response.json()['reply'] == '可以从节奏聊起。'
        assert response.json()['session_id']
        invoke.assert_awaited_once()
    with patch('main.agent_app.ainvoke', new=AsyncMock(side_effect=RuntimeError('synthetic-secret'))):
        response = client.post('/chat', json={'message': '继续'})
        assert response.json()['turn_result']['status'] == 'failed'
        assert 'synthetic-secret' not in response.text


def test_tool_exception_is_failure_without_exception_body():
    result = format_tool_error(RuntimeError('synthetic-secret'))
    assert 'synthetic-secret' not in result
    assert json.loads(result)['_error'] == 'tool_execution_failed'
    assert _tool_payload_kind(result) == 'error'


def test_readiness_preserves_bearer_auth(monkeypatch):
    monkeypatch.setattr(get_settings(), 'AUTH_ENABLED', True)
    monkeypatch.setattr(get_settings(), 'API_USER_TOKENS', {'reader': 'x' * 32})
    client = TestClient(app)
    assert client.get('/api/readiness').status_code == 401
    assert client.get('/api/readiness', headers={'Authorization': 'Bearer ' + 'x' * 32}).status_code == 200


def test_local_search_requires_explicit_enable(monkeypatch):
    monkeypatch.setattr(get_settings(), 'RAG_ENABLED', True)
    assert 'search_local_bangumi' in {tool.name for tool in get_agent_tools()}
