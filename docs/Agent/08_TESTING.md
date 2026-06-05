# 08 — 测试策略

## 测试层级

```
Layer 1: 节点单元测试 (mock LLM)
   ├─ reasoning_node: 意图分类 + mock LLM 返回 → 验证 query_intent + last_tool_calls
   ├─ tool_node: mock 工具 → 验证 ToolMessage 格式
   ├─ critic_node: mock LLM → 验证定向反馈格式
   └─ memory_node: 验证滑动窗口截断

Layer 2: 图谱集成测试 (mock LLM + 内存工具)
   ├─ "你好" → intent=chitchat → 不触发工具 → PASS → END
   ├─ "搜进击的巨人" → intent=lookup → 触发 search → critic REVISE → 再推理 → PASS
   ├─ Critic REVISE + feedback → reasoning接收 feedback → 定向修正 → PASS
   ├─ 错误路径: 3 轮 REVISE → 熔断 → error_flag=True
   └─ 滑动窗口: 大量消息 → 截断 → 继续推理

Layer 3: 端到端测试 (真实 LLM + 真实 API, 可选)
   └─ 完整对话: "海贼王最新一集评价" → 调 API → 返回分析
```

---

## Layer 1: 意图分类器测试

```python
# test/test_agent.py

import pytest
from agent.nodes import classify_intent_rule

class TestIntentClassifier:
    """意图分类器 — 规则层"""

    @pytest.mark.parametrize("message,expected", [
        ("你好", "chitchat"),
        ("谢谢你的帮助", "chitchat"),
        ("嗨", "chitchat"),
        ("hello", "chitchat"),
        ("晚安", "chitchat"),
    ])
    def test_classify_chitchat(self, message, expected):
        assert classify_intent_rule(message) == expected

    @pytest.mark.parametrize("message,expected", [
        ("什么是三集定律", "factual"),
        ("顶上战争是哪两方", "factual"),
        ("解释一下作画崩坏", "factual"),
    ])
    def test_classify_factual(self, message, expected):
        assert classify_intent_rule(message) == expected

    @pytest.mark.parametrize("message,expected", [
        ("进击的巨人评分", "lookup"),
        ("找一下命运石之门", "lookup"),
        ("搜索鬼灭之刃", "lookup"),
        ("查一下这个番的声优", "lookup"),
    ])
    def test_classify_lookup(self, message, expected):
        assert classify_intent_rule(message) == expected

    @pytest.mark.parametrize("message,expected", [
        ("类似命运石之门的烧脑番", "discovery"),
        ("推荐几部好看的机战番", "discovery"),
        ("还有什么类似的作品", "discovery"),
        ("评分最高的冷门番", "discovery"),
    ])
    def test_classify_discovery(self, message, expected):
        assert classify_intent_rule(message) == expected

    @pytest.mark.parametrize("message,expected", [
        ("今天放什么番", "realtime"),
        ("本周新番排期", "realtime"),
        ("最近什么番比较火", "realtime"),
        ("最近流行什么", "realtime"),
    ])
    def test_classify_realtime(self, message, expected):
        assert classify_intent_rule(message) == expected

    def test_classify_short_message_defaults_to_chitchat(self):
        """短消息默认归类为 chitchat"""
        assert classify_intent_rule("嗯") == "chitchat"
        assert classify_intent_rule("好") == "chitchat"

    def test_classify_unknown_falls_back_to_none(self):
        """无法分类的消息返回 None（触发 LLM fallback）"""
        assert classify_intent_rule("这个番的画风怎么样和那个比") is None

    def test_priority_queue_composite_before_simple(self):
        """优先级队列：复合意图不被简单意图的关键词劫持"""
        # discovery 的"找...番"模式优先于 lookup 的"找"
        assert classify_intent_rule("找类似命运石之门的番") == "discovery"
        assert classify_intent_rule("帮我找和进击的巨人差不多的番") == "discovery"
        assert classify_intent_rule("推荐冷门机战番") == "discovery"
        # realtime 的"最近"优先于其他
        assert classify_intent_rule("最近评分最高的番") == "realtime"
        # lookup 在复合关键词不匹配时才命中
        assert classify_intent_rule("找进击的巨人评分") == "lookup"
        assert classify_intent_rule("查命运石之门声优") == "lookup"
```

## Layer 1: reasoning_node 测试

