# 04 — critic_node: LLM 输出质量评估

## 职责

评估当前对话是否充分回答了用户问题。决定 PASS（结束）或 REVISE（重试）。

## 当前占位

```python
def critic_node(state: AgentState) -> dict:
    if iterations >= 3: return {"critic_status": "PASS", "error_flag": True}
    return {"critic_status": "REVISE" if iterations < 2 else "PASS"}
```

## 核心逻辑

```python
from langchain_core.messages import AIMessage, SystemMessage

CRITIC_SYSTEM_PROMPT = """你是 Bangumi 助手的输出质量控制专家。评估助手的最后一条回复。

标准：
- PASS: 回复完整回答了用户的问题。包含具体信息（名称、评分、数字等），而非模糊的通用建议。
- REVISE: 回复缺少关键信息、过于笼统、或没有充分利用可用工具的结果。

只回复 PASS 或 REVISE。"""

def critic_node(state: AgentState) -> dict:
    # 熔断防御
    if state.get("iterations", 0) >= 3:
        return {"critic_status": "PASS", "error_flag": True}

    # 提取最后一条 AI 回复
    messages = state["messages"]
    last_ai = None
    for m in reversed(messages):
        if isinstance(m, AIMessage) and not m.tool_calls:
            last_ai = m
            break

    if last_ai is None:
        return {"critic_status": "REVISE"}

    # LLM 评估
    llm = ChatOpenAI(model=settings.LLM_MODEL, temperature=0)
    eval_messages = [
        SystemMessage(content=CRITIC_SYSTEM_PROMPT),
        HumanMessage(content=f"用户问题: {messages[1].content}\n\n助手回复: {last_ai.content}"),
    ]
    verdict = llm.invoke(eval_messages).content.strip().upper()

    return {"critic_status": "PASS" if "PASS" in verdict else "REVISE"}
```

## 成本分析

| 组件 | Token 消耗 |
|---|---|
| 系统提示词 | ~100 tokens |
| 评估消息 | ~300 tokens |
| 输出 | 1 token (PASS/REVISE) |
| **每轮评估** | **~400 tokens** |

> 3 轮迭代的 critic 总消耗约 1200 tokens，约 $0.003 (GPT-4o)。远低于一轮完整的 API 工具调用，成本可接受。

## 另一种方案：规则 Critic（零 Token）

如果不想消耗 LLM Token，可以保留规则自省作为默认方案：

```python
def critic_node(state: AgentState) -> dict:
    iterations = state.get("iterations", 0)
    if iterations >= 3:
        return {"critic_status": "PASS", "error_flag": True}

    # 检查是否有工具返回但 LLM 没引用
    has_tool_msgs = any(isinstance(m, ToolMessage) for m in state["messages"])
    last_ai_has_content = any(
        isinstance(m, AIMessage) and m.content and not m.tool_calls
        for m in reversed(state["messages"])
    )

    if has_tool_msgs and not last_ai_has_content:
        return {"critic_status": "REVISE"}
    return {"critic_status": "PASS"}
```

> 推荐先用规则版本，确认流程跑通后切换到 LLM 版本。两版接口相同，互换零成本。
