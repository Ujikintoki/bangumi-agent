"""
短期记忆管理测试

覆盖 count_tokens、estimate_tokens、trim_messages、manage_memory。
可独立运行: python -m pytest test/test_memory.py -v
"""

from __future__ import annotations

import tiktoken
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agent.memory import count_tokens, estimate_tokens, manage_memory, trim_messages


class TestCountTokens:
    """count_tokens — 精确 Token 计数"""

    def test_english_text(self):
        tokens = count_tokens("Hello world")
        assert tokens > 0
        # "Hello world" 对于 cl100k_base 应该是 2 tokens
        assert tokens == 2

    def test_chinese_text(self):
        tokens = count_tokens("你好世界")
        assert tokens > 0
        # 中文字符通常每个占 1.5-2.5 tokens
        # "你好世界" 4 字符，应该 > 4
        assert tokens >= 4

    def test_mixed_text(self):
        tokens = count_tokens("Hello 你好 world 世界")
        assert tokens > 0
        assert tokens > 6  # 至少每个单词/字一个 token

    def test_empty_string(self):
        assert count_tokens("") == 0

    def test_long_text_proportional(self):
        """长文本 Token 数大致正比于文本长度"""
        short = count_tokens("hello")
        long = count_tokens("hello " * 100)
        # 100 倍文本长度应接近 100 倍 token 数
        assert long > short * 50

    def test_same_as_direct_tiktoken(self):
        """与直接用 tiktoken 编码结果一致"""
        text = "Bangumi 评分 8.7，排名 #15"
        direct = len(tiktoken.get_encoding("cl100k_base").encode(text))
        assert count_tokens(text) == direct


class TestEstimateTokens:
    """estimate_tokens — 多消息类型 Token 估算"""

    def test_single_human_message(self):
        msgs = [HumanMessage(content="你好")]
        tokens = estimate_tokens(msgs)
        assert tokens > 0

    def test_multiple_message_types(self):
        msgs = [
            SystemMessage(content="You are Bangumi assistant."),
            HumanMessage(content="搜进击的巨人"),
            AIMessage(content="", tool_calls=[{"name": "search", "args": {"keyword": "巨人"}, "id": "c1"}]),
            ToolMessage(content="找到 3 个结果", tool_call_id="c1"),
            AIMessage(content="进击的巨人评分 8.7"),
        ]
        tokens = estimate_tokens(msgs)
        assert tokens > 0
        # 至少应该大于每条消息 1 token
        assert tokens >= 5

    def test_empty_list(self):
        assert estimate_tokens([]) == 0

    def test_increasing_with_content_length(self):
        short = estimate_tokens([HumanMessage(content="hi")])
        long = estimate_tokens([HumanMessage(content="这是一段很长的中文文本 " * 50)])
        assert long > short

    def test_aimessage_with_list_content(self):
        """AIMessage content 为 list[dict] 时也能正确计数"""
        msg = AIMessage(content=[{"type": "text", "text": "hello"}])
        tokens = estimate_tokens([msg])
        assert tokens > 0


class TestTrimMessages:
    """trim_messages — 滑动窗口截断"""

    def test_preserves_system_message(self):
        messages = [
            SystemMessage(content="You are Bangumi assistant."),
            HumanMessage(content="Q1"),
            AIMessage(content="A1"),
            HumanMessage(content="Q2"),
            AIMessage(content="A2"),
        ]
        trimmed = trim_messages(messages, max_tokens=20)
        assert any(isinstance(m, SystemMessage) for m in trimmed)

    def test_trims_from_head(self):
        """旧消息从头部截断，最近消息保留"""
        messages = [
            SystemMessage(content="You are Bangumi assistant."),
            HumanMessage(content="第一条旧消息" * 10),
            AIMessage(content="第一条回复" * 10),
            HumanMessage(content="第二条旧消息" * 10),
            AIMessage(content="第二条回复" * 10),
            HumanMessage(content="最新消息"),
        ]
        trimmed = trim_messages(messages, max_tokens=30)
        # 最新消息应该被保留
        assert any("最新消息" in m.content for m in trimmed if isinstance(m, HumanMessage))

    def test_no_truncation_when_under_budget(self):
        messages = [
            SystemMessage(content="System"),
            HumanMessage(content="Hi"),
            AIMessage(content="Hello"),
        ]
        trimmed = trim_messages(messages, max_tokens=10000)
        assert len(trimmed) == len(messages)

    def test_system_message_alone_exceeds_budget(self):
        """极端情况：系统消息本身就超过预算"""
        huge_system = SystemMessage(content="x" * 100000)  # 远超预算
        messages = [huge_system, HumanMessage(content="Hi")]
        trimmed = trim_messages(messages, max_tokens=100)
        # SystemMessage 始终保留（即使超预算）
        assert any(isinstance(m, SystemMessage) for m in trimmed)

    def test_returns_same_type(self):
        messages = [
            SystemMessage(content="System"),
            HumanMessage(content="Hi"),
        ]
        trimmed = trim_messages(messages, max_tokens=10000)
        assert isinstance(trimmed, list)

    def test_large_message_list(self):
        """大量消息时正确截断"""
        messages = [SystemMessage(content="You are an assistant.")]
        # 添加 50 对 Human + AI 消息
        for i in range(50):
            messages.append(HumanMessage(content=f"问题 {i}: 这是一段比较长的消息 " * 3))
            messages.append(AIMessage(content=f"回答 {i}: 这也是一段比较长的回复 " * 3))
        original_count = len(messages)
        trimmed = trim_messages(messages, max_tokens=500)
        # 应该截断了部分消息
        assert len(trimmed) < original_count
        # SystemMessage 仍然存在
        assert any(isinstance(m, SystemMessage) for m in trimmed)
        # 最近的消息保留
        assert len(trimmed) >= 1


class TestManageMemory:
    """manage_memory — 记忆管理入口"""

    def test_no_truncation_when_under_budget(self):
        messages = [
            SystemMessage(content="System"),
            HumanMessage(content="Hi"),
        ]
        result = manage_memory(messages, max_tokens=10000)
        assert result is messages  # 原样返回

    def test_truncation_when_over_budget(self):
        messages = [
            SystemMessage(content="System"),
            HumanMessage(content="长文本" * 500),  # 大量 token
            AIMessage(content="回复"),
        ]
        result = manage_memory(messages, max_tokens=50)
        assert result is not messages  # 新列表（截断了）
        assert any(isinstance(m, SystemMessage) for m in result)

    def test_empty_messages(self):
        result = manage_memory([], max_tokens=8000)
        assert result == []

    def test_default_max_tokens(self):
        messages = [HumanMessage(content="Hi")]
        result = manage_memory(messages)  # 使用默认 8000
        assert result is messages  # 远未超限
