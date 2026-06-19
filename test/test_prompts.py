"""
系统提示词测试

验证 BASE_SYSTEM_PROMPT、INTENT_PROMPTS、build_system_prompt()、
CRITIC_SYSTEM_PROMPT、build_dialogue_prompt() 完整性。
包含 output_style 四象限验证。
可独立运行: python -m pytest test/test_prompts.py -v
"""

from __future__ import annotations

from agent.classifier import _VALID_INTENTS
from agent.dialogue.prompts import (
    DIALOGUE_CORE_PROMPT,
    build_dialogue_prompt,
)
from agent.styles import (
    BANGUMI_STYLE_APPENDIX,
    BANGUMI_STYLE_RESEARCH_APPENDIX,
    STYLE_APPENDICES,
    STYLE_APPENDICES_RESEARCH,
)
from agent.research.prompts import (
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

    # ── 数据模型约束 & 退出条件 ────────────────────────────

    def test_base_prompt_has_data_model_constraint(self):
        """BASE_SYSTEM_PROMPT 应包含角色/人物无评分的领域约束"""
        assert "只有" in BASE_SYSTEM_PROMPT and "评分" in BASE_SYSTEM_PROMPT
        assert "角色" in BASE_SYSTEM_PROMPT and "没有评分" in BASE_SYSTEM_PROMPT or "没有" in BASE_SYSTEM_PROMPT
        assert "collects" in BASE_SYSTEM_PROMPT

    def test_lookup_has_exit_conditions(self):
        """lookup prompt 应包含退出条件和名称消歧指导"""
        lookup = INTENT_PROMPTS["lookup"]
        assert "退出条件" in lookup
        assert "名称消歧" in lookup
        assert "诚实告知" in lookup
        assert "追问" in lookup

    def test_discovery_has_exit_conditions(self):
        """discovery prompt 应包含退出条件"""
        discovery = INTENT_PROMPTS["discovery"]
        assert "退出条件" in discovery
        assert "诚实告知" in discovery

    # ── 对话连续性规则 ─────────────────────────────────────────

    def test_research_has_continuity_rules(self):
        """BASE_SYSTEM_PROMPT 应包含对话连续性规则"""
        assert "对话连续性规则" in BASE_SYSTEM_PROMPT
        assert "话题绑定检测" in BASE_SYSTEM_PROMPT

    def test_research_continuity_anaphora_signals(self):
        """BASE_SYSTEM_PROMPT 应列出指代信号和边界判定"""
        assert "明确指代" in BASE_SYSTEM_PROMPT
        assert "全新话题" in BASE_SYSTEM_PROMPT
        assert "模糊边界" in BASE_SYSTEM_PROMPT
        # 判定示例应包含正反例
        assert "赛马娘有新作吗" in BASE_SYSTEM_PROMPT
        assert "不提 EVA" in BASE_SYSTEM_PROMPT or "不出现 EVA" in BASE_SYSTEM_PROMPT

    def test_research_continuity_principle(self):
        """BASE_SYSTEM_PROMPT 应包含保守原则"""
        assert "宁可少用历史" in BASE_SYSTEM_PROMPT
        assert "不要错误关联" in BASE_SYSTEM_PROMPT
        assert "污染无关回答" in BASE_SYSTEM_PROMPT

    def test_research_continuity_intent_persistence(self):
        """BASE_SYSTEM_PROMPT 应引导意图延续，防止跳到无关意图"""
        assert "意图延续" in BASE_SYSTEM_PROMPT
        assert "get_trending_topics" in BASE_SYSTEM_PROMPT  # 反例引用

    def test_dialogue_has_continuity_rules(self):
        """DIALOGUE_CORE_PROMPT 应包含精简版对话连续性规则"""
        assert "对话连续性" in DIALOGUE_CORE_PROMPT
        assert "明确指代" in DIALOGUE_CORE_PROMPT
        assert "全新话题" in DIALOGUE_CORE_PROMPT
        assert "不要把旧话题内容混入新回答" in DIALOGUE_CORE_PROMPT
        assert "宁可问清也不瞎猜" in DIALOGUE_CORE_PROMPT

    # ── output_style 四象限验证 ──────────────────────────────

    def test_research_neutral_excludes_style(self):
        """research + neutral: 不应包含 Bangumi娘 人格"""
        result = build_system_prompt("lookup", output_style="neutral")
        assert "腹黑" not in result
        assert "吐槽" not in result

    def test_research_bangumi_includes_style(self):
        """research + bangumi: 应包含 Bangumi娘 人格（软版本）"""
        result = build_system_prompt("lookup", output_style="bangumi")
        assert "腹黑" in result
        assert "吐槽" in result
        # 软版本不应包含字数限制
        assert "30-80 字" not in BANGUMI_STYLE_RESEARCH_APPENDIX
        assert "150 字" not in BANGUMI_STYLE_RESEARCH_APPENDIX
        # 软版本应强调数据完整性
        assert "数据完整性" in BANGUMI_STYLE_RESEARCH_APPENDIX or "不要因为风格" in BANGUMI_STYLE_RESEARCH_APPENDIX

    def test_dialogue_neutral_excludes_persona(self):
        """dialogue + neutral: 不应包含 Bangumi娘 人格"""
        result = build_dialogue_prompt(output_style="neutral")
        assert "腹黑萝莉" not in result
        assert "毒舌吐槽役" not in result
        # 但应包含核心能力
        assert "工具使用策略" in result or "浅层原则" in result

    def test_dialogue_bangumi_includes_persona(self):
        """dialogue + bangumi: 应包含 Bangumi娘 人格"""
        result = build_dialogue_prompt(output_style="bangumi")
        assert "腹黑" in result
        assert "吐槽" in result
        assert "30-80 字" in result

    def test_dialogue_core_prompt_has_no_persona(self):
        """DIALOGUE_CORE_PROMPT 本身不应包含人格内容"""
        assert "腹黑萝莉" not in DIALOGUE_CORE_PROMPT
        assert "毒舌吐槽役" not in DIALOGUE_CORE_PROMPT
        assert "Bangumi娘" not in DIALOGUE_CORE_PROMPT

    def test_style_registry_keys(self):
        """两个风格注册表都应包含 neutral 和 bangumi"""
        for registry in (STYLE_APPENDICES, STYLE_APPENDICES_RESEARCH):
            assert "neutral" in registry
            assert "bangumi" in registry
            assert registry["neutral"] == ""
            assert len(registry["bangumi"]) > 0
