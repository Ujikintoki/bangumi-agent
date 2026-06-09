# 06 — 记忆管理：三层架构

## 问题

多轮对话 + 工具调用会产生大量消息。Token 预算有限。更重要的是，一个"帮你发现番剧"的 Agent 应该记住用户偏好——上周说喜欢《命运石之门》，这周让推荐时应该记得。

## 三层记忆架构

```
┌─────────────────────────────────────────────────────┐
│ Layer 1: 短期记忆（单次 ReAct 循环内）                 │
│ 策略：滑动窗口 + Token 预算管理                        │
│ 存储：内存（AgentState.messages）                     │
│ 生命周期：单次 graph invocation                       │
│ 状态：✅ Phase 3 实现                                 │
├─────────────────────────────────────────────────────┤
│ Layer 2: 会话记忆（跨轮次对话）                        │
│ 策略：关键信息提取 + 对话摘要                          │
│ 存储：Redis / PostgreSQL sessions 表                  │
│ 生命周期：用户会话期间（如 24h）                       │
│ 状态：📋 Phase 3 预留接口，Phase 4 实现               │
├─────────────────────────────────────────────────────┤
│ Layer 3: 用户画像（跨会话持久化）                      │
│ 策略：从对话中提取偏好，结构化存储                      │
│ 存储：PostgreSQL user_profiles / preferences 表       │
│ 生命周期：永久                                       │
│ 状态：📋 Phase 3 预留接口，Phase 4+ 实现              │
└─────────────────────────────────────────────────────┘
```

---

## Layer 1: 短期记忆（Phase 3 实现）

### Token 预算

单轮 ReAct 循环的消息量估算：

```
SystemMessage:  ~600 tokens (基础 prompt + intent 变体)
HumanMessage:    ~50 tokens (用户问题)
AIMessage:      ~100 tokens (含 tool_calls)
ToolMessage:    ~500 tokens (工具返回)
AIMessage:      ~200 tokens (LLM 最终回复)
                ----
                ~1450 tokens / 轮
```

3 轮 = ~4350 tokens。加上 critic_feedback 注入和 SystemMessage 变体，长期会话可能需要管理。

### 策略：滑动窗口

```
消息历史 (时间轴从左到右)
[SystemPrompt] [Msg1] [Msg2] ... [Msg_{N-K}] [Msg_{N-K+1} ... Msg_N]
                 ↑                           ↑
            旧消息：被截断                    最近 K 条：完整保留
```

### 两层截断策略

**第 0 层：单条消息内容截断**

在列表级截断之前，先对单条超大消息（主要是 ToolMessage 返回的海量 JSON）进行内容截断。单条上限 `_MAX_SINGLE_MESSAGE_TOKENS = 2000`，超出则用 tiktoken 精确截断后追加 `...[内容已截断]` 标记。防止一条 50KB 的搜索结果单条爆掉全部 token 预算。

```python
def _truncate_oversized_messages(messages, max_single_tokens=2000):
    """截断超过单条上限的消息内容"""
    # 遍历所有消息，对超过 max_single_tokens 的 ToolMessage 做内容截断
    # 保留 tool_call_id 和 name 元数据
```

**第 1 层：列表级滑动窗口**

当总 Token 超预算时，从头部丢弃旧消息。ToolMessage 优先截断而非丢弃。

### 触发时机：reasoning_node 开头

每轮 reasoning 开头调用 `manage_memory()`，先单条截断再检查总预算，最可靠地管理上下文窗口。

### 实现

```python
# agent/memory.py
import tiktoken
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, ToolMessage

# 全局 encoder（cl100k_base 是 GPT-4/DeepSeek/Qwen 的通用编码）
_ENCODER = tiktoken.get_encoding("cl100k_base")

def count_tokens(text: str) -> int:
    """精确 Token 计数（tiktoken cl100k_base）"""
    return len(_ENCODER.encode(text))

def estimate_tokens(messages: list) -> int:
    """精确 Token 计数。

    使用 tiktoken 而非 len(content)//4 的原因：
    - 中文单个字符通常占 1.5-2.5 tokens，//4 会低估 30-50%
    - 工具返回的 JSON 中大括号、引号、英文 key 各占 1 token，//4 严重低估
    - 生产环境中低估会导致 context_length_exceeded 错误
    """
    total = 0
    for m in messages:
        content = m.content if hasattr(m, 'content') else str(m)
        if isinstance(content, str):
            total += count_tokens(content)
        elif isinstance(content, list):  # AIMessage 的 content 可能是 list[dict]
            total += count_tokens(str(content))
    return total

def trim_messages(messages: list, max_tokens: int = 8000) -> list:
    """滑动窗口截断：保留 SystemMessage + 最近消息"""
    # 分离系统提示词（始终保留）
    system_msgs = [m for m in messages if isinstance(m, SystemMessage)]
    other_msgs = [m for m in messages if not isinstance(m, SystemMessage)]

    # 从尾部向头部保留
    kept = []
    token_count = estimate_tokens(system_msgs)

    for m in reversed(other_msgs):
        estimated = estimate_tokens([m])
        if token_count + estimated > max_tokens:
            # 超大 ToolMessage：截断内容而非整条丢弃
            if isinstance(m, ToolMessage):
                remaining = max_tokens - token_count
                if remaining > 100:
                    truncated_m = _truncate_message_content(m, remaining)
                    kept.insert(0, truncated_m)
                    token_count += estimate_tokens([truncated_m])
            break
        kept.insert(0, m)
        token_count += estimated

    return system_msgs + kept
```

### 在 Graph 中集成

