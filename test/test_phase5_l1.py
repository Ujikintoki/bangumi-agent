"""
Phase 5 L1 短记忆模块 — 验证测试

验证 manage_memory 入口后移后的核心行为：
1. 预算常量正确
2. 完整消息列表截断（含 fat SystemPrompt 模拟 L2 注入）
3. Dialogue 预算 4000
4. tiktoken 容错

运行: python -m pytest test/test_phase5_l1.py -v
"""

from __future__ import annotations

import json
import logging
import sys
from unittest import mock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agent.memory.short_term import (
    DEFAULT_MAX_TOKENS,
    DEPTH_TOKEN_BUDGETS,
    L2_MEMORY_BUDGET_TOKENS,
    count_tokens,
    estimate_tokens,
    manage_memory,
    trim_messages,
)


class TestBudgetConstants:
    """预算常量定义验证（Phase 8: 按 depth 分级）"""

    def test_default_max_is_10000(self):
        assert DEFAULT_MAX_TOKENS == 10000

    def test_depth_budgets_structure(self):
        """DEPTH_TOKEN_BUDGETS 包含两种模式且 deep > fast"""
        assert DEPTH_TOKEN_BUDGETS["fast"] == 10000
        assert DEPTH_TOKEN_BUDGETS["deep"] == 16000
        assert DEPTH_TOKEN_BUDGETS["deep"] > DEPTH_TOKEN_BUDGETS["fast"]

    def test_l2_budget_is_500(self):
        assert L2_MEMORY_BUDGET_TOKENS == 500

    def test_l2_budget_equals_config_default(self):
        """确保 memory.py 常量与 config.py Settings 默认值一致，防止两处定义漂移。"""
        from core.config import Settings
        assert L2_MEMORY_BUDGET_TOKENS == Settings().MEMORY_MAX_INJECT_TOKENS


