# 08 — 测试策略

## 测试层级

```
Layer 1: 节点单元测试 (mock LLM)
   ├─ reasoning_node: mock LLM 返回 → 验证 needs_tool 路由
   ├─ tool_node: mock 工具 → 验证 ToolMessage 格式
   └─ critic_node: mock LLM → 验证 PASS/REVISE 决策

Layer 2: 图谱集成测试 (mock LLM + 内存工具)
   ├─ "你好" → 不触发工具 → PASS → END
   ├─ "搜进击的巨人" → 触发 search → critic REVISE → 再推理 → PASS
   └─ 错误路径: 3 轮 REVISE → 熔断

Layer 3: 端到端测试 (真实 LLM + 真实 API, 可选)
   └─ 完整对话: "海贼王最新一集评价" → 调 API → 返回分析
```

## Layer 1 示例: mock reasoning_node

```python
from unittest.mock import MagicMock, patch
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

@pytest.fixture
def mock_llm():
    with patch("agent.nodes.ChatOpenAI") as mock:
        instance = MagicMock()
        mock.return_value = instance
        yield instance

def test_reasoning_detects_tool_need(mock_llm):
    # LLM 返回含 tool_calls 的 AIMessage
    from langchain_core.messages import ToolCall
    mock_llm.bind_tools.return_value.invoke.return_value = AIMessage(
        content="",
        tool_calls=[{"name": "search_bangumi_subject", "args": {"keyword": "巨人"}, "id": "call_1"}]
    )

    state = {
        "messages": [SystemMessage(content="..."), HumanMessage(content="搜进击的巨人")],
        "iterations": 0,
        "last_tool_calls": [],
        "critic_status": "PENDING",
        "error_flag": False,
    }
    result = reasoning_node(state)
    assert result["iterations"] == 1
    assert len(result["last_tool_calls"]) == 1

def test_reasoning_no_tool_for_trivia(mock_llm):
    # LLM 直接回答
    mock_llm.bind_tools.return_value.invoke.return_value = AIMessage(
        content="三集定律是指...",
        tool_calls=[]
    )
    state = { ... }  # "什么是三集定律？"
    result = reasoning_node(state)
    assert result["last_tool_calls"] == []
```

## Layer 2 示例: 图谱集成测试

```python
def test_graph_hello_skips_tools():
    """'你好'不触发任何工具。"""
    from agent.graph import agent_app
    result = agent_app.invoke({
        "messages": [SystemMessage(content="..."), HumanMessage(content="你好")],
        ...
    })
    tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 0  # 没有工具调用
```

## 不需要测的

- LangGraph 内部路由逻辑（已有 LangGraph 自己的测试覆盖）
- LangChain ToolNode 执行逻辑（同上）
- 单个工具的输入输出（Phase 2 `test_tools.py` 已覆盖）
