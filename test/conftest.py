"""Phase 1-2 共享 Fixtures。

提供 mock HTTP 响应、mock 数据工厂等可复用测试工具。
所有 fixture 不依赖外部服务（无网络、无数据库）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


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
    """创建模拟的 httpx.AsyncClient，按 path 返回预设响应。

    Args:
        response_map: path → response_data 的映射。
            path 如 ``"/p1/subjects/8"``。

    Returns:
        配置好的 AsyncMock 实例，用作 ``BangumiClient._client``。
    """
    mock = AsyncMock()

    async def request(method: str, path: str, **kwargs):
        if path in response_map:
            data = response_map[path]
            resp = mock_response(data)
            return resp
        # 默认 404
        resp = MagicMock()
        resp.status_code = 404
        resp.content = True
        resp.json.return_value = {"error": "not found"}
        import httpx

        raise httpx.HTTPStatusError(
            "404", request=MagicMock(), response=resp
        )

    mock.request = request
    return mock


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
        "reactions": [{"users": [1, 2, 3]}],  # 3 reactions
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
                    "relation": 0,  # CV
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
            "subject": {
                "id": 1,
                "name": "Anime A",
                "nameCN": "",
                "rating": {"score": 7.5, "total": 3000},
            },
            "watchers": 5000,
        },
        {
            "subject": {
                "id": 2,
                "name": "Anime B",
                "nameCN": "",
                "rating": {"score": 6.0, "total": 1000},
            },
            "watchers": 2000,
        },
    ]


@pytest.fixture
def trending_response() -> dict:
    """Trending 响应。"""
    return {
        "data": [
            {
                "subject": {
                    "id": 1,
                    "name": "Hot Anime",
                    "nameCN": "",
                    "type": 2,
                    "rating": {"score": 8.0},
                },
                "count": 500,
            }
        ],
        "total": 1,
    }


@pytest.fixture
def user_collections_data() -> list[dict]:
    """用户收藏数据（30 条，测试 display cap）。"""
    return [
        {
            "subject": {
                "id": i,
                "name": f"Item {i}",
                "nameCN": "",
                "type": 2,
                "rating": {"score": 7.0},
            },
            "type": 2,
            "rate": 7,
        }
        for i in range(1, 31)
    ]