class TestCompleteMessageListTruncation:
    """验证核心改动：manage_memory 感知完整消息列表（含 fat SystemPrompt）

    模拟 L2 记忆注入后的场景：SystemPrompt 比原来大 500 tokens，
    manage_memory 应在完整列表上截断，确保总 budget 不超标。
    """

    # 构造一个接近真实大小的 BASE + intent prompt（~1200 tokens）
    _REALISTIC_SYSTEM_PROMPT = (
        '你是 Bangumi 助手，一个专注于二次元和ACGN作品的AI。'
        '你掌握的领域包括但不限于动漫、漫画、音乐、游戏和二次元。\n\n'
        '## 你的能力\n\n'
        '1. **API 查询**：获取 Bangumi 站内的实时数据（评论、热度、放送排期、角色声优、用户画像等）\n'
        '2. **语义搜索**：通过本地 RAG 数据库发现作品（支持模糊描述如「80年代黑暗机战番」）\n'
        '3. **常识推理**：基于训练知识回答动漫/漫画/音乐/游戏领域的问题\n\n'
        '## 回答风格\n\n'
        '- 简洁、具体、可操作\n'
        '- 提到番剧时附带评分和简短描述\n'
        '- 如果信息不足，主动建议下一步可以做什么\n'
        '- 用中文回复\n'
        '- **每部作品优先使用中文名**（如工具返回的 name_cn 非空则用中文名），无中文名时用日文原名\n\n'
        '## 输出格式规则（必须遵守）\n\n'
        '- **禁止使用 Markdown 表格**（你的输出是纯文本终端，表格不渲染）\n'
        '- 列表使用 `- ` 或 `1. ` 开头，每行一条\n'
        '- 每部作品格式：`中文名（日文名）— ⭐评分 | 补充信息`\n'
        '- 评分缺失时写 `暂无评分`，不要留空或写 `—`\n\n'
        + ('附加指引文本以模拟真实 SystemPrompt 大小 ' * 15)
    )

    # 模拟 L2 记忆注入文本（~500 tokens）
    _L2_MEMORY_CONTEXT = (
        '## 用户历史\n\n'
        '你之前和该用户有过以下相关对话：\n\n'
        '- [2天前] 用户询问了「类似星际牛仔的番剧」，你推荐了《混沌武士》《黑之契约者》'
        '《ACCA13区监察课》。用户对《混沌武士》表现出兴趣。\n'
        '- [5天前] 用户搜索了「80年代机器人动画」，你推荐了《机动战士高达》'
        '《超时空要塞》《装甲骑兵》。用户偏好硬科幻机战。\n\n'
        '**用户偏好摘要**：喜欢科幻/机战类型，偏好80-90年代作品，倾向于高分条目（≥7.5）。\n\n'
        '请结合以上历史信息回答当前问题。如果历史和当前问题无关，可以忽略。\n'
        + ('（模拟 L2 记忆填充内容以占用约 500 tokens）' * 8)
    )

    def _build_messages_for_llm(self, system_prompt: str, conversation: list) -> list:
        """模拟 reasoning_node 构建 messages_for_llm 的完整过程。

        Step 2-3: build_system_prompt → [新 SystemMessage] + raw state messages
        Step 3.5: manage_memory(messages_for_llm, max_tokens)
        """
        messages_for_llm = [SystemMessage(content=system_prompt)]
        for m in conversation:
            if isinstance(m, SystemMessage):
                continue
            messages_for_llm.append(m)
        return messages_for_llm

    def test_fat_system_prompt_respected_in_budget(self):
        """SystemPrompt 含 L2 记忆注入时，总 tokens 不超预算。

        模拟：新 SystemPrompt = BASE + L2 记忆（~1700 tokens），
        外加大量对话历史。manage_memory 应在完整列表上截断。
        """
        full_prompt = self._REALISTIC_SYSTEM_PROMPT + "\n" + self._L2_MEMORY_CONTEXT
        prompt_tokens = count_tokens(full_prompt)

        # 构造超长对话历史（远超剩余预算）
        conversation = []
        for i in range(40):
            conversation.append(
                HumanMessage(content=f"用户第{i}轮问题: 这是一段比较长的对话历史消息 " * 8)
            )
            conversation.append(
                AIMessage(content=f"助手第{i}轮回复: 这也是一段比较长的回复内容包含作品名称评分等信息 " * 8)
            )

        messages_for_llm = self._build_messages_for_llm(full_prompt, conversation)
        raw_total = estimate_tokens(messages_for_llm)
        assert raw_total > DEFAULT_MAX_TOKENS, (
            f"原始消息应超预算: {raw_total} > {DEFAULT_MAX_TOKENS}"
        )

        # --- 核心验证：manage_memory 在完整列表上截断 ---
        trimmed = manage_memory(messages_for_llm, max_tokens=DEFAULT_MAX_TOKENS)
        trimmed_total = estimate_tokens(trimmed)

        assert trimmed_total <= DEFAULT_MAX_TOKENS, (
            f"截断后总 tokens {trimmed_total} 应 ≤ 预算 {DEFAULT_MAX_TOKENS}"
        )

        # SystemMessage（含 L2 记忆）应完整保留
        sys_msgs = [m for m in trimmed if isinstance(m, SystemMessage)]
        assert len(sys_msgs) == 1
        assert self._L2_MEMORY_CONTEXT[:30] in sys_msgs[0].content, (
            "L2 记忆上下文应保留在 SystemPrompt 中"
        )

    def test_larger_system_prompt_means_less_history_budget(self):
        """SystemPrompt 越大 → 对话历史预算越少。

        对比：相同对话历史下，fat prompt vs slim prompt，
        fat prompt 版本截断后保留的对话轮数应更少。
        """
        conversation = []
        for i in range(30):
            conversation.append(HumanMessage(content=f"用户问题{i}: " + "搜索推荐番剧 " * 10))
            conversation.append(AIMessage(content=f"助手回复{i}: " + "推荐作品详情评分介绍 " * 10))

        # Fat prompt（含 L2 记忆）
        fat_prompt = self._REALISTIC_SYSTEM_PROMPT + "\n" + self._L2_MEMORY_CONTEXT
        fat_messages = self._build_messages_for_llm(fat_prompt, conversation)
        fat_trimmed = manage_memory(fat_messages, max_tokens=DEFAULT_MAX_TOKENS)

        # Slim prompt（不含 L2 记忆）
        slim_messages = self._build_messages_for_llm(self._REALISTIC_SYSTEM_PROMPT, conversation)
        slim_trimmed = manage_memory(slim_messages, max_tokens=DEFAULT_MAX_TOKENS)

        fat_non_sys = [m for m in fat_trimmed if not isinstance(m, SystemMessage)]
        slim_non_sys = [m for m in slim_trimmed if not isinstance(m, SystemMessage)]

        fat_total = estimate_tokens(fat_trimmed)
        slim_total = estimate_tokens(slim_trimmed)
        assert fat_total <= DEFAULT_MAX_TOKENS and slim_total <= DEFAULT_MAX_TOKENS

        assert len(fat_non_sys) <= len(slim_non_sys), (
            f"fat prompt 对话轮数 ({len(fat_non_sys)}) 应 ≤ slim prompt 对话轮数 ({len(slim_non_sys)})"
        )
        logger = logging.getLogger("bgm-agent.memory")
        logger.info(
            "Fat prompt (%d tokens) → %d 条对话; Slim prompt (%d tokens) → %d 条对话",
            count_tokens(fat_prompt), len(fat_non_sys),
            count_tokens(self._REALISTIC_SYSTEM_PROMPT), len(slim_non_sys),
        )

    def test_system_message_never_dropped(self):
        """无论 SystemPrompt 多大，SystemMessage 始终保留。

        这是 trim_messages 的核心策略——系统提示词不可丢失。
        """
        huge_prompt = "X" * 10000  # 远超预算
        messages_for_llm = self._build_messages_for_llm(
            huge_prompt,
            [HumanMessage(content="Hi"), AIMessage(content="Hello")],
        )
        trimmed = manage_memory(messages_for_llm, max_tokens=500)
        sys_msgs = [m for m in trimmed if isinstance(m, SystemMessage)]
        assert len(sys_msgs) == 1, "SystemMessage 应始终保留"

    def test_conversation_truncated_when_system_is_huge(self):
        """SystemPrompt 本身超预算时，对话历史被全部截断。

        trim_messages 的设计决策：SystemMessage 始终保留（即使超预算），
        因为它携带了 L2 记忆上下文的完整信息，丢失会导致上下文断裂。
        对话历史被全部丢弃。
        """
        huge_prompt = "记忆中包含大量上下文 " * 400  # ~1600+ tokens
        conversation = [
            HumanMessage(content="Q1: " + "内容 " * 100),
            AIMessage(content="A1: " + "内容 " * 100),
        ]
        messages_for_llm = self._build_messages_for_llm(huge_prompt, conversation)
        trimmed = manage_memory(messages_for_llm, max_tokens=500)

        # SystemMessage 始终保留（设计决策）
        sys_msgs = [m for m in trimmed if isinstance(m, SystemMessage)]
        assert len(sys_msgs) == 1

        # 对话历史被丢弃（预算全给 SystemMessage 了）
        non_sys = [m for m in trimmed if not isinstance(m, SystemMessage)]
        assert len(non_sys) == 0, (
            f"SystemPrompt 本身超预算时应丢弃所有对话历史，"
            f"实际保留了 {len(non_sys)} 条"
        )


