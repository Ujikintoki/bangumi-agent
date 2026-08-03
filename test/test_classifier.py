"""
意图分类器测试 — v4: function calling 结构化输出

覆盖 classify_intent_llm、classify_intent、route_by_classification 入口。
可独立运行: python -m pytest test/test_classifier.py -v
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from langchain_openai import ChatOpenAI

from agent.orchestrate.classifier import (
    _VALID_INTENTS,
    classify_intent,
    classify_intent_llm,
    route_by_classification,
)
from test.conftest import make_mock_llm

pytestmark = pytest.mark.asyncio


def _make_tc(intent: str, confidence: float = 0.9) -> list[dict]:
    """构造 mock tool_calls，模拟 LLM function calling 返回。"""
    return [{
        "name": "classify_intent",
        "args": {"intent": intent, "confidence": confidence},
        "id": "call_test",
        "type": "tool_call",
    }]


class TestIntentClassifierLLM:
    """LLM 分类核心函数 — v4: function calling"""

    async def test_returns_intent_and_confidence(self):
        """正常返回 (intent, confidence) 元组"""
        intent, conf = await classify_intent_llm(
            "推荐类似巨人的番", make_mock_llm(tool_calls=_make_tc("explore", 0.95))
        )
        assert intent == "explore"
        assert conf == 0.95

    async def test_falls_back_on_invalid_intent(self):
        """无效 intent → fallback"""
        intent, conf = await classify_intent_llm(
            "query", make_mock_llm(tool_calls=_make_tc("invalid_xyz", 0.5))
        )
        assert intent == "fallback"
        assert conf == 0.0

    async def test_falls_back_on_error(self):
        """LLM 异常 → fallback"""
        mock = MagicMock(spec=ChatOpenAI)
        mock.bind_tools.return_value = mock
        mock.ainvoke.side_effect = RuntimeError("API error")
        intent, conf = await classify_intent_llm("query", mock)
        assert intent == "fallback"
        assert conf == 0.0

    async def test_no_tool_calls_falls_back(self):
        """无 tool_calls → fallback"""
        mock = make_mock_llm(content="some text", tool_calls=[])
        intent, conf = await classify_intent_llm("query", mock)
        assert intent == "fallback"
        assert conf == 0.0

    async def test_alias_resolution(self):
        """旧 intent 名 → v4 intent 名"""
        # "chitchat" → "chat"
        intent, conf = await classify_intent_llm(
            "你好", make_mock_llm(tool_calls=_make_tc("chitchat", 0.9))
        )
        assert intent == "chat"

    async def test_clamps_confidence(self):
        """confidence 钳制到 [0, 1]"""
        intent, conf = await classify_intent_llm(
            "query", make_mock_llm(tool_calls=_make_tc("fetch", 1.5))
        )
        assert intent == "fetch"
        assert conf == 1.0

        intent, conf = await classify_intent_llm(
            "query", make_mock_llm(tool_calls=_make_tc("fetch", -0.5))
        )
        assert conf == 0.0

    async def test_short_anime_title(self):
        """短作品名 — LLM 应判 fetch 或 fallback，非 chat"""
        intent, _ = await classify_intent_llm(
            "EVA", make_mock_llm(tool_calls=_make_tc("fetch", 0.7))
        )
        assert intent != "chat"


class TestClassifyIntent:
    """入口函数 — v4"""

    async def test_returns_intent_and_confidence(self):
        mock = make_mock_llm(tool_calls=_make_tc("explore", 0.95))
        intent, conf = await classify_intent("推荐几部好看的番", mock)
        assert intent == "explore"
        assert conf == 0.95

    async def test_returns_fallback_when_no_llm(self):
        intent, conf = await classify_intent("花开伊吕波和taritari哪个更感人", None)
        assert intent == "fallback"
        assert conf == 0.0

    async def test_empty_message(self):
        intent, conf = await classify_intent("", None)
        assert intent == "fallback"
        assert conf == 0.0

    async def test_llm_classifies_chat(self):
        mock = make_mock_llm(tool_calls=_make_tc("chat", 0.9))
        intent, conf = await classify_intent("你好", mock)
        assert intent == "chat"
        assert conf == 0.9


class TestRouteByClassification:
    """置信度路由"""

    def test_high_confidence_direct(self):
        assert route_by_classification("explore", 0.9) == "explore"
        assert route_by_classification("chat", 0.85) == "chat"
        assert route_by_classification("discuss", 0.8) == "discuss"

    def test_medium_confidence_chat_downgrades(self):
        """chat 中置信度 → 有实体时降级到 fetch"""
        assert route_by_classification("chat", 0.6, has_entities=True) == "fetch"
        assert route_by_classification("chat", 0.6, has_entities=False) == "fallback"

    def test_medium_confidence_discuss_downgrades(self):
        """discuss 中置信度 → explore"""
        assert route_by_classification("discuss", 0.6) == "explore"

    def test_medium_confidence_others_keep(self):
        """fetch/explore/realtime 中置信度保持"""
        assert route_by_classification("fetch", 0.6) == "fetch"
        assert route_by_classification("explore", 0.7) == "explore"
        assert route_by_classification("realtime", 0.55) == "realtime"

    def test_low_confidence_all_fallback(self):
        assert route_by_classification("explore", 0.3) == "fallback"
        assert route_by_classification("chat", 0.2) == "fallback"
        assert route_by_classification("discuss", 0.49) == "fallback"

    def test_profile_downgrades(self):
        """profile 中置信度 → fallback"""
        assert route_by_classification("profile", 0.6) == "fallback"


class TestValidIntents:
    """_VALID_INTENTS 完整性 — v4: 7 intent"""

    def test_all_expected_intents_present(self):
        expected = {"chat", "fetch", "explore", "discuss", "profile", "realtime", "fallback"}
        assert _VALID_INTENTS == expected
