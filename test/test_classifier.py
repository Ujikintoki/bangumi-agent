"""
意图分类器测试 — LLM 单阶段分类

覆盖 classify_intent_llm、classify_intent 入口。
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
)
from test.conftest import make_mock_llm

pytestmark = pytest.mark.asyncio


class TestIntentClassifierLLM:
    """LLM 分类核心函数"""

    async def test_returns_valid_intent(self):
        assert await classify_intent_llm("推荐类似巨人的番", make_mock_llm(content="discovery")) == "discovery"

    async def test_falls_back_to_unknown_on_invalid_output(self):
        assert await classify_intent_llm("query", make_mock_llm(content="invalid_xyz")) == "unknown"

    async def test_falls_back_to_unknown_on_error(self):
        mock = MagicMock(spec=ChatOpenAI)
        mock.ainvoke.side_effect = RuntimeError("API error")
        assert await classify_intent_llm("query", mock) == "unknown"

    async def test_extracts_first_word_only(self):
        assert await classify_intent_llm("找巨人", make_mock_llm(content="lookup  \n extra")) == "lookup"

    async def test_short_anime_title_handled_by_prompt(self):
        """短作品名（"EVA"）——LLM 应判 unknown 或 lookup，而非 chitchat"""
        result = await classify_intent_llm("EVA", make_mock_llm(content="unknown"))
        assert result in ("unknown", "lookup")

    async def test_compound_query_not_misclassified_as_chitchat(self):
        """混合查询（寒暄+数据）——LLM 应判为数据查询意图"""
        result = await classify_intent_llm(
            "你好，EVA评分怎么样？", make_mock_llm(content="lookup")
        )
        assert result != "chitchat"


class TestClassifyIntent:
    """入口函数 — LLM 单阶段"""

    async def test_llm_classifies_discovery(self):
        mock = make_mock_llm(content="discovery")
        intent, method = await classify_intent("推荐几部好看的番", mock)
        assert intent == "discovery"
        assert method == "llm"

    async def test_llm_classifies_lookup(self):
        mock = make_mock_llm(content="lookup")
        intent, method = await classify_intent("这个番和那个番比怎么样", mock)
        assert intent == "lookup"
        assert method == "llm"

    async def test_returns_unknown_when_no_llm(self):
        intent, method = await classify_intent("花开伊吕波和taritari哪个更感人", None)
        assert intent == "unknown"
        assert method == "llm"

    async def test_empty_message(self):
        intent, method = await classify_intent("", None)
        assert intent == "chitchat"
        assert method == "llm"

    async def test_chitchat_classified_by_llm(self):
        """纯寒暄走 LLM 分类，method 为 llm"""
        mock = make_mock_llm(content="chitchat")
        intent, method = await classify_intent("你好", mock)
        assert intent == "chitchat"
        assert method == "llm"


class TestValidIntents:
    """_VALID_INTENTS 完整性"""

    def test_all_expected_intents_present(self):
        expected = {"chitchat", "factual", "lookup", "discovery", "realtime", "debate", "emotional", "unknown"}
        assert _VALID_INTENTS == expected