class TestDialogueBudget:
    """Dialogue Agent 专用预算 4000 tokens"""

    def test_depth_budget_less_for_fast(self):
        """fast 预算 10000 < deep 16000，同等对话历史时应截断更多"""
        conversation = []
        for i in range(25):
            conversation.append(HumanMessage(content=f"问题{i}: 查询番剧数据 " * 6))
            conversation.append(AIMessage(content=f"回复{i}: 推荐作品评分介绍 " * 6))

        sys_prompt = "Bangumi娘 System Prompt " * 40  # ~200 tokens

        messages = [SystemMessage(content=sys_prompt)]
        for m in conversation:
            if not isinstance(m, SystemMessage):
                messages.append(m)

        deep_trimmed = manage_memory(messages.copy(), max_tokens=DEPTH_TOKEN_BUDGETS["deep"])
        fast_trimmed = manage_memory(messages.copy(), max_tokens=DEPTH_TOKEN_BUDGETS["fast"])

        assert estimate_tokens(deep_trimmed) <= DEPTH_TOKEN_BUDGETS["deep"]
        assert estimate_tokens(fast_trimmed) <= DEPTH_TOKEN_BUDGETS["fast"]

        # fast 应该保留更少对话历史
        d_count = len([m for m in deep_trimmed if not isinstance(m, SystemMessage)])
        f_count = len([m for m in fast_trimmed if not isinstance(m, SystemMessage)])
        assert f_count <= d_count, (
            f"fast 对话数 ({f_count}) 应 ≤ deep 对话数 ({d_count})"
        )


