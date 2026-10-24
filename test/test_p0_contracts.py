"""P0 behavior regressions: no real model, database, or network."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage

import main
from agent.memory.cache import SessionCache
from core.auth import validate_auth_configuration
from core.config import get_settings


def headers(user="alice"):
    return {"Authorization": "Bearer " + ("a" if user == "alice" else "b") * 40}


@pytest.fixture
def secured(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "AUTH_ENABLED", True)
    monkeypatch.setattr(settings, "API_USER_TOKENS", {"alice": "a" * 40, "bob": "b" * 40})
    monkeypatch.setattr("agent.memory.cache._cache_instance", SessionCache())
    monkeypatch.setattr(main, "_remember_session", AsyncMock())
    seen = []

    async def graph(state, **kwargs):
        seen.append(state)
        return {**state, "query_intent": "fetch", "messages": [*state["messages"], AIMessage(content="done")]}

    monkeypatch.setattr(main.agent_app, "ainvoke", graph)
    return seen


@pytest.mark.parametrize("endpoint", ["/chat", "/chat/stream"])
@pytest.mark.parametrize("credential", [None, "Bearer wrong", "Basic abc"])
def test_requires_credentials(secured, endpoint, credential):
    response = TestClient(main.app).post(endpoint, json={"message": "hello"},
                                        headers={} if credential is None else {"Authorization": credential})
    assert response.status_code == 401
    assert not secured


@pytest.mark.parametrize("endpoint", ["/chat", "/chat/stream"])
def test_cannot_claim_another_user(secured, endpoint):
    r = TestClient(main.app).post(endpoint, headers=headers(), json={"message": "hello", "user_id": "bob"})
    assert r.status_code == 403
    assert not secured


def test_unauthenticated_mode_rejects_named_identity(secured, monkeypatch):
    monkeypatch.setattr(get_settings(), "AUTH_ENABLED", False)
    client = TestClient(main.app)
    assert client.post("/chat", json={"message": "hello", "user_id": "alice"}).status_code == 403
    assert client.post("/chat", json={"message": "hello"}).status_code == 200
    assert secured[-1]["user_id"] == "anonymous"


@pytest.mark.parametrize("endpoint", ["/chat", "/chat/stream"])
def test_session_resumes_and_is_user_scoped(secured, endpoint):
    client = TestClient(main.app)
    first = client.post(endpoint, headers=headers(), json={"message": "alice-only-fact"})
    assert first.status_code == 200
    data = first.json() if endpoint == "/chat" else json.loads(first.text.splitlines()[0][6:])
    sid = data["session_id"]
    assert len(sid) == 32
    client.post("/chat", headers=headers(), json={"message": "continue", "session_id": sid})
    assert secured[-1]["user_id"] == "alice"
    assert any(m.content == "alice-only-fact" for m in secured[-1]["messages"])
    client.post("/chat", headers=headers("bob"), json={"message": "hello", "session_id": sid})
    assert secured[-1]["user_id"] == "bob"
    assert not any(m.content == "alice-only-fact" for m in secured[-1]["messages"])


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["/chat", "/chat/stream"])
async def test_concurrent_request_rejected_and_release_after_cancel(secured, monkeypatch, endpoint):
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(state, **kwargs):
        entered.set()
        await release.wait()
        return {**state, "query_intent": "fetch", "messages": [*state["messages"], AIMessage(content="done")]}

    monkeypatch.setattr(main.agent_app, "ainvoke", slow)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        payload = {"message": "hello", "session_id": "shared"}
        first = asyncio.create_task(client.post(endpoint, headers=headers(), json=payload))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            for other in ("/chat", "/chat/stream"):
                assert (await client.post(other, headers=headers(), json=payload)).status_code == 409
        finally:
            first.cancel()
            try:
                await first
            except asyncio.CancelledError:
                pass
            release.set()
        assert (await client.post(endpoint, headers=headers(), json=payload)).status_code == 200


@pytest.mark.parametrize("endpoint", ["/chat", "/chat/stream"])
@pytest.mark.parametrize("error", [TimeoutError, RuntimeError])
def test_failed_turn_returns_session_and_releases_claim(secured, monkeypatch, endpoint, error):
    monkeypatch.setattr(main.agent_app, "ainvoke", AsyncMock(side_effect=error("secret-marker")))
    client = TestClient(main.app)
    for _ in range(2):
        response = client.post(endpoint, headers=headers(), json={"message": "hello", "session_id": "same"})
        assert response.status_code == 200
        assert "secret-marker" not in response.text
        data = response.json() if endpoint == "/chat" else json.loads(response.text.splitlines()[0][6:])
        assert data["session_id"] == "same"


@pytest.mark.parametrize("tokens", [{}, {"anonymous": "a" * 40}, {"alice": "short"},
                                       {"alice": "a" * 40, "bob": "a" * 40}])
def test_invalid_auth_fails_closed(secured, monkeypatch, tokens):
    monkeypatch.setattr(get_settings(), "API_USER_TOKENS", tokens)
    with pytest.raises(RuntimeError):
        validate_auth_configuration()
    assert TestClient(main.app).post("/chat", json={"message": "hello"}).status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize("first", [True, False])
async def test_pipeline_memory_reaches_model(monkeypatch, first):
    from agent.nodes import pipeline
    model = MagicMock()
    model.ainvoke = AsyncMock(return_value=AIMessage(content="done"))
    recall = AsyncMock(return_value="prefers SF")
    monkeypatch.setattr(pipeline, "create_llm", lambda **kw: model)
    monkeypatch.setattr(pipeline, "get_agent_tools", list)
    monkeypatch.setattr(pipeline, "recall_memory_step", recall)
    await pipeline._pipeline_step({"messages": [HumanMessage(content="recommend")],
                                   "_memory_context": None if first else "prefers SF"},
                                  [], "auto", "base prompt", is_first_step=first)
    assert "prefers SF" in model.ainvoke.call_args.args[0][0].content
    assert recall.await_count == int(first)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["pipeline", "reasoning"])
async def test_provider_errors_not_exposed(monkeypatch, caplog, kind):
    from agent.nodes import pipeline, reasoning
    module = pipeline if kind == "pipeline" else reasoning
    model = MagicMock()
    model.bind_tools.return_value = model
    model.ainvoke = AsyncMock(side_effect=RuntimeError("secret-marker"))
    monkeypatch.setattr(module, "create_llm", lambda **kw: model)
    monkeypatch.setattr(module, "get_agent_tools", list)
    monkeypatch.setattr(module, "recall_memory_step", AsyncMock(return_value=""))
    state = {"messages": [HumanMessage(content="hello")], "user_id": "anonymous"}
    result = await (pipeline._pipeline_step(state, [], "auto", "base") if kind == "pipeline"
                    else reasoning.reasoning_node(state))
    assert result["messages"] == []
    assert result["turn_result"]["status"] == "failed"
    assert result["turn_result"]["error_type"] == "RuntimeError"
    assert "secret-marker" not in caplog.text
