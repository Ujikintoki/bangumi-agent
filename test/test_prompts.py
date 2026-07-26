"""
系统提示词测试 — Phase 6 统一架构

验证 prompt_builder 组装逻辑、profile 完整性、意图策略变体、
CRITIC_SYSTEM_PROMPT、output_style 四象限。
可独立运行: python -m pytest test/test_prompts.py -v
"""

from __future__ import annotations

from agent.classifier import _VALID_INTENTS
from agent.profiles import (
    AGENT_REGISTRY,
    BANGUMI_CHARACTER,
    CHARACTER_REGISTRY,
    COMPANION_PROFILE,
    NEUTRAL_CHARACTER,
    get_agent_profile,
    get_character,
)
from agent.prompt_builder import build_system_prompt as _build
from agent.prompts import COMPANION_INTENT_PROMPTS
from agent.research.prompts import (
    CRITIC_SYSTEM_PROMPT,
    INTENT_PROMPTS as DEEP_INTENT_PROMPTS,
    TOOL_DEPENDENCY_CONSTRAINT,
    build_system_prompt as build_deep_prompt,
)


class TestProfiles:
    """Profile 实例完整性。"""

    def test_all_characters_registered(self):
        """CHARACTER_REGISTRY 应包含 neutral 和 bangumi。"""
        assert "neutral" in CHARACTER_REGISTRY
        assert "bangumi" in CHARACTER_REGISTRY
        assert CHARACTER_REGISTRY["neutral"] is NEUTRAL_CHARACTER
        assert CHARACTER_REGISTRY["bangumi"] is BANGUMI_CHARACTER

    def test_all_agents_registered(self):
        """AGENT_REGISTRY 应包含 companion（以及兼容的 dialogue/research key）。"""
        assert "companion" in AGENT_REGISTRY
        assert AGENT_REGISTRY["companion"] is COMPANION_PROFILE
        # 旧 key 兼容
        assert "dialogue" in AGENT_REGISTRY
        assert "research" in AGENT_REGISTRY

    def test_bangumi_character_has_required_fields(self):
        """Bangumi 角色应有身份、动机、表达风格、约束。"""
        assert len(BANGUMI_CHARACTER.identity) > 20
        assert len(BANGUMI_CHARACTER.motivation) > 20
        assert len(BANGUMI_CHARACTER.expression_guide) > 20
        assert len(BANGUMI_CHARACTER.guardrails) > 20
        assert "字数限制" in BANGUMI_CHARACTER.guardrails
        assert "emoji" in BANGUMI_CHARACTER.guardrails

    def test_neutral_character_has_no_bangumi_persona(self):
        """Neutral 角色不应包含腹黑/吐槽人格。"""
        assert "腹黑" not in NEUTRAL_CHARACTER.identity
        assert "吐槽" not in NEUTRAL_CHARACTER.identity
        assert "萝莉" not in NEUTRAL_CHARACTER.identity

    def test_neutral_has_empty_guardrails_for_style(self):
        """Neutral 角色不应有字数上限（那是 Bangumi 的约束）。"""
        assert "30-80" not in NEUTRAL_CHARACTER.guardrails
        assert "150 字" not in NEUTRAL_CHARACTER.guardrails

    def test_get_character_unknown_falls_back_to_neutral(self):
        """未知 style key 应回退到 NEUTRAL_CHARACTER。"""
        assert get_character("nonexistent") is NEUTRAL_CHARACTER

    def test_get_agent_unknown_falls_back_to_companion(self):
        """未知 agent_type 应回退到 COMPANION_PROFILE。"""
        assert get_agent_profile("nonexistent") is COMPANION_PROFILE

    def test_companion_default_character_is_bangumi(self):
        """Companion Agent 默认角色应为 bangumi。"""
        assert COMPANION_PROFILE.default_character == "bangumi"

    def test_bangumi_is_companion_persona(self):
        """BANGUMI_CHARACTER 应为 Companion 损友人格（非旧版 Research 变体）。"""
        assert "二次元损友" in BANGUMI_CHARACTER.identity
        assert "让对话有趣" in BANGUMI_CHARACTER.motivation
        # 不应再有"深度链式调用"等 Research 变体的措辞
        assert "数据完整性优先" not in BANGUMI_CHARACTER.tool_behavior