```python
from unittest.mock import MagicMock, patch
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, ToolCall

class TestReasoningNode:
    """reasoning_node — LLM function-calling"""

    @pytest.fixture
    def base_state(self):
        return {
            "messages": [
                SystemMessage(content="You are Bangumi assistant."),
                HumanMessage(content="搜进击的巨人"),
            ],
            "iterations": 0,
            "critic_status": "PENDING",
            "critic_feedback": "",
            "last_tool_calls": [],
            "query_intent": "unknown",
            "session_id": "test",
            "user_id": "test-user",
            "error_flag": False,
        }

    def test_detects_tool_need(self, base_state, mock_llm):
        """有工具调用时返回 last_tool_calls"""
        mock_llm.bind_tools.return_value.invoke.return_value = AIMessage(
            content="",
            tool_calls=[{"name": "search_bangumi_subject", "args": {"keyword": "巨人"}, "id": "call_1"}]
        )
        result = reasoning_node(base_state)
        assert result["iterations"] == 1
        assert len(result["last_tool_calls"]) == 1
        assert result["query_intent"] == "lookup"

    def test_no_tool_for_chitchat(self, base_state, mock_llm):
        """chitchat 意图不绑定工具"""
        base_state["messages"][1] = HumanMessage(content="你好")
        result = reasoning_node(base_state)
        assert result["last_tool_calls"] == []
        assert result["query_intent"] == "chitchat"

    def test_no_tool_for_factual(self, base_state, mock_llm):
        """factual 意图不调用工具"""
        base_state["messages"][1] = HumanMessage(content="什么是三集定律")
        mock_llm.invoke.return_value = AIMessage(content="三集定律是指...", tool_calls=[])
        result = reasoning_node(base_state)
        assert result["last_tool_calls"] == []
        assert result["query_intent"] == "factual"

    def test_error_flag_returns_fallback(self, base_state):
        """error_flag 时返回兜底消息"""
        base_state["error_flag"] = True
        result = reasoning_node(base_state)
        assert "抱歉" in result["messages"][0].content
        assert result["last_tool_calls"] == []

    def test_critic_feedback_injected(self, base_state, mock_llm):
        """critic_feedback 时注入提示词"""
        base_state["critic_feedback"] = "缺少评分数据 | 调用 get_subject_detail | 缺失评分"
        base_state["query_intent"] = "lookup"
        base_state["iterations"] = 1

        mock_llm.bind_tools.return_value.invoke.return_value = AIMessage(
            content="",
            tool_calls=[{"name": "get_bangumi_subject_detail", "args": {"subject_id": 123}, "id": "call_2"}]
        )
        result = reasoning_node(base_state)
        # feedback 被消费后清空
        assert result["critic_feedback"] == ""
        # 定向调用了正确的工具
        assert result["last_tool_calls"][0]["name"] == "get_bangumi_subject_detail"
```

## Layer 1: critic_node 测试

```python
class TestCriticNode:
    """critic_node — 定向反馈"""

    def test_pass_for_complete_reply(self, mock_llm):
        """完整回复 PASS"""
        mock_llm.invoke.return_value = AIMessage(content="PASS: 回复完整，包含具体评分和描述")
        state = {
            "messages": [
                SystemMessage(content="..."),
                HumanMessage(content="进击的巨人评分"),
                AIMessage(content="进击的巨人评分 8.7，排名全站 #15"),
            ],
            "iterations": 1,
        }
        result = critic_node(state)
        assert result["critic_status"] == "PASS"
        assert "PASS" in result.get("critic_feedback", "")

    def test_revise_with_specific_feedback(self, mock_llm):
        """不完整回复 REVISE + 定向反馈"""
        mock_llm.invoke.return_value = AIMessage(
            content="REVISE: 仅引用了评论但缺少评分数据 | 调用 get_bangumi_subject_detail | 缺失评分"
        )
        state = {
            "messages": [
                SystemMessage(content="..."),
                HumanMessage(content="进击的巨人评分"),
                AIMessage(content="评价还不错"),
            ],
            "iterations": 1,
        }
        result = critic_node(state)
        assert result["critic_status"] == "REVISE"
        assert "REVISE" in result.get("critic_feedback", "")
        assert "get_bangumi_subject_detail" in result.get("critic_feedback", "")

    def test_circuit_breaker_at_max_iterations(self):
        """超过最大迭代数时强制 PASS"""
        state = {
            "messages": [
                SystemMessage(content="..."),
                HumanMessage(content="测试"),
                AIMessage(content="回复"),
            ],
            "iterations": 3,
        }
        result = critic_node(state)
        assert result["critic_status"] == "PASS"
        assert result.get("error_flag") is True

    def test_rule_critic_detects_empty_reply_after_tools(self):
        """规则版: 工具返回了数据但 LLM 没回复"""
        from langchain_core.messages import ToolMessage
        state = {
            "messages": [
                HumanMessage(content="搜巨人"),
                AIMessage(content="", tool_calls=[{"name": "search", "args": {}, "id": "c1"}]),
                ToolMessage(content="找到 5 个结果", tool_call_id="c1"),
            ],
            "iterations": 1,
        }
        result = critic_node_rule(state)
        assert result["critic_status"] == "REVISE"

    def test_escape_hatch_when_data_not_found(self, mock_llm):
        """逃逸舱: API 确实无数据时禁止 REVISE，避免死循环"""
        # 助手已调用工具并如实告知没有数据
        state = {
            "messages": [
                SystemMessage(content="..."),
                HumanMessage(content="查一下这个角色的声优"),
                AIMessage(content="", tool_calls=[{"name": "get_entity_comments", "args": {}, "id": "c1"}]),
                ToolMessage(content="暂无评论数据", tool_call_id="c1"),
                AIMessage(content="抱歉，该角色暂无评论信息。Bangumi 数据库中未收录此角色的相关讨论。"),
            ],
            "iterations": 2,
        }
        mock_llm.invoke.return_value = AIMessage(content="PASS: 助手已调用工具并如实告知数据不存在")
        result = critic_node(state)
        assert result["critic_status"] == "PASS", \
            f"逃逸舱失效！数据确实不存在时不应 REVISE。got feedback: {result.get('critic_feedback')}"

    def test_escape_hatch_when_search_returns_empty(self, mock_llm):
        """逃逸舱: search 返回空结果 → PASS"""
        state = {
            "messages": [
                SystemMessage(content="..."),
                HumanMessage(content="搜一下不存在的番剧XYZ"),
                AIMessage(content="", tool_calls=[{"name": "search_bangumi_subject", "args": {"keyword": "XYZ"}, "id": "c1"}]),
                ToolMessage(content="未找到匹配的条目", tool_call_id="c1"),
                AIMessage(content="未找到与'XYZ'匹配的条目，请尝试其他关键词。"),
            ],
            "iterations": 1,
        }
        mock_llm.invoke.return_value = AIMessage(content="PASS: 工具已调用，数据确实不存在")
        result = critic_node(state)
        assert result["critic_status"] == "PASS", \
            f"逃逸舱失效！搜索结果为空时不应 REVISE。got: {result.get('critic_feedback')}"
```

