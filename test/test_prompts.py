"""
系统提示词测试

验证 prompt_builder 组装逻辑、profile 完整性、意图策略变体、
CRITIC_SYSTEM_PROMPT、output_style 四象限。
可独立运行: python -m pytest test/test_prompts.py -v
"""

from __future__ import annotations

from agent.classifier import _VALID_INTENTS
from agent.dialogue.prompts import build_dialogue_prompt
from agent.profiles import (
    AGENT_REGISTRY,
    BANGUMI_CHARACTER,
    CHARACTER_REGISTRY,
    DIALOGUE_PROFILE,
    NEUTRAL_CHARACTER,
    RESEARCH_PROFILE,
    get_agent_profile,
    get_character,
)
from agent.prompt_builder import build_system_prompt as _build
from agent.research.prompts import (
    CRITIC_SYSTEM_PROMPT,
    INTENT_PROMPTS,
    TOOL_DEPENDENCY_CONSTRAINT,
    build_system_prompt,
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
        """AGENT_REGISTRY 应包含 dialogue 和 research。"""
        assert "dialogue" in AGENT_REGISTRY
        assert "research" in AGENT_REGISTRY
        assert AGENT_REGISTRY["dialogue"] is DIALOGUE_PROFILE
        assert AGENT_REGISTRY["research"] is RESEARCH_PROFILE

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

    def test_get_agent_unknown_falls_back_to_dialogue(self):
        """未知 agent_type 应回退到 DIALOGUE_PROFILE。"""
        assert get_agent_profile("nonexistent") is DIALOGUE_PROFILE

    def test_dialogue_default_character_is_bangumi(self):
        """Dialogue Agent 默认角色应为 bangumi。"""
        assert DIALOGUE_PROFILE.default_character == "bangumi"

    def test_research_default_character_is_neutral(self):
        """Research Agent 默认角色应为 neutral。"""
        assert RESEARCH_PROFILE.default_character == "neutral"


class TestPromptBuilder:
    """Prompt builder 组装逻辑。"""

    def test_builder_character_first(self):
        """角色身份应出现在 prompt 最前面。"""
        result = _build(
            agent_profile=DIALOGUE_PROFILE,
            character=BANGUMI_CHARACTER,
        )
        # Bangumi娘 身份应在开头
        assert result.startswith("# 你是 Bangumi娘")

    def test_builder_neutral_research(self):
        """Research + neutral: 中性助手，无腹黑/吐槽。"""
        result = _build(
            agent_profile=RESEARCH_PROFILE,
            character=NEUTRAL_CHARACTER,
        )
        assert "腹黑" not in result
        assert "吐槽" not in result
        assert "⭐评分" in result  # 结构化格式保留

    def test_builder_bangumi_research(self):
        """Research + bangumi: 腹黑人格，但无字数限制（使用 BANGUMI_RESEARCH_CHARACTER）。"""
        result = _build(
            agent_profile=RESEARCH_PROFILE,
            character=get_character("bangumi", agent_type="research"),
        )
        assert "腹黑" in result
        assert "吐槽" in result
        assert "30-80 字" not in result
        # 应包含数据完整性声明
        assert "数据完整性" in result or "不要因为风格" in result

    def test_builder_includes_tool_strategy(self):
        """Dialogue 应包含浅层原则。"""
        result = _build(
            agent_profile=DIALOGUE_PROFILE,
            character=BANGUMI_CHARACTER,
        )
        assert "浅层原则" in result or "最多 2 轮" in result

    def test_builder_includes_continuity_rules(self):
        """应包含对话连续性规则。"""
        result = _build(
            agent_profile=DIALOGUE_PROFILE,
            character=NEUTRAL_CHARACTER,
        )
        assert "对话连续性" in result
        assert "明确指代" in result

    def test_builder_includes_tool_calling_rules(self):
        """应包含工具调用后必须回复的规则。"""
        result = _build(
            agent_profile=DIALOGUE_PROFILE,
            character=NEUTRAL_CHARACTER,
        )
        assert "工具调用后必须生成文字回复" in result

    def test_builder_with_intent_strategy(self):
        """传入 intent 时应包含策略变体。"""
        result = _build(
            agent_profile=RESEARCH_PROFILE,
            character=NEUTRAL_CHARACTER,
            intent="lookup",
            intent_strategies=INTENT_PROMPTS,
            tool_constraint=TOOL_DEPENDENCY_CONSTRAINT,
        )
        assert "精确查找" in result
        assert "工具依赖规则" in result

    def test_builder_with_critic_feedback(self):
        """传入 critic_feedback 时应注入改进指令。"""
        result = _build(
            agent_profile=RESEARCH_PROFILE,
            character=NEUTRAL_CHARACTER,
            critic_feedback="缺少评分 | 调用 get_detail | 不够具体",
        )
        assert "缺少评分" in result
        assert "请针对以上问题修正" in result

    def test_builder_without_critic_feedback(self):
        """无 critic_feedback 时不应注入改进指令。"""
        result = _build(
            agent_profile=RESEARCH_PROFILE,
            character=NEUTRAL_CHARACTER,
        )
        assert "上一轮回复需要改进" not in result

    def test_builder_with_memory_context(self):
        """传入 memory_context 时应包含记忆文本。"""
        result = _build(
            agent_profile=DIALOGUE_PROFILE,
            character=BANGUMI_CHARACTER,
            memory_context="## 用户历史\n- [昨天] 讨论了高达SEED",
        )
        assert "高达SEED" in result
        assert "用户历史" in result

    def test_builder_with_last_chance(self):
        """传入 last_chance_instruction 时应出现在末尾。"""
        instruction = "## ⚠️ 最后一轮——必须现在回复"
        result = _build(
            agent_profile=DIALOGUE_PROFILE,
            character=BANGUMI_CHARACTER,
            last_chance_instruction=instruction,
        )
        assert result.endswith(instruction)

    def test_builder_neutral_no_bangumi_persona(self):
        """Neutral 角色不应包含腹黑/吐槽——任何位置。"""
        result = _build(
            agent_profile=DIALOGUE_PROFILE,
            character=NEUTRAL_CHARACTER,
        )
        assert "腹黑萝莉" not in result
        assert "毒舌吐槽役" not in result
        assert "Bangumi娘" not in result


class TestBuildDialoguePrompt:
    """build_dialogue_prompt 薄封装。"""

    def test_returns_non_empty(self):
        result = build_dialogue_prompt()
        assert len(result) > 200

    def test_bangumi_has_persona(self):
        result = build_dialogue_prompt(output_style="bangumi")
        assert "腹黑" in result

    def test_neutral_excludes_persona(self):
        result = build_dialogue_prompt(output_style="neutral")
        assert "腹黑萝莉" not in result
        assert "毒舌吐槽役" not in result

    def test_includes_memory_when_present(self):
        result = build_dialogue_prompt(
            memory_context="## 用户历史\n- 讨论了高达SEED",
            output_style="bangumi",
        )
        assert "高达SEED" in result


class TestBuildResearchPrompt:
    """build_system_prompt 薄封装。"""

    def test_returns_non_empty(self):
        result = build_system_prompt("lookup")
        assert len(result) > 200

    def test_neutral_excludes_bangumi_style(self):
        result = build_system_prompt("lookup", output_style="neutral")
        assert "腹黑" not in result

    def test_bangumi_includes_style(self):
        result = build_system_prompt("lookup", output_style="bangumi")
        assert "腹黑" in result
        assert "吐槽" in result

    def test_includes_intent_strategy(self):
        result = build_system_prompt("discovery")
        assert "发现推荐" in result

    def test_includes_tool_constraint(self):
        result = build_system_prompt("lookup")
        assert "工具依赖规则" in result

    def test_includes_critic_feedback(self):
        result = build_system_prompt(
            "lookup", critic_feedback="缺少评分 | 调用 get_detail"
        )
        assert "缺少评分" in result

    def test_debate_intent_exists(self):
        """debate 意图应有策略变体。"""
        assert "debate" in INTENT_PROMPTS
        result = build_system_prompt("debate")
        assert "观点争论" in result or "debate" in result.lower()

    def test_emotional_intent_exists(self):
        """emotional 意图应有策略变体。"""
        assert "emotional" in INTENT_PROMPTS
        result = build_system_prompt("emotional")
        assert "情绪" in result


class TestIntentPrompts:
    """INTENT_PROMPTS 完整性。"""

    def test_all_valid_intents_have_prompts(self):
        for intent in _VALID_INTENTS:
            assert intent in INTENT_PROMPTS, f"缺少 intent: {intent}"
            assert len(INTENT_PROMPTS[intent]) > 0

    def test_lookup_and_unknown_include_tool_constraint(self):
        assert TOOL_DEPENDENCY_CONSTRAINT.strip() in INTENT_PROMPTS["lookup"]
        assert TOOL_DEPENDENCY_CONSTRAINT.strip() in INTENT_PROMPTS["unknown"]

    def test_chitchat_factual_exclude_tool_constraint(self):
        assert TOOL_DEPENDENCY_CONSTRAINT.strip() not in INTENT_PROMPTS["chitchat"]
        assert TOOL_DEPENDENCY_CONSTRAINT.strip() not in INTENT_PROMPTS["factual"]

    def test_debate_emotional_exclude_tool_constraint(self):
        """debate/emotional 不应包含工具依赖约束——它们默认不绑工具。"""
        assert TOOL_DEPENDENCY_CONSTRAINT.strip() not in INTENT_PROMPTS["debate"]
        assert TOOL_DEPENDENCY_CONSTRAINT.strip() not in INTENT_PROMPTS["emotional"]

    def test_critic_prompt_has_escape_hatch(self):
        assert "逃逸舱" in CRITIC_SYSTEM_PROMPT or "Escape Hatch" in CRITIC_SYSTEM_PROMPT
        assert "必须判定为 PASS" in CRITIC_SYSTEM_PROMPT

    def test_base_prompt_has_data_model_constraint(self):
        """Research prompt 应包含数据模型约束。"""
        result = build_system_prompt("lookup")
        assert "只有" in result and "评分" in result
        assert "角色" in result

    def test_lookup_has_exit_conditions(self):
        lookup = INTENT_PROMPTS["lookup"]
        assert "退出条件" in lookup
        assert "名称消歧" in lookup
        assert "诚实告知" in lookup

    def test_discovery_has_exit_conditions(self):
        discovery = INTENT_PROMPTS["discovery"]
        assert "退出条件" in discovery

    def test_debate_strategy_no_data(self):
        """debate 策略应强调不依赖数据。"""
        debate = INTENT_PROMPTS["debate"]
        assert "不是数据查询" in debate or "少调工具" in debate

    def test_emotional_strategy_empathy_first(self):
        """emotional 策略应强调先共情。"""
        emotional = INTENT_PROMPTS["emotional"]
        assert "共情" in emotional


class TestResearchContinuityRules:
    """Research prompt 对话连续性规则。"""

    def test_has_continuity_rules(self):
        result = build_system_prompt("lookup")
        assert "对话连续性规则" in result
        assert "话题绑定检测" in result

    def test_has_anaphora_signals(self):
        result = build_system_prompt("lookup")
        assert "明确指代" in result
        assert "全新话题" in result
        assert "模糊边界" in result

    def test_has_principle(self):
        result = build_system_prompt("lookup")
        assert "宁可少用历史" in result
        assert "不要错误关联" in result
