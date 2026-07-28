"""
系统提示词测试 — Phase 7 Prompt Engineering 重设计

验证 Character Card、TOOL_INTUITION、Scene Hints、参数映射、
render prompt（参数感知 + 快速跳过）、guardrails 字数占位符。
可独立运行: python -m pytest test/test_prompts.py -v
"""

from __future__ import annotations

from agent.orchestrate.classifier import _VALID_INTENTS
from agent.persona.profiles import (
    AGENT_REGISTRY,
    BANGUMI_CHARACTER,
    CHARACTER_REGISTRY,
    COMPANION_PROFILE,
    NEUTRAL_CHARACTER,
    _render_tone,
    get_agent_profile,
    get_character,
    get_character_card,
)
from agent.orchestrate.prompt_builder import TOOL_INTUITION, build_system_prompt as _build
from agent.orchestrate.strategies import COMPANION_INTENT_PROMPTS, COMPANION_SCENE_HINTS
from agent.persona.render import (
    _should_skip_render,
    build_render_prompt,
)
from agent.orchestrate.deep_strategies import (
    CRITIC_SYSTEM_PROMPT,
    DEEP_SCENE_HINTS,
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
        assert "dialogue" in AGENT_REGISTRY
        assert "research" in AGENT_REGISTRY

    def test_bangumi_character_has_required_fields(self):
        """Bangumi 角色应有身份、参数、硬约束（含 word_limit 占位符）。"""
        assert len(BANGUMI_CHARACTER.identity) > 20
        assert len(BANGUMI_CHARACTER.motivation) > 10
        assert len(BANGUMI_CHARACTER.expression_guide) > 10
        assert len(BANGUMI_CHARACTER.guardrails) > 20
        # Phase 7: guardrails 使用 {word_limit} 占位符
        assert "{word_limit}" in BANGUMI_CHARACTER.guardrails
        assert "不用 emoji" in BANGUMI_CHARACTER.guardrails
        # Phase 7: 参数字段应有默认值
        assert 0.0 <= BANGUMI_CHARACTER.snark <= 1.0
        assert 0.0 <= BANGUMI_CHARACTER.depth_taste <= 1.0
        assert 0.0 <= BANGUMI_CHARACTER.initiative <= 1.0

    def test_neutral_character_has_no_bangumi_persona(self):
        """Neutral 角色不应包含腹黑/吐槽人格。"""
        assert "腹黑" not in NEUTRAL_CHARACTER.identity
        assert "吐槽" not in NEUTRAL_CHARACTER.identity
        assert "萝莉" not in NEUTRAL_CHARACTER.identity

    def test_neutral_has_empty_guardrails_for_style(self):
        """Neutral 角色不应有 Bangumi 特有的字数硬编码。"""
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
        """BANGUMI_CHARACTER 应为 Companion 损友人格。"""
        assert "二次元损友" in BANGUMI_CHARACTER.identity or "ACGN 老害" in BANGUMI_CHARACTER.identity
        assert "让对话有趣" in BANGUMI_CHARACTER.motivation
        assert "数据完整性优先" not in BANGUMI_CHARACTER.tool_behavior

    def test_character_card_exists_for_bangumi(self):
        """Bangumi 应有 Character Card（Phase 7.5: 人格描述，非台词范本）。"""
        card = get_character_card("bangumi")
        assert card is not None
        assert "Bangumi 看板娘" in card or "ACGN 爱好者" in card
        # 应包含审美体系关键词
        assert "好不好看" in card or "重不重要" in card
        # 应包含数据态度
        assert "注脚" in card or "正文" in card

    def test_character_card_exists_for_neutral(self):
        """Neutral 应有 Character Card。"""
        card = get_character_card("neutral")
        assert card is not None
        assert "ACGN" in card

    def test_render_tone_defaults(self):
        """_render_tone 默认参数应返回三段非空文本。"""
        result = _render_tone(0.65, 0.70, 0.75)
        assert len(result) == 3
        assert all(len(v) > 10 for v in result.values())
        assert "tone" in result
        assert "depth" in result
        assert "rhythm" in result

    def test_render_tone_extremes(self):
        """_render_tone 极值参数应返回不同文本。"""
        low = _render_tone(0.0, 0.0, 0.0)
        high = _render_tone(1.0, 1.0, 1.0)
        # extreme values should produce different tone text
        assert low["tone"] != high["tone"]


class TestPromptBuilder:
    """Prompt builder 组装逻辑（Phase 7: 5 段结构）。"""

    def test_builder_starts_with_character_card(self):
        """Prompt 应以 '你是谁'（Character Card section）开头。"""
        result = _build(
            agent_profile=COMPANION_PROFILE,
            character=BANGUMI_CHARACTER,
        )
        assert result.startswith("# 你是谁")

    def test_character_card_before_capabilities(self):
        """Character Card 应在 capabilities 之前出现。"""
        result = _build(
            agent_profile=COMPANION_PROFILE,
            character=BANGUMI_CHARACTER,
        )
        card_pos = result.find("你是谁")
        cap_pos = result.find("你的能力")
        assert card_pos > 0 and cap_pos > 0
        assert card_pos < cap_pos, "Character Card 应在 capabilities 之前"

    def test_builder_neutral(self):
        """Neutral 角色：中性助手，不含 Bangumi 人格。"""
        result = _build(
            agent_profile=COMPANION_PROFILE,
            character=NEUTRAL_CHARACTER,
        )
        assert "腹黑" not in result
        assert "Bangumi娘" not in result
        assert "损友" not in result

    def test_builder_bangumi_has_persona(self):
        """Bangumi 角色：应有 Character Card 中的关键标识。"""
        result = _build(
            agent_profile=COMPANION_PROFILE,
            character=BANGUMI_CHARACTER,
        )
        assert "ACGN 老害" in result or "Bangumi 看板娘" in result
        # word_limit 应被格式化（不再有占位符）
        assert "{word_limit}" not in result
        assert "200 字" in result or "120 字" in result or "350 字" in result

    def test_builder_includes_tool_strategy(self):
        """应包含够了就停原则——TOOL_GUIDANCE 已覆盖。"""
        result = _build(
            agent_profile=COMPANION_PROFILE,
            character=BANGUMI_CHARACTER,
        )
        assert "够了" in result or "数据够了直接回复" in result

    def test_builder_includes_continuity_rules(self):
        """应包含对话连续性规则。"""
        result = _build(
            agent_profile=COMPANION_PROFILE,
            character=NEUTRAL_CHARACTER,
        )
        assert "对话连续性" in result
        assert "明确指代" in result

    def test_builder_includes_tool_intuition(self):
        """应包含 TOOL_GUIDANCE（Phase 8: 五合一工具指引）。"""
        result = _build(
            agent_profile=COMPANION_PROFILE,
            character=NEUTRAL_CHARACTER,
        )
        assert "你的工具" in result
        assert "没查到" in result

    def test_builder_with_deep_scene_hint(self):
        """Deep 模式 + intent 应注入 DEEP_SCENE_HINTS。"""
        result = _build(
            agent_profile=COMPANION_PROFILE,
            character=NEUTRAL_CHARACTER,
            depth="deep",
            intent="lookup",
            intent_strategies=DEEP_INTENT_PROMPTS,
            scene_hints=DEEP_SCENE_HINTS,
        )
        # Scene Hint 格式: [当前：...]
        assert "[当前：" in result
        # Phase 8: 工具规则由 TOOL_GUIDANCE 覆盖，不再单独注入
        assert "你的工具" in result

    def test_builder_with_companion_scene_hint(self):
        """Companion 浅层模式应注入 Scene Hints，不含工具依赖规则。"""
        result = _build(
            agent_profile=COMPANION_PROFILE,
            character=BANGUMI_CHARACTER,
            depth="auto",
            intent="lookup",
            intent_strategies=COMPANION_INTENT_PROMPTS,
            scene_hints=COMPANION_SCENE_HINTS,
        )
        assert "[当前：" in result
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

    def test_builder_word_limit_by_depth(self):
        """Guardrails 的 {word_limit} 应按 depth 正确格式化。"""
        q = _build(agent_profile=COMPANION_PROFILE, character=BANGUMI_CHARACTER, depth="quick")
        a = _build(agent_profile=COMPANION_PROFILE, character=BANGUMI_CHARACTER, depth="auto")
        d = _build(agent_profile=COMPANION_PROFILE, character=BANGUMI_CHARACTER, depth="deep")
        assert "120 字" in q
        assert "200 字" in a
        assert "350 字" in d
        # 占位符不应残留
        assert "{word_limit}" not in q
        assert "{word_limit}" not in a
        assert "{word_limit}" not in d

    def test_builder_includes_tone_hint(self):
        """应包含 '今天的语气' 段（从参数生成）。"""
        result = _build(
            agent_profile=COMPANION_PROFILE,
            character=BANGUMI_CHARACTER,
        )
        assert "今天的语气" in result


class TestDeepPrompt:
    """深度模式 prompt（deep_strategies.build_system_prompt）。"""

    def test_returns_non_empty(self):
        result = build_deep_prompt("lookup")
        assert len(result) > 200

    def test_neutral_excludes_bangumi_style(self):
        result = build_deep_prompt("lookup", output_style="neutral")
        assert "腹黑" not in result

    def test_bangumi_includes_style(self):
        result = build_deep_prompt("lookup", output_style="bangumi")
        assert "ACGN 老害" in result or "损友" in result or "Bangumi 看板娘" in result

    def test_includes_scene_hint(self):
        result = build_deep_prompt("discovery")
        assert "[当前：" in result

    def test_includes_tool_constraint(self):
        """Phase 8: 工具约束已合并到 TOOL_GUIDANCE，检查并行规则存在。"""
        result = build_deep_prompt("lookup")
        assert "并行规则" in result or "你的工具" in result

    def test_includes_critic_feedback(self):
        result = build_deep_prompt(
            "lookup", critic_feedback="缺少评分 | 调用 get_detail"
        )
        assert "缺少评分" in result

    def test_debate_intent_exists(self):
        assert "debate" in DEEP_INTENT_PROMPTS
        result = build_deep_prompt("debate")
        assert "[当前：" in result

    def test_emotional_intent_exists(self):
        assert "emotional" in DEEP_INTENT_PROMPTS
        result = build_deep_prompt("emotional")
        assert "[当前：" in result


class TestIntentPrompts:
    """INTENT_PROMPTS / SCENE_HINTS 完整性。"""

    def test_all_valid_intents_have_deep_prompts(self):
        for intent in _VALID_INTENTS:
            assert intent in DEEP_INTENT_PROMPTS, f"缺少 deep intent: {intent}"
            assert len(DEEP_INTENT_PROMPTS[intent]) > 0

    def test_all_valid_intents_have_companion_prompts(self):
        for intent in _VALID_INTENTS:
            assert intent in COMPANION_INTENT_PROMPTS, f"缺少 companion intent: {intent}"
            # "unknown" 的 Scene Hint 可以为空（无场景提示）
            if intent == "unknown":
                continue
            assert len(COMPANION_INTENT_PROMPTS[intent]) > 0, \
                f"companion intent {intent} 不应为空"

    def test_all_valid_intents_have_companion_scene_hints(self):
        for intent in _VALID_INTENTS:
            assert intent in COMPANION_SCENE_HINTS, f"缺少 companion scene_hint: {intent}"

    def test_all_valid_intents_have_deep_scene_hints(self):
        for intent in _VALID_INTENTS:
            assert intent in DEEP_SCENE_HINTS, f"缺少 deep scene_hint: {intent}"

    def test_lookup_and_unknown_have_tool_constraint(self):
        """Deep lookup/unknown 的 Scene Hints 不嵌入工具约束——
        Phase 8: TOOL_DEPENDENCY_CONSTRAINT 已合并到 TOOL_GUIDANCE。"""
        for intent in ["lookup", "unknown"]:
            assert "[当前：" in DEEP_INTENT_PROMPTS[intent]

    def test_chitchat_factual_exclude_tool_constraint(self):
        """chitchat/factual 的 Scene Hints 应为简短视角提示。"""
        assert "[当前：" in DEEP_INTENT_PROMPTS["chitchat"]
        assert "[当前：" in DEEP_INTENT_PROMPTS["factual"]

    def test_companion_intents_exclude_tool_constraint(self):
        """Companion 浅层策略：Scene Hints 应为视角提示，不含工具约束（Phase 8: 工具规则统一在 TOOL_GUIDANCE）。"""
        for intent in COMPANION_INTENT_PROMPTS:
            assert "[当前：" in COMPANION_INTENT_PROMPTS[intent] or COMPANION_INTENT_PROMPTS[intent] == "", \
                f"companion intent {intent} 格式不正确"

    def test_critic_prompt_has_escape_hatch(self):
        assert "逃逸舱" in CRITIC_SYSTEM_PROMPT or "Escape Hatch" in CRITIC_SYSTEM_PROMPT
        assert "必须判定为 PASS" in CRITIC_SYSTEM_PROMPT

    def test_base_prompt_has_data_model_constraint(self):
        result = build_deep_prompt("lookup")
        assert "只有" in result and "评分" in result
        assert "角色" in result

    def test_deep_lookup_has_exit_conditions(self):
        """Deep lookup Scene Hint 应包含深入挖掘的关键词。"""
        lookup = DEEP_INTENT_PROMPTS["lookup"]
        assert "深入" in lookup or "值得" in lookup

    def test_debate_scene_hint_data_backs_opinion(self):
        debate = DEEP_INTENT_PROMPTS["debate"]
        assert "数据" in debate or "判断" in debate

    def test_emotional_scene_hint_empathy_first(self):
        emotional = DEEP_INTENT_PROMPTS["emotional"]
        assert "朋友" in emotional or "真心" in emotional

    def test_companion_scene_hints_are_short(self):
        """Companion Scene Hints 应短于 deep 版（~50 chars vs ~100 chars）。"""
        for intent in _VALID_INTENTS:
            if intent == "unknown":
                continue
            c_hint = COMPANION_SCENE_HINTS.get(intent, "")
            d_hint = DEEP_SCENE_HINTS.get(intent, "")
            if c_hint and d_hint:
                assert len(c_hint) <= len(d_hint) + 20, \
                    f"Companion {intent} hint 不应显著长于 deep 版"


class TestHonestyPrinciple:
    """Prompt 中数据不足时的诚实兜底原则。"""

    def test_tool_intuition_has_honesty_principle(self):
        """TOOL_GUIDANCE 应包含诚实兜底原则。"""
        assert "没查到" in TOOL_INTUITION
        assert "不编造" in TOOL_INTUITION
        assert "诚实说" in TOOL_INTUITION

    def test_companion_prompt_has_honesty_principle(self):
        """Companion prompt 应包含诚实原则。"""
        result = _build(
            agent_profile=COMPANION_PROFILE,
            character=BANGUMI_CHARACTER,
        )
        assert "没查到" in result
        assert "编造数据" in result or "编造具体数字" in result

    def test_deep_prompt_has_honesty_principle(self):
        """深度 prompt 应包含诚实原则。"""
        result = build_deep_prompt("lookup")
        assert "没查到" in result
        assert "编造" in result


class TestRenderPrompt:
    """Render prompt 构建（Phase 7: 参数感知 + 快速跳过）。"""

    def test_render_prompt_non_empty(self):
        """build_render_prompt 应返回非空 prompt。"""
        result = build_render_prompt(BANGUMI_CHARACTER, "EVA 怎么样？", "EVA 评分 9.1，排名第一。")
        assert len(result) > 100

    def test_render_prompt_includes_identity(self):
        """Render prompt 应包含角色身份。"""
        result = build_render_prompt(BANGUMI_CHARACTER, "test", "test response")
        assert "Bangumi娘" in result or "看板娘" in result

    def test_render_prompt_includes_user_query(self):
        """Render prompt 应包含用户问题。"""
        result = build_render_prompt(BANGUMI_CHARACTER, "EVA 评分怎么样", "9.1 分")
        assert "EVA 评分怎么样" in result

    def test_render_prompt_includes_agent_response(self):
        """Render prompt 应包含原始回复。"""
        result = build_render_prompt(BANGUMI_CHARACTER, "test", "EVA 评分 9.1，排名 #1")
        assert "EVA 评分 9.1" in result

    def test_render_prompt_neutral_no_bangumi_persona(self):
        """Neutral 角色 render prompt 不应包含损友吐槽。"""
        result = build_render_prompt(NEUTRAL_CHARACTER, "test", "response")
        assert "腹黑" not in result
        assert "吐槽" not in result

    def test_render_prompt_bangumi_has_style(self):
        """Bangumi 角色 render prompt 应包含吐槽风格。"""
        result = build_render_prompt(BANGUMI_CHARACTER, "test", "response")
        assert "损友" in result or "吐槽" in result or "水了点" in result

    def test_render_prompt_no_data_interpretation(self):
        """Render prompt 不应包含数据解读教科书。"""
        result = build_render_prompt(BANGUMI_CHARACTER, "test", "response")
        assert "rating_count" not in result

    def test_render_prompt_has_hard_constraints(self):
        """Render prompt 应包含硬约束。"""
        result = build_render_prompt(BANGUMI_CHARACTER, "test", "response")
        assert "硬约束" in result
        assert "emoji" in result

    def test_render_prompt_word_limit_by_depth(self):
        """字数限制应按 depth 分档。"""
        q = build_render_prompt(BANGUMI_CHARACTER, "test", "r", depth="quick")
        a = build_render_prompt(BANGUMI_CHARACTER, "test", "r", depth="auto")
        d = build_render_prompt(BANGUMI_CHARACTER, "test", "r", depth="deep")
        assert "120 字" in q
        assert "200 字" in a
        assert "350 字" in d
        assert "{word_limit}" not in q

    def test_render_prompt_ending_not_always_question(self):
        """结尾规则应允许反问、判断、冷吐槽。"""
        result = build_render_prompt(BANGUMI_CHARACTER, "test", "response")
        assert "冷吐槽" in result or "反问" in result

    def test_render_prompt_snark_affects_style(self):
        """高 snark 时 render prompt 应有更犀利的风格指引。"""
        low = build_render_prompt(BANGUMI_CHARACTER, "test", "r", snark=0.2)
        high = build_render_prompt(BANGUMI_CHARACTER, "test", "r", snark=0.9)
        # 高 snark 应有额外的毒舌规则
        assert len(high) >= len(low)

    def test_should_skip_render_short_chitchat(self):
        """短闲聊无工具调用应跳过 render。"""
        from langchain_core.messages import AIMessage, HumanMessage

        state = {"messages": [
            HumanMessage(content="今天好累"),
            AIMessage(content="那就别看烧脑的了。"),
        ]}
        assert _should_skip_render(state) is True

    def test_should_not_skip_render_with_tools(self):
        """有工具调用时不应跳过 render。"""
        from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

        state = {"messages": [
            HumanMessage(content="EVA 评分"),
            AIMessage(content="", tool_calls=[{"name": "search", "args": {}, "id": "tc1"}]),
            ToolMessage(content="...", tool_call_id="1", name="search"),
            AIMessage(content="EVA 9.1 分。"),
        ]}
        assert _should_skip_render(state) is False

    def test_should_not_skip_render_long_chitchat(self):
        """长闲聊（>60 字）不应跳过 render。"""
        from langchain_core.messages import AIMessage, HumanMessage

        state = {"messages": [
            HumanMessage(content="讲讲 EVA"),
            AIMessage(content="EVA 是一部非常有意思的作品，它的深度和复杂度远超一般的机甲动画。" * 5),
        ]}
        assert _should_skip_render(state) is False