## Layer 1: 记忆管理测试

```python
class TestMemory:
    """短期记忆 — 滑动窗口"""

    def test_trim_messages_preserves_system(self):
        """截断时保留 SystemMessage"""
        messages = [
            SystemMessage(content="You are Bangumi assistant."),
            HumanMessage(content="Q1"),
            AIMessage(content="A1"),
            HumanMessage(content="Q2"),
            AIMessage(content="A2"),
        ]
        trimmed = trim_messages(messages, max_tokens=20)  # 很小的预算
        # SystemMessage 始终保留
        assert any(isinstance(m, SystemMessage) for m in trimmed)
        # 至少保留了最近的消息
        assert len(trimmed) >= 1

    def test_estimate_tokens(self):
        """Token 估算大致正确"""
        msgs = [HumanMessage(content="你好世界" * 100)]  # 400 字符
        tokens = estimate_tokens(msgs)
        assert tokens == 100  # 400 // 4
```

## Layer 2: 图谱集成测试

```python
class TestGraphIntegration:
    """图谱集成 — mock LLM + 内存工具"""

    def test_chitchat_skips_tools(self):
        """'你好' 不触发任何工具"""
        from agent.graph import agent_app
        result = agent_app.invoke({
            "messages": [SystemMessage(content="..."), HumanMessage(content="你好")],
            "iterations": 0,
            "critic_status": "PENDING",
            "critic_feedback": "",
            "last_tool_calls": [],
            "query_intent": "unknown",
            "session_id": "test",
            "user_id": "test-user",
            "error_flag": False,
        })
        tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
        assert len(tool_messages) == 0

    def test_full_react_cycle_with_revise(self):
        """完整 ReAct: search → REVISE → detail → PASS"""
        # 第一轮: reasoning 返回 search tool_call
        # → tool_node 返回结果
        # → critic REVISE + feedback
        # → 第二轮: reasoning 看到 feedback，调 detail
        # → critic PASS → END
        pass  # 需要完整的 mock 编排

    def test_circuit_breaker_after_3_iterations(self):
        """3 轮后强制结束"""
        pass
```

## 不需要测的

- LangGraph 内部路由逻辑（已有 LangGraph 自己的测试覆盖）
- LangChain ToolNode 执行逻辑（同上）
- 单个工具的输入输出（Phase 2 `test_tools.py` 已覆盖）
- LLM 分类器对每个边缘 case 的准确性（LLM 行为不可确定，只测规则层 + 格式校验）

## 测试运行

```bash
# 运行所有 Agent 测试
pytest test/test_agent.py -v

# 只跑意图分类器测试（最快，无需 mock LLM）
pytest test/test_agent.py::TestIntentClassifier -v

# 只跑 critic 测试
pytest test/test_agent.py::TestCriticNode -v

# 跑全部测试
pytest test/ -v
```
