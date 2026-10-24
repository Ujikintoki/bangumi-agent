"""本轮失败、历史和结构化工具交接的行为回归。"""
import json
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import main
from agent.nodes import pipeline
from agent.routing.routes import route_after_reasoning


@pytest.mark.asyncio
async def test_pipeline_failure_does_not_become_render_facts(monkeypatch):
    model = AsyncMock()
    model.ainvoke.side_effect = RuntimeError("secret-provider-body")
    monkeypatch.setattr(pipeline, "create_llm", lambda **kw: model)
    monkeypatch.setattr(pipeline, "get_agent_tools", list)
    result = await pipeline._pipeline_step(
        {"messages": [HumanMessage(content="查评分")], "iterations": 1},
        [], "auto", "base",
    )
    assert result["messages"] == []
    assert result["turn_result"]["status"] == "failed"
    assert "secret-provider-body" not in str(result)


def test_failure_route_never_replays_previous_tool_call():
    state = {
        "messages": [AIMessage(content="", tool_calls=[{
            "name": "search", "args": {}, "id": "old-call",
        }])],
        "turn_result": {"status": "failed"},
    }
    assert route_after_reasoning(state) == "__end__"


@pytest.mark.asyncio
async def test_chat_render_receives_history_and_user_correction(monkeypatch):
    render = AsyncMock(return_value="明白，你说的是演出。")
    monkeypatch.setattr(main, "render_reply", render)
    messages = [
        HumanMessage(content="我觉得边缘行者第二季的演出很散"),
        AIMessage(content="你不喜欢剧情？"),
        HumanMessage(content="不是剧情，是演出"),
    ]
    out, reply = await main._render_final_reply(
        messages, "不是剧情，是演出", "chat", "bangumi", "fast",
    )
    context = render.call_args.kwargs["conversation_context"]
    assert context[0]["content"] == messages[0].content
    assert context[1]["role"] == "assistant"
    assert messages == out[:-1]
    assert out[-1].content == reply


def test_ai_text_with_tool_calls_is_never_overwritten():
    call = AIMessage(content="让我查查", tool_calls=[{
        "name": "search", "args": {}, "id": "c1",
    }])
    messages = [HumanMessage(content="查一下"), call,
                ToolMessage(content='{"_error":"failed"}', tool_call_id="c1")]
    out = main._replace_last_ai_content(messages, "这次没查到。")
    assert out[:-1] == messages
    assert out[-1].content == "这次没查到。"


def test_empty_opinion_metadata_cannot_count_as_data():
    payload = {
        "comments": {"items": [], "sample_limit": 8, "comment_count": 0},
        "reviews": {"items": [], "total": 0},
        "coverage": {"board": "not_covered"},
    }
    assert main._tool_payload_kind(json.dumps(payload)) == "empty"


def test_opinion_budget_preserves_valid_json_complete_quote_and_missing_scope():
    from agent.memory.short_term import _truncate_message_content, count_tokens

    first_quote = "可核验的第一条完整短评，结尾也必须在。"
    payload = {
        "subject_id": 1,
        "comments": {"items": [{
            "id": i, "author": f"班友{i}", "content": first_quote if i == 0 else "长评" * 200,
            "url": f"https://bgm.tv/subject/1/comments#{i}",
        } for i in range(12)], "sampled_count": 12},
        "reviews": {"items": [{"summary": "材料" * 400}]},
        "coverage": {"board": "not_covered"},
    }
    msg = ToolMessage(content=json.dumps(payload, ensure_ascii=False),
                      name="get_subject_opinions", tool_call_id="opinions")
    out = _truncate_message_content(msg, 600)
    data = json.loads(out.content)
    assert count_tokens(out.content) <= 600
    assert data["comments"]["items"][0]["content"] == first_quote
    assert data["comments"]["context_omitted_count"] > 0
    assert data["coverage"]["board"] == "not_covered"
    assert out.tool_call_id == "opinions"
    assert "context_truncated" not in payload


def test_same_author_cannot_attach_the_wrong_comment():
    result = {"sources": [{"comments_sample": {
        "sampled": 2, "total": 2, "items": [
            {"id": 1, "author": "同名", "url": "https://bgm.tv/subject/1/comments#1"},
            {"id": 2, "author": "同名", "url": "https://bgm.tv/subject/1/comments#2"},
        ],
    }}]}
    reply = main._append_community_provenance("同名这条很有意思。[comment:2]", result)
    assert "comments#2" in reply
    assert "comments#1" not in reply
    assert "[comment:2]" not in reply
    assert main._append_community_provenance("同名这条很有意思。", result) == "同名这条很有意思。"


def test_repeated_read_after_user_correction_is_not_a_loop():
    from agent.guardrails import check_duplicate_tool_calls
    from agent.routing.routes import route_after_tool_react

    def call(identifier):
        return AIMessage(content="", tool_calls=[{
            "name": "get_subject_opinions", "args": {"subject_id": 1}, "id": identifier,
        }])

    messages = [
        HumanMessage(content="读评论"), call("old"),
        ToolMessage(content='{"items":[{"id":1}]}', tool_call_id="old"),
        AIMessage(content="这条在批评剧情。"),
        HumanMessage(content="我说的是表达手法"), call("new"),
        ToolMessage(content='{"items":[{"id":1}]}', tool_call_id="new"),
    ]
    assert check_duplicate_tool_calls(messages) == ""
    assert route_after_tool_react({"messages": messages, "iterations": 1,
                                   "query_intent": "discuss", "depth": "fast"}) == "reasoning_node"


