"""
系统提示词测试

验证 BASE_SYSTEM_PROMPT、INTENT_PROMPTS、build_system_prompt()、
CRITIC_SYSTEM_PROMPT 完整性。
可独立运行: python -m pytest test/test_prompts.py -v
"""

from __future__ import annotations

from agent.classifier import _VALID_INTENTS
from agent.prompts import (
    BASE_SYSTEM_PROMPT,
    CRITIC_SYSTEM_PROMPT,
    INTENT_PROMPTS,
    TOOL_DEPENDENCY_CONSTRAINT,
    build_system_prompt,
)


class TestPrompts:
    def test_base_prompt_non_empty(self):
        assert "Bangumi 助手" in BASE_SYSTEM_PROMPT

    def test_build_includes_intent_prompt(self):
        result = build_system_prompt("discovery")
        assert "发现推荐" in result or "RAG" in result

    def test_build_includes_critic_feedback(self):
        result = build_system_prompt("lookup", critic_feedback="缺少评分 | 调用 get_detail")
        assert "缺少评分" in result and "请针对以上问题修正" in result

    def test_build_no_feedback_when_empty(self):
        assert "上一轮回复需要改进" not in build_system_prompt("lookup")

    def test_all_intents_have_prompts(self):
        for intent in _VALID_INTENTS:
            assert intent in INTENT_PROMPTS, f"缺少 intent: {intent}"
            assert len(INTENT_PROMPTS[intent]) > 0

    def test_lookup_and_unknown_include_tool_constraint(self):
        assert TOOL_DEPENDENCY_CONSTRAINT.strip() in INTENT_PROMPTS["lookup"]
        assert TOOL_DEPENDENCY_CONSTRAINT.strip() in INTENT_PROMPTS["unknown"]

    def test_chitchat_and_factual_exclude_tool_constraint(self):
        assert TOOL_DEPENDENCY_CONSTRAINT.strip() not in INTENT_PROMPTS["chitchat"]
        assert TOOL_DEPENDENCY_CONSTRAINT.strip() not in INTENT_PROMPTS["factual"]

    def test_critic_prompt_has_escape_hatch(self):
        assert "逃逸舱" in CRITIC_SYSTEM_PROMPT or "Escape Hatch" in CRITIC_SYSTEM_PROMPT
        assert "必须判定为 PASS" in CRITIC_SYSTEM_PROMPT
