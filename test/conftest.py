"""Phase 1-3 共享 Fixtures 和测试工具。

提供 mock HTTP 响应、mock 数据工厂、mock LLM、mock 工具
等可复用测试工具。所有 fixture 不依赖外部服务（无网络、无数据库）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import tool as langchain_tool
from langchain_openai import ChatOpenAI

from agent.state import AgentState


# ═══════════════════════════════════════════════════════════════════
# 全局测试环境
# ═══════════════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def _disable_dev_mode_for_tests():
    """强制 DEV_MODE=False，确保 test mock ``agent_app.ainvoke`` 不被 astream 路径绕过。

    DEV_MODE=true 时 main.py 走 ``_run_with_telemetry`` → ``astream()`` 路径，
    而测试 mock 的是 ``agent_app.ainvoke``。此 fixture 在每次测试前将 DEV_MODE 置为 False，
    测试结束后恢复原值。get_settings() 是 @lru_cache 单例，修改对 main.py 可见。
    """
    from core.config import get_settings

    settings = get_settings()
    original = settings.DEV_MODE
    settings.DEV_MODE = False
    yield
    settings.DEV_MODE = original


@pytest.fixture(autouse=True)
def _disable_rate_limit_for_tests():
    """测试环境关闭限流，避免大量 /chat 请求命中 429。

    与 _disable_dev_mode_for_tests 同理：直接修改 get_settings() 缓存的单例。
    middleware.py 每次请求读取 RATE_LIMIT_PER_MINUTE，置 0 即完全放行。
    """
    from core.config import get_settings

    settings = get_settings()
    original = settings.RATE_LIMIT_PER_MINUTE
    settings.RATE_LIMIT_PER_MINUTE = 0
    yield
    settings.RATE_LIMIT_PER_MINUTE = original


@pytest.fixture(autouse=True)
def _no_request_side_effects():
    """请求后处理阶段不得触发真实外部调用（Render + L2 记忆写入）。

    ``/chat`` 在 graph 之外还有两处副作用，只 mock ``agent_app.ainvoke``
    拦不住：

    1. ``main.render_reply`` —— 一次付费 LLM 调用，且会改写测试断言依赖
       的字面量，用例随网络红绿翻转（实测：同一份代码连跑 5 次，3 红 2 绿）。
    2. ``MemoryManager.remember_session`` —— 一次摘要 LLM + 一次 embedding
       API + 一次 ``session_memories`` 写入。测试既不该花钱，也不该往真实
       记忆表灌数据。

    两者统一降级为无副作用：render 原样返回（等价于它自身的降级路径），
    记忆写入整体跳过。直接调用 ``agent.persona.render.render_reply`` 或
    ``MemoryManager`` 其余方法的单元测试不受影响。
    """
    from agent.memory.long_term import MemoryManager

    passthrough = AsyncMock(side_effect=lambda render_input, **kwargs: render_input)
    with (
        patch("main.render_reply", passthrough),
        patch.object(MemoryManager, "remember_session", AsyncMock()),
    ):
        yield


# ═══════════════════════════════════════════════════════════════════
# Mock HTTP 响应工厂
# ═══════════════════════════════════════════════════════════════════


def mock_response(json_data: dict | list, status_code: int = 200) -> MagicMock:
    """创建模拟的 httpx Response 对象。"""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.content = True
    return resp


def mock_httpx_client(response_map: dict[str, dict | list]) -> AsyncMock:
    """创建模拟的 httpx.AsyncClient，按 path 返回预设响应。"""
    mock = AsyncMock()

    async def request(method: str, path: str, **kwargs):
        if path in response_map:
            data = response_map[path]
            resp = mock_response(data)
            return resp
        resp = MagicMock()
        resp.status_code = 404
        resp.content = True
        resp.json.return_value = {"error": "not found"}
        import httpx

        raise httpx.HTTPStatusError("404", request=MagicMock(), response=resp)

    mock.request = request
    return mock


# ═══════════════════════════════════════════════════════════════════
# Agent Phase 3 共享测试工厂
# ═══════════════════════════════════════════════════════════════════


def make_state(**overrides) -> AgentState:
    """构造 AgentState，所有字段带有合理默认值。"""
    defaults: dict = {
        "messages": [SystemMessage(content="You are Bangumi assistant."), HumanMessage(content="你好")],
        "iterations": 0,
        "query_intent": "unknown",
        "session_id": "test-session",
        "user_id": "test-user",
        "error_flag": False,
        "_memory_context": None,
        "output_style": "neutral",
        "depth": "fast",
    }
    defaults.update(overrides)
    return defaults  # type: ignore[return-value]


def make_mock_llm(content: str = "Mock response", tool_calls: list[dict] | None = None):
    """创建 mock ChatOpenAI，invoke() / ainvoke() 返回预设 AIMessage。"""
    mock = MagicMock(spec=ChatOpenAI)
    aimessage = AIMessage(content=content, tool_calls=tool_calls or [])
    mock.invoke.return_value = aimessage
    mock.ainvoke.return_value = aimessage
    mock.bind_tools.return_value = mock
    return mock


# ═══════════════════════════════════════════════════════════════════
# Mock 工具（ToolNode 测试用）
# ═══════════════════════════════════════════════════════════════════


@langchain_tool
def mock_search_tool(keyword: str) -> str:
    """搜索 Bangumi 条目。"""
    return f"搜索 '{keyword}' 的结果: 找到 3 个匹配条目"


@langchain_tool
def mock_detail_tool(subject_id: int) -> str:
    """获取条目详情。"""
    return f"条目 #{subject_id} 详情: 评分 8.5, 排名 #42"


@langchain_tool
def mock_failing_tool(query: str) -> str:
    """会抛出异常的工具。"""
    raise RuntimeError("模拟工具执行失败")


MOCK_TOOLS = [mock_search_tool, mock_detail_tool]


# ═══════════════════════════════════════════════════════════════════
# Mock 数据工厂
# ═══════════════════════════════════════════════════════════════════


@pytest.fixture
def slim_subject() -> dict:
    """标准 SlimSubject 数据。"""
    return {
        "id": 8,
        "name": "コードギアス 反逆のルルーシュR2",
        "nameCN": "",
        "type": 2,
        "rating": {"score": 8.19, "rank": 42, "total": 9438},
        "images": {
            "common": "https://lain.bgm.tv/pic/cover/c/8.jpg",
            "large": "https://lain.bgm.tv/pic/cover/l/8.jpg",
            "medium": "https://lain.bgm.tv/pic/cover/m/8.jpg",
            "small": "https://lain.bgm.tv/pic/cover/s/8.jpg",
        },
        "nsfw": False,
    }


@pytest.fixture
def full_subject(slim_subject: dict) -> dict:
    """完整 Subject 数据。"""
    return {
        **slim_subject,
        "summary": "东京决战一年后...",
        "eps": 25,
        "platform": {"type": "TV", "typeCN": "TV"},
        "airtime": {"date": "2008-04-06", "year": 2008, "month": 4, "weekday": 7},
        "tags": [
            {"name": "科幻", "count": 8500},
            {"name": "原创", "count": 6000},
        ],
        "nsfw": False,
    }


@pytest.fixture
def sample_comment() -> dict:
    """标准 Comment 数据。"""
    return {
        "id": 1,
        "content": "这是一条测试评论",
        "reactions": [{"users": [1, 2, 3]}],
        "replies": 5,
        "createdAt": 1700000000,
    }


@pytest.fixture
def sample_subject_comment() -> dict:
    """Subject 评论（含 rate）。"""
    return {
        "id": 1,
        "comment": "确实可称之为神作",
        "rate": 9,
        "reactions": [{"users": [1, 2]}],
        "replies": 0,
    }


@pytest.fixture
def sample_character_data() -> dict:
    """Character API 响应数据。"""
    return {
        "id": 1,
        "name": "ルルーシュ",
        "nameCN": "鲁路修",
        "role": 1,
        "summary": "作品主角",
        "collects": 5000,
        "nsfw": False,
    }


@pytest.fixture
def sample_person_data() -> dict:
    """Person API 响应数据。"""
    return {
        "id": 100,
        "name": "福山潤",
        "nameCN": "福山润",
        "career": ["seiyu", "actor"],
        "type": 1,
        "collects": 8500,
    }


@pytest.fixture
def subject_characters_response() -> list[dict]:
    """GET /p1/subjects/8/characters 的 mock 响应。"""
    return [
        {
            "character": {"id": 1, "name": "ルルーシュ", "nameCN": "鲁路修", "role": 1},
            "casts": [
                {
                    "person": {"id": 100, "name": "福山潤", "nameCN": "福山润"},
                    "relation": 0,
                    "summary": "",
                }
            ],
            "type": 0,
            "order": 0,
        }
    ]


@pytest.fixture
def episode_response() -> dict:
    """Episode 详情响应。"""
    return {
        "id": 1023497,
        "name": "梨花の決断",
        "nameCN": "",
        "sort": 1,
        "airdate": "2024-01-01",
        "duration": "24m",
        "desc": "测试单集描述",
        "comment": 5,
        "subjectID": 8,
        "type": 0,
        "subject": {"id": 8, "name": "Test Subject", "nameCN": "", "type": 2},
    }


@pytest.fixture
def calendar_items() -> list[dict]:
    """CalendarItem 列表。"""
    return [
        {
            "subject": {"id": 1, "name": "Anime A", "nameCN": "", "rating": {"score": 7.5, "total": 3000}},
            "watchers": 5000,
        },
        {
            "subject": {"id": 2, "name": "Anime B", "nameCN": "", "rating": {"score": 6.0, "total": 1000}},
            "watchers": 2000,
        },
    ]


@pytest.fixture
def trending_response() -> dict:
    """Trending 响应。"""
    return {
        "data": [
            {
                "subject": {"id": 1, "name": "Hot Anime", "nameCN": "", "type": 2, "rating": {"score": 8.0}},
                "count": 500,
            }
        ],
        "total": 1,
    }


@pytest.fixture
def user_collections_data() -> list[dict]:
    """用户收藏数据（30 条，测试 display cap + 新字段）。"""
    return [
        {
            "subject": {
                "id": i, "name": f"Item {i}", "nameCN": "", "type": 2,
                "rating": {"score": 7.0},
                "info": f"2024-{i%12+1:02d}-01 / 作者{i}" if i % 3 == 0 else "",
                "metaTags": ["日本", "动画"] if i % 5 == 0 else [],
            },
            "interest": {
                "type": 2,
                "rate": 7,
                "comment": f"短评{i}" if i % 4 == 0 else "",
                "tags": [],
                "updatedAt": 1700000000 + i * 86400,
            },
        }
        for i in range(1, 31)
    ]