@pytest.mark.asyncio
async def test_successful_chat_is_not_reported_as_empty(monkeypatch):
    monkeypatch.setattr(main, "render_reply", AsyncMock(return_value="明白，继续说演出。"))
    messages = [HumanMessage(content="继续说")]
    result = main._build_turn_result(messages)
    await main._render_final_reply(messages, "继续说", "chat", "bangumi", "fast", result)
    assert result["status"] == "success"
    assert result["task_status"] == "unverified"


def test_unknown_citation_cannot_leave_an_unattributed_claim():
    result = {"status": "success", "sources": []}
    reply = main._append_community_provenance("某人说了未经核验的引文[comment:999]", result)
    assert "未经核验的引文" not in reply
    assert result["status"] == "partial"
    assert "unverified_citation" in result["missing"]


def test_known_citation_without_url_is_still_unverified():
    result = {"sources": [{"comments_sample": {"items": [{"id": 2, "author": "班友"}]}}]}
    reply = main._append_community_provenance("未经核验的引文[comment:2]", result)
    assert "未经核验的引文" not in reply
    assert "unverified_citation" in result["missing"]


def test_render_also_budgets_community_material_without_mutating_originals():
    from agent.memory.short_term import count_tokens

    result = {"status": "success", "sources": [{
        "comments_sample": {"sampled": 15, "total": 15, "items": [
            {"id": i + 1, "author": str(i), "excerpt": "原文材料" * 2000,
             "url": f"https://bgm.tv/subject/1/comments#{i + 1}",
             "source_id": f"comment:{i + 1}"} for i in range(15)
        ]}, "coverage": {"board": "not_covered"},
    }]}
    rendered_input = main._render_input_for_turn("摘要", result)
    assert count_tokens(rendered_input) < 2000
    assert "context_omitted_count" in rendered_input
    assert len(result["sources"][0]["comments_sample"]["items"]) == 15
    reply = main._append_community_provenance("引用[comment:1]", result)
    assert "本次获取 comments 15 条" in reply
    assert "本次整理保留 1 条" in reply
    assert "原文不完整" in reply


@pytest.mark.asyncio
async def test_blog_preserves_original_and_budget_discloses_excerpt(monkeypatch):
    from agent.memory.short_term import _truncate_message_content, count_tokens
    from clients.client import BangumiClient
    from schemas.tools_input import GetBlogInput

    original = "完整长文内容" * 2000
    async with BangumiClient() as client:
        monkeypatch.setattr(client, "_get", AsyncMock(return_value={
            "id": 7, "content": original, "user": {"nickname": "作者"},
        }))
        result = await client.get_blog(GetBlogInput(entry_id=7, include_comments=False, include_subjects=False))
    assert result["blog"]["content"] == original
    assert result["blog"]["truncated"] is False
    msg = ToolMessage(content=json.dumps(result, ensure_ascii=False), name="get_blog", tool_call_id="blog")
    out = _truncate_message_content(msg, 700)
    payload = json.loads(out.content)
    assert count_tokens(out.content) <= 700
    assert payload["blog"]["content_kind"] == "excerpt"
    assert payload["blog"]["truncated"] is True
    assert payload["blog"]["url"] == "https://bgm.tv/blog/7"


@pytest.mark.asyncio
async def test_render_failure_still_resolves_citation_marker(monkeypatch):
    monkeypatch.setattr(main, "render_reply", AsyncMock(return_value=None))
    messages = [HumanMessage(content="说说这条评论"),
                AIMessage(content="这条评论很有趣。[comment:2]")]
    result = {"status": "success", "sources": [{"comments_sample": {
        "sampled": 1, "total": 1,
        "items": [{"id": 2, "author": "班友", "url": "https://bgm.tv/subject/1/comments#2"}],
    }}]}
    _, reply = await main._render_final_reply(messages, "说说这条评论", "discuss", "neutral", "fast", result)
    assert "[comment:2]" not in reply
    assert "[班友 的原评](https://bgm.tv/subject/1/comments#2)" in reply
    assert result["task_status"] == "incomplete"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/chat", "/chat/stream"])
async def test_http_execution_failure_has_same_contract_and_no_provider_body(monkeypatch, caplog, path):
    import httpx

    monkeypatch.setattr(main.agent_app, "ainvoke", AsyncMock(side_effect=RuntimeError("private-provider-detail")))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        response = await client.post(path, json={"message": "查评分"})
    assert response.status_code == 200
    if path == "/chat":
        result = response.json()["turn_result"]
    else:
        event = next(line[6:] for line in response.text.splitlines()
                     if line.startswith("data: {") and '"node": "error"' in line)
        result = json.loads(event)["turn_result"]
    assert result == {"status": "failed", "task_status": "incomplete", "missing": ["execution"], "sources": []}
    assert "private-provider-detail" not in response.text
    assert "private-provider-detail" not in caplog.text