class TestTiktokenFaultTolerance:
    """tiktoken 初始化失败时优雅降级"""

    def test_count_tokens_fallback_on_encoder_none(self):
        """模拟 _ENCODER 为 None，count_tokens 回退到 len//2 估算"""
        text = "这是一段中文测试文本用于验证回退逻辑"

        # 正常结果
        normal = count_tokens(text)
        assert normal > 0

        # 模拟 encoder 为 None
        with mock.patch("agent.memory.short_term._ENCODER", None):
            fallback = count_tokens(text)
            assert fallback == max(1, len(text) // 2)
            assert fallback >= normal * 0.3  # 不应偏差过大（中文约 0.5x）

    def test_truncate_text_by_tokens_fallback(self):
        """模拟 _ENCODER 为 None，截断回退到字符截断"""
        from agent.memory.short_term import _truncate_text_by_tokens

        text = "A" * 500
        with mock.patch("agent.memory.short_term._ENCODER", None):
            result = _truncate_text_by_tokens(text, max_tokens=10)
            # 字符回退: max_tokens * 2 = 20 chars
            assert len(result) <= 21  # 带截断标记可能略超
            assert result.startswith("A" * 20)

    def test_manage_memory_works_without_tiktoken(self):
        """manage_memory 在 encoder=None 时不崩溃"""
        messages = [
            SystemMessage(content="System Prompt"),
            HumanMessage(content="用户问题: 搜索番剧 " * 20),
            AIMessage(content="助手回复: 推荐列表 " * 20),
        ]

        with mock.patch("agent.memory.short_term._ENCODER", None):
            result = manage_memory(messages, max_tokens=200)
            assert isinstance(result, list)
            assert any(isinstance(m, SystemMessage) for m in result)
            # 回退估算下截断仍生效
            assert len(result) <= len(messages)


class TestDigestionGuidanceInBudget:
    """消化态引导指令参与 Token 预算计算

    确保消化态 HumanMessage 在 manage_memory 之前追加，参与截断。
    """

    def test_digestion_hint_included_in_budget(self):
        """消化态引导指令在 manage_memory 前追加 → 参与预算 → 尾部优先保留"""
        digestion_hint = HumanMessage(
            content=(
                "（系统指令：以上是工具返回的数据。请综合这些信息回答用户的问题。"
                "如果当前数据足以回答，直接生成文字回复；"
                "如果确实需要更多数据，可以继续调用必要的工具。）"
            )
        )

        system_prompt = "你是 Bangumi 助手 " * 60  # ~300 tokens
        conversation = [
            HumanMessage(content="用户问题 " * 20),
            AIMessage(
                content="工具调用回复 " * 20,
                tool_calls=[{"name": "search", "args": {}, "id": "c1"}],
            ),
            ToolMessage(content="工具返回数据 " * 30, tool_call_id="c1"),
        ]

        # 模拟 reasoning_node Step 3 + 消化态引导 + Step 3.5
        messages_for_llm = [SystemMessage(content=system_prompt)]
        for m in conversation:
            if not isinstance(m, SystemMessage):
                messages_for_llm.append(m)
        messages_for_llm.append(digestion_hint)

        trimmed = manage_memory(messages_for_llm, max_tokens=1200)

        assert estimate_tokens(trimmed) <= 1200
        # 消化态引导应在截断后的列表中（它是最新消息，trim_messages 尾部优先保留）
        found = any(
            "系统指令：以上是工具返回的数据" in (m.content if hasattr(m, "content") else "")
            for m in trimmed
        )
        if not found:
            # 诊断输出
            contents = [
                (type(m).__name__, (m.content if hasattr(m, "content") else str(m))[:80])
                for m in trimmed
            ]
            pytest.fail(f"消化态引导指令应被保留。截断后消息列表: {contents}")
        assert found, "消化态引导指令应被保留（它在列表尾部）"


class TestToolResultCompression:
    """Phase 8: 历史 ToolMessage 压缩为关键字段摘要。"""

    def test_compress_search_result_reduces_size(self):
        """search_bangumi_subject 的 dict 结果应被压缩为关键字段。"""
        from agent.memory.short_term import _compress_tool_result

        # 模拟 search_bangumi_subject 返回
        content = json.dumps({
            "results": [
                {
                    "id": 876, "name": "Bakemonogatari", "name_cn": "化物语",
                    "type": "TV", "date": "2009-07-03",
                    "rating": {"score": 8.4, "rank": 61, "count": 15000},
                    "summary": "长篇大论的摘要文本 " * 50,  # padding
                },
                {
                    "id": 1234, "name": "Nisemonogatari", "name_cn": "伪物语",
                    "type": "TV", "date": "2012-01-07",
                    "rating": {"score": 8.2, "rank": 120},
                    "summary": "更多摘要 " * 50,
                },
            ]
        })
        msg = ToolMessage(content=content, tool_call_id="t1", name="search_bangumi_subject")
        compressed = _compress_tool_result(msg)

        assert compressed is not msg  # 新对象
        assert "[已压缩]" in compressed.content
        assert "化物语" in compressed.content
        assert "⭐8.4" in compressed.content
        assert "#61" in compressed.content
        assert "伪物语" in compressed.content
        # 应远小于原始
        assert count_tokens(compressed.content) < count_tokens(content) // 3

    def test_compress_skips_current_round(self):
        """当前轮的 ToolMessage 保留完整——只有上一轮的才压缩。"""
        messages = [
            SystemMessage(content="You are Bangumi娘"),
            HumanMessage(content="EVA 评分怎么样"),
            AIMessage(content="", tool_calls=[{"name": "search", "args": {}, "id": "c1"}]),
            ToolMessage(
                content=json.dumps({"results": [
                    {"id": 1, "name": "EVA", "name_cn": "新世纪福音战士",
                     "type": "TV", "rating": {"score": 9.1, "rank": 1}}
                ]}),
                tool_call_id="c1", name="search_bangumi_subject",
            ),
            # R2: 新问题——之前的 ToolMessage 变为"上一轮"
            HumanMessage(content="那排名呢"),
        ]

        from agent.memory.short_term import _compress_previous_round_tools
        result = _compress_previous_round_tools(messages)

        # EVA 的 ToolMessage 在第二个人类消息之前 → 应被压缩
        tool_msgs = [m for m in result if isinstance(m, ToolMessage)]
        assert len(tool_msgs) == 1
        assert "[已压缩]" in tool_msgs[0].content

    def test_compress_person_detail(self):
        """get_person_detail 应保留前 400 tokens。"""
        from agent.memory.short_term import _compress_tool_result

        content = "person detail data " * 200  # long content
        msg = ToolMessage(content=content, tool_call_id="t1", name="get_person_detail")

        compressed = _compress_tool_result(msg)
        assert count_tokens(compressed.content) <= 450  # 400 + marker
        assert "[已压缩]" in compressed.content

    def test_compress_empty_content_noop(self):
        """空 content 的 ToolMessage 不压缩。"""
        from agent.memory.short_term import _compress_tool_result

        msg = ToolMessage(content="", tool_call_id="t1", name="search_bangumi_subject")
        result = _compress_tool_result(msg)
        assert result is msg  # 返回原对象

    def test_manage_memory_includes_compression(self):
        """manage_memory 应在截断前先压缩历史工具结果。"""
        fat_search = json.dumps({"results": [
            {"id": i, "name": f"Anime{i}", "type": "TV",
             "rating": {"score": 7.5, "rank": i * 10},
             "summary": "padding " * 100}
            for i in range(10)
        ]})

        messages = [
            SystemMessage(content="System prompt " * 80),
            HumanMessage(content="R1: 推荐动画"),
            AIMessage(content="", tool_calls=[{"name": "search", "args": {}, "id": "c1"}]),
            ToolMessage(content=fat_search, tool_call_id="c1", name="search_bangumi_subject"),
            AIMessage(content="这里是推荐列表……"),
            HumanMessage(content="R2: 第一部评分怎么样"),
        ]

        result = manage_memory(messages, max_tokens=DEPTH_TOKEN_BUDGETS["fast"])
        assert estimate_tokens(result) <= DEPTH_TOKEN_BUDGETS["fast"]

        # 压缩后的 R1 ToolMessage 应保留关键信息
        tool_msgs = [m for m in result if isinstance(m, ToolMessage)]
        compressed = [m for m in tool_msgs if "[已压缩]" in m.content]
        assert len(compressed) >= 1
        assert "⭐7.5" in compressed[0].content  # 关键数据保留
