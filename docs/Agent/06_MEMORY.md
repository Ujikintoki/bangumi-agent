# 06 — 短期记忆管理

## 问题

多轮对话 + 工具调用会产生大量消息。Token 预算有限（如 GPT-4o = 128K，但实际要保留给工具返回结果）。

示例：1 轮 ReAct 循环的消息量

```
SystemMessage:  ~400 tokens (系统提示词)
HumanMessage:    ~50 tokens (用户问题)
AIMessage:       ~100 tokens (含 tool_calls)
ToolMessage:     ~500 tokens (工具返回的 JSON/文本)
AIMessage:       ~200 tokens (LLM 最终回复)
                 ----
                  ~1250 tokens / 轮
```

3 轮 = ~3750 tokens，还远在预算内。但长期会话 + 频繁工具调用需要管理。

## 策略：滑动窗口 + 摘要压缩

```
消息历史 (时间轴从左到右)
[SystemPrompt] [Msg1] [Msg2] ... [Msg_{N-K}] [Msg_{N-K+1} ... Msg_N]
                 ↑                           ↑
            旧消息：摘要压缩为一条             最近 K 条：完整保留
```

### 实现

```python
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, trim_messages

def manage_context(messages: list, max_tokens: int = 8000) -> list:
    """滑动窗口截断，保留系统提示词 + 最近消息。"""

    # 分离系统提示词
    system_msgs = [m for m in messages if isinstance(m, SystemMessage)]
    other_msgs = [m for m in messages if not isinstance(m, SystemMessage)]

    # 从尾部保留最近消息，直到接近 max_tokens
    kept = []
    token_count = sum(len(m.content) // 4 for m in system_msgs)  # 粗略估算

    for m in reversed(other_msgs):
        estimated = len(str(m.content)) // 4
        if token_count + estimated > max_tokens:
            break
        kept.insert(0, m)
        token_count += estimated

    return system_msgs + kept
```

### 进阶：摘要压缩

当截断的消息超过阈值时，用 LLM 生成压缩摘要：

```python
SUMMARY_PROMPT = """将以下对话历史压缩为简短摘要，保留关键信息：
- 用户的核心需求和偏好
- 已经调用过的工具和关键结果

对话：
{conversation}

摘要："""

def summarize_old_messages(old_messages: list) -> str:
    llm = ChatOpenAI(model=settings.LLM_MODEL, temperature=0)
    conversation_text = "\n".join(str(m.content) for m in old_messages)
    summary = llm.invoke(SUMMARY_PROMPT.format(conversation=conversation_text))
    return summary.content
```

### 何时触发压缩

```python
def estimate_tokens(messages: list) -> int:
    return sum(len(str(m.content)) // 4 for m in messages)

# 在 reasoning_node 开头检查
def reasoning_node(state: AgentState) -> dict:
    if estimate_tokens(state["messages"]) > 8000:
        old = state["messages"][:-10]  # 保留最近 10 条
        summary = summarize_old_messages(old)
        state["messages"] = [SystemMessage(content=SYSTEM_PROMPT)] \
                          + [HumanMessage(content=f"[历史摘要] {summary}")] \
                          + state["messages"][-10:]
    # ... 继续推理
```

> **Phase 3 初期建议**：暂不实现压缩，先用滑动窗口。大多数单次查询不会超过 8000 tokens。
