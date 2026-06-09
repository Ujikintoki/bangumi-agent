"""
意图分类器测试

覆盖规则层（优先级队列）、LLM fallback、两阶段入口 classify_intent。
可独立运行: python -m pytest test/test_classifier.py -v
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from langchain_openai import ChatOpenAI

from agent.classifier import (
    INTENT_RULES,
    _VALID_INTENTS,
    classify_intent,
    classify_intent_llm,
    classify_intent_rule,
)
from test.conftest import make_mock_llm

pytestmark = pytest.mark.asyncio


class TestIntentClassifierRule:
    """规则层 — 优先级队列验证"""

    @pytest.mark.parametrize("message,expected", [
        ("你好", "chitchat"), ("谢谢你的帮助", "chitchat"), ("嗨", "chitchat"),
        ("hello", "chitchat"), ("晚安", "chitchat"),
    ])
    def test_classify_chitchat(self, message, expected):
        assert classify_intent_rule(message) == expected

    @pytest.mark.parametrize("message,expected", [
        ("什么是三集定律", "factual"), ("解释一下作画崩坏", "factual"),
    ])
    def test_classify_factual(self, message, expected):
        assert classify_intent_rule(message) == expected

    def test_factual_falls_back_to_llm_for_ambiguous(self):
        assert classify_intent_rule("顶上战争是哪两方") is None

    @pytest.mark.parametrize("message,expected", [
        ("找进击的巨人评分", "lookup"), ("查命运石之门声优", "lookup"),
        ("搜索鬼灭之刃的评论", "lookup"), ("查一下这个番的信息", "lookup"),
    ])
    def test_classify_lookup(self, message, expected):
        assert classify_intent_rule(message) == expected

    @pytest.mark.parametrize("message,expected", [
        ("类似命运石之门的烧脑番", "discovery"), ("推荐几部好看的机战番", "discovery"),
        ("还有什么类似的作品", "discovery"), ("评分最高的冷门番", "discovery"),
        ("有哪些好看的番", "discovery"),
    ])
    def test_classify_discovery(self, message, expected):
        assert classify_intent_rule(message) == expected

    @pytest.mark.parametrize("message,expected", [
        ("今天放什么番", "realtime"), ("本周新番排期", "realtime"),
        ("最近什么番比较火", "realtime"), ("这季度有什么好看的", "realtime"),
    ])
    def test_classify_realtime(self, message, expected):
        assert classify_intent_rule(message) == expected

    def test_priority_queue(self):
        """复合意图不被简单意图的关键词劫持"""
        assert classify_intent_rule("找类似命运石之门的番") == "discovery"
        assert classify_intent_rule("帮忙找和进击的巨人差不多的番") == "discovery"
        assert classify_intent_rule("推荐冷门机战番") == "discovery"
        assert classify_intent_rule("最近放什么新番") == "realtime"
        assert classify_intent_rule("最近评分最高的番") == "discovery"
        assert classify_intent_rule("找进击的巨人评分") == "lookup"

    def test_short_message_falls_back_to_llm(self):
        """短消息不再硬判 chitchat，交由 LLM fallback 分类"""
        assert classify_intent_rule("嗯") is None
        assert classify_intent_rule("mygo") is None  # 短作品名不应误判

    def test_unknown_falls_back_to_none(self):
        assert classify_intent_rule("这个番的画风怎么样和那个比") is None

    def test_empty_message(self):
        assert classify_intent_rule("") == "chitchat"

    def test_intent_rules_is_ordered_list(self):
        assert isinstance(INTENT_RULES, list)
        first = [i for i, _ in INTENT_RULES[:2]]
        assert "discovery" in first and "realtime" in first


class TestIntentClassifierLLM:
    """LLM fallback 分类"""

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


class TestClassifyIntent:
    """两阶段入口"""

    async def test_rule_wins_when_matched(self):
        mock = make_mock_llm()
        intent, method = await classify_intent("推荐几部好看的番", mock)
        assert intent == "discovery" and method == "rule"
        mock.ainvoke.assert_not_called()

    async def test_llm_fallback(self):
        intent, method = await classify_intent("这个番和那个番比怎么样", make_mock_llm(content="lookup"))
        assert intent == "lookup" and method == "llm"

    async def test_returns_unknown_when_no_llm(self):
        # 注意：避免含 "hi"/"hello" 等英文关键词的输入
        intent, method = await classify_intent("花开伊吕波和taritari哪个更感人", None)
        assert intent == "unknown" and method.startswith("rule")

    async def test_empty_message(self):
        intent, method = await classify_intent("", None)
        assert intent == "chitchat" and "empty" in method