class TestPromptBuilder:
    """Prompt builder 组装逻辑（8 层）。"""

    def test_builder_character_first(self):
        """角色身份应出现在 prompt 最前面。"""
        result = _build(
            agent_profile=COMPANION_PROFILE,
            character=BANGUMI_CHARACTER,
        )
        assert result.startswith("# 你是 Bangumi娘")

    def test_expression_guide_at_layer_2(self):
        """expression_guide 应在 layer 2（紧跟 identity）。"""
        result = _build(
            agent_profile=COMPANION_PROFILE,
            character=BANGUMI_CHARACTER,
        )
        # expression_guide 应在 capabilities 之前出现
        expr_pos = result.find("表达风格")
        cap_pos = result.find("你的能力")
        assert expr_pos > 0 and cap_pos > 0
        assert expr_pos < cap_pos, "expression_guide 应在 capabilities 之前"

    def test_builder_neutral(self):
        """Neutral 角色：中性助手，不含 Bangumi 人格。"""
        result = _build(
            agent_profile=COMPANION_PROFILE,
            character=NEUTRAL_CHARACTER,
        )
        assert "腹黑" not in result
        assert "Bangumi娘" not in result

    def test_builder_bangumi_has_persona(self):
        """Bangumi 角色：应有腹黑损友人格 + 字数限制。"""
        result = _build(
            agent_profile=COMPANION_PROFILE,
            character=BANGUMI_CHARACTER,
        )
        assert "腹黑" in result or "吐槽" in result
        assert "30-80 字" in result

    def test_builder_includes_tool_strategy(self):
        """应包含够了就停原则。"""
        result = _build(
            agent_profile=COMPANION_PROFILE,
            character=BANGUMI_CHARACTER,
        )
        assert "够了就停" in result or "最多 1-2 轮" in result

    def test_builder_includes_continuity_rules(self):
        """应包含对话连续性规则。"""
        result = _build(
            agent_profile=COMPANION_PROFILE,
            character=NEUTRAL_CHARACTER,
        )
        assert "对话连续性" in result
        assert "明确指代" in result

    def test_builder_includes_tool_calling_rules(self):
        """应包含工具调用后必须回复的规则。"""
        result = _build(
            agent_profile=COMPANION_PROFILE,
            character=NEUTRAL_CHARACTER,
        )
        assert "工具调用后必须生成文字回复" in result

    def test_builder_with_intent_strategy(self):
        """传入 intent + 深度策略时应包含策略变体。"""
        result = _build(
            agent_profile=COMPANION_PROFILE,
            character=NEUTRAL_CHARACTER,
            depth="deep",
            intent="lookup",
            intent_strategies=DEEP_INTENT_PROMPTS,
            tool_constraint=TOOL_DEPENDENCY_CONSTRAINT,
        )
        assert "精确查找" in result
        assert "工具依赖规则" in result

    def test_builder_with_shallow_intent(self):
        """Companion 浅层策略不应包含工具依赖规则。"""
        result = _build(
            agent_profile=COMPANION_PROFILE,
            character=BANGUMI_CHARACTER,
            depth="auto",
            intent="lookup",
            intent_strategies=COMPANION_INTENT_PROMPTS,
        )
        assert "精确查找" in result
        assert "工具依赖规则" not in result

    def test_builder_with_critic_feedback(self):
        """传入 critic_feedback 时应注入改进指令。"""
        result = _build(
            agent_profile=COMPANION_PROFILE,
            character=NEUTRAL_CHARACTER,
            depth="deep",
            critic_feedback="缺少评分 | 调用 get_detail | 不够具体",
        )
        assert "缺少评分" in result
        assert "请针对以上问题修正" in result

    def test_builder_without_critic_feedback(self):
        """无 critic_feedback 时不应注入改进指令。"""
        result = _build(
            agent_profile=COMPANION_PROFILE,
            character=NEUTRAL_CHARACTER,
        )
        assert "上一轮回复需要改进" not in result

    def test_builder_with_memory_context(self):
        """传入 memory_context 时应包含记忆文本。"""
        result = _build(
            agent_profile=COMPANION_PROFILE,
            character=BANGUMI_CHARACTER,
            memory_context="## 用户历史\n- [昨天] 讨论了高达SEED",
        )
        assert "高达SEED" in result
        assert "用户历史" in result

    def test_builder_neutral_no_bangumi_persona(self):
        """Neutral 角色不应包含腹黑/吐槽——任何位置。"""
        result = _build(
            agent_profile=COMPANION_PROFILE,
            character=NEUTRAL_CHARACTER,
        )
        assert "腹黑萝莉" not in result
        assert "毒舌吐槽役" not in result
        assert "Bangumi娘" not in result

    def test_builder_deep_has_data_guide(self):
        """deep 模式应包含数据解读指南。"""
        result = _build(
            agent_profile=COMPANION_PROFILE,
            character=NEUTRAL_CHARACTER,
            depth="deep",
            intent="lookup",
            intent_strategies=DEEP_INTENT_PROMPTS,
            tool_constraint=TOOL_DEPENDENCY_CONSTRAINT,
            data_guide="## 数据解读指南\ntest data guide content",
        )
        assert "数据解读指南" in result

    def test_builder_shallow_no_data_guide(self):
        """非 deep 模式不应包含数据解读指南。"""
        result = _build(
            agent_profile=COMPANION_PROFILE,
            character=BANGUMI_CHARACTER,
            depth="auto",
            intent="lookup",
            intent_strategies=COMPANION_INTENT_PROMPTS,
        )
        assert "数据解读指南" not in result