方式一——添加 `memory_node`：

```python
# agent/nodes.py
def memory_node(state: AgentState) -> dict:
    """滑动窗口截断"""
    messages = state["messages"]
    if estimate_tokens(messages) > 8000:
        trimmed = trim_messages(messages, max_tokens=8000)
        return {"messages": trimmed}  # 替换整个消息列表
    return {}

# agent/graph.py — 在 tool_node 和 critic_node 之间插入
graph.add_edge("tool_node", "memory_node")
graph.add_edge("memory_node", "critic_node")
```

当前实现——集成在 `reasoning_node` 内（方式二）：

```python
def manage_memory(messages, max_tokens=8000):
    """两步策略：先截断超大单条消息，再检查总预算。"""
    # Step 0: 截断超大单条消息（>2000 tokens → 截断内容）
    messages = _truncate_oversized_messages(messages)

    # Step 1: 检查总预算，超限时滑动窗口截断
    if estimate_tokens(messages) <= max_tokens:
        return messages
    return trim_messages(messages, max_tokens)
```

### 进阶：摘要压缩（Phase 3 可选，默认不启用）

当截断的消息超过阈值时，用 LLM 生成压缩摘要：

```python
SUMMARY_PROMPT = """将以下对话历史压缩为简短摘要，保留关键信息：
- 用户的核心需求和偏好
- 已经调用过的工具和关键结果
- 尚未解决的用户需求

对话：
{conversation}

摘要："""

def summarize_old_messages(old_messages: list, llm: ChatOpenAI) -> str:
    conversation_text = "\n".join(
        f"[{type(m).__name__}]: {m.content}" for m in old_messages
    )
    summary = llm.invoke(SUMMARY_PROMPT.format(conversation=conversation_text))
    return f"[历史摘要] {summary.content}"
```

---

## Layer 2: 会话记忆（Phase 3 预留，Phase 4 实现）

### 数据模型

```python
# 未来数据库表设计
class SessionMemory(BaseModel):
    session_id: str        # UUID
    user_id: str           # 用户标识
    created_at: datetime
    last_active_at: datetime
    summary: str           # 对话摘要（由 memory_extraction_node 生成）
    key_entities: list[str] # 讨论过的关键条目，如 ["subject_10", "character_5"]
    key_topics: list[str]  # 讨论过的关键话题，如 ["2024年1月新番", "机战", "骨头社"]
    raw_message_count: int # 原始消息数量
    ttl: int = 86400       # 24小时过期
```

### 接口预留

- `AgentState` 已有 `session_id` 和 `user_id`
- `/chat` 端点已接收这两个参数
- Phase 4 只需在 endpoint 层加 `load_session()`/`save_session()`

---

## Layer 3: 用户画像（Phase 3 预留，Phase 4+ 实现）

### 提取什么

从对话中自动提取用户偏好：

| 维度 | 示例 | 来源 |
|---|---|---|
| 偏好类型 | "机战"、"恋爱"、"悬疑" | 用户搜索/询问的关键词 |
| 偏好作品 | subject_10 (命运石之门) | 用户提到或查询过的条目 |
| 评分倾向 | 偏好高分 (>8.0) | 用户查询时的过滤条件 |
| 活跃时段 | 周末晚上 | 请求时间戳聚合 |
| 对话风格 | 喜欢详细分析 vs 简短推荐 | 用户反馈和追问模式 |

### 存储格式

```python
# 未来数据库表设计
class UserProfile(BaseModel):
    user_id: str                           # 用户标识
    preferred_genres: list[str] = []       # ["机战", "悬疑", "科幻"]
    preferred_tags: list[str] = []         # ["时间旅行", "反乌托邦"]
    favorite_subjects: list[str] = []      # ["subject_10", "subject_42"]
    avg_rating_threshold: float = 7.0      # 用户偏好评分阈值
    interaction_count: int = 0             # 交互总次数
    created_at: datetime
    updated_at: datetime
```

### 记忆提取节点（Phase 4 设计）

当 critic 判定 PASS 后，在 END 之前插入 `memory_extraction_node`：

```
reasoning → tool → critic → PASS → [memory_extraction_node] → END
                                        │
                               提取偏好 → 写入 profile
```

提取逻辑：
1. 分析本轮对话中用户表达的兴趣/偏好
2. 提取关键条目 ID（用户查询/讨论过的作品）
3. 更新 Layer 2 会话摘要 + Layer 3 用户画像

### Phase 3 仅需做的事

1. `AgentState` 中加上 `user_id` 和 `session_id` ✅（已在 01_STATE.md 中定义）
2. `/chat` 端点接收这两个参数 ✅（已在 07_ENDPOINT.md 中定义）
3. 在 `initial_state` 中传递这两个字段 ✅

Phase 4 来时只需：
- 创建 `sessions` / `user_profiles` 表
- 实现 `memory_extraction_node`
- 在 graph 中注册该节点
- 在 endpoint 层加 load/save 逻辑

---

## AgentState 中的记忆相关字段

```python
class AgentState(TypedDict):
    # ... 其他字段

    # 短期记忆（Layer 1）— 直接使用 messages 字段
    # messages: Annotated[list[BaseMessage], operator.add]

    # 会话记忆（Layer 2 预留）
    session_id: str     # ← /chat 端点传入

    # 用户画像（Layer 3 预留）
    user_id: str        # ← /chat 端点传入
```

> Phase 3 只需要 `session_id` 和 `user_id` 存在于 State 中，不需要任何持久化逻辑。它们作为预留接口保证未来扩展时不改 State 结构。