class TestDeepPrompt:
    """深度模式 prompt（research.prompts.build_system_prompt）。"""

    def test_returns_non_empty(self):
        result = build_deep_prompt("lookup")
        assert len(result) > 200

    def test_neutral_excludes_bangumi_style(self):
        result = build_deep_prompt("lookup", output_style="neutral")
        assert "腹黑" not in result

    def test_bangumi_includes_style(self):
        result = build_deep_prompt("lookup", output_style="bangumi")
        assert "损友" in result or "腹黑" in result
        assert "吐槽" in result

    def test_includes_intent_strategy(self):
        result = build_deep_prompt("discovery")
        assert "发现推荐" in result

    def test_includes_tool_constraint(self):
        result = build_deep_prompt("lookup")
        assert "工具依赖规则" in result

    def test_includes_critic_feedback(self):
        result = build_deep_prompt(
            "lookup", critic_feedback="缺少评分 | 调用 get_detail"
        )
        assert "缺少评分" in result

    def test_debate_intent_exists(self):
        assert "debate" in DEEP_INTENT_PROMPTS
        result = build_deep_prompt("debate")
        assert "观点争论" in result

    def test_emotional_intent_exists(self):
        assert "emotional" in DEEP_INTENT_PROMPTS
        result = build_deep_prompt("emotional")
        assert "情绪" in result

    def test_deep_prompt_has_data_guide(self):
        """深度 prompt 应包含数据解读指南。"""
        result = build_deep_prompt("lookup")
        assert "数据解读指南" in result


class TestIntentPrompts:
    """INTENT_PROMPTS 完整性。"""

    def test_all_valid_intents_have_deep_prompts(self):
        for intent in _VALID_INTENTS:
            assert intent in DEEP_INTENT_PROMPTS, f"缺少 deep intent: {intent}"
            assert len(DEEP_INTENT_PROMPTS[intent]) > 0

    def test_all_valid_intents_have_companion_prompts(self):
        for intent in _VALID_INTENTS:
            assert intent in COMPANION_INTENT_PROMPTS, f"缺少 companion intent: {intent}"
            assert len(COMPANION_INTENT_PROMPTS[intent]) > 0

    def test_lookup_and_unknown_include_tool_constraint(self):
        assert TOOL_DEPENDENCY_CONSTRAINT.strip() in DEEP_INTENT_PROMPTS["lookup"]
        assert TOOL_DEPENDENCY_CONSTRAINT.strip() in DEEP_INTENT_PROMPTS["unknown"]

    def test_chitchat_factual_exclude_tool_constraint(self):
        assert TOOL_DEPENDENCY_CONSTRAINT.strip() not in DEEP_INTENT_PROMPTS["chitchat"]
        assert TOOL_DEPENDENCY_CONSTRAINT.strip() not in DEEP_INTENT_PROMPTS["factual"]

    def test_companion_intents_exclude_tool_constraint(self):
        """Companion 浅层策略不应包含工具依赖约束。"""
        for intent in COMPANION_INTENT_PROMPTS:
            assert TOOL_DEPENDENCY_CONSTRAINT.strip() not in COMPANION_INTENT_PROMPTS[intent], \
                f"companion intent {intent} 不应包含工具依赖约束"

    def test_critic_prompt_has_escape_hatch(self):
        assert "逃逸舱" in CRITIC_SYSTEM_PROMPT or "Escape Hatch" in CRITIC_SYSTEM_PROMPT
        assert "必须判定为 PASS" in CRITIC_SYSTEM_PROMPT

    def test_base_prompt_has_data_model_constraint(self):
        result = build_deep_prompt("lookup")
        assert "只有" in result and "评分" in result
        assert "角色" in result

    def test_lookup_has_exit_conditions(self):
        lookup = DEEP_INTENT_PROMPTS["lookup"]
        assert "退出条件" in lookup
        assert "名称消歧" in lookup
        assert "诚实告知" in lookup

    def test_debate_strategy_data_backs_opinion(self):
        debate = DEEP_INTENT_PROMPTS["debate"]
        assert "数据支撑观点" in debate or "用数据" in debate

    def test_emotional_strategy_empathy_first(self):
        emotional = DEEP_INTENT_PROMPTS["emotional"]
        assert "共情" in emotional


class TestHonestyPrinciple:
    """Prompt 中数据不足时的诚实兜底原则。"""

    def test_companion_prompt_has_honesty_principle(self):
        """Companion prompt 应包含'诚实比瞎编'原则。"""
        result = _build(
            agent_profile=COMPANION_PROFILE,
            character=BANGUMI_CHARACTER,
        )
        assert "诚实比瞎编" in result
        assert "不要编造" in result

    def test_deep_prompt_has_honesty_principle(self):
        """深度 prompt 应包含'诚实比瞎编'原则。"""
        result = build_deep_prompt("lookup")
        assert "诚实比瞎编" in result
        assert "不要编造" in result
