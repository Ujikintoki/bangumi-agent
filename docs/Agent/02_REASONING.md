# 02 — reasoning_node: 意图分类 + LLM Function-Calling

## 职责

两阶段处理：
1. **意图分类**：判断用户查询类型，决定工具调用策略
2. **LLM 推理**：调用 LLM（绑定工具），决定直接回复 OR 调用哪些工具

## 整体流程

```
reasoning_node(state)
    │
    ├─ Step 0: 兜底检查（error_flag → 直接返回错误回复）
    │
    ├─ Step 1: 意图分类
    │   ├─ 规则匹配（关键词 + 正则）→ 覆盖 80% 常见查询
    │   └─ LLM fallback（规则无法匹配时）
    │   输出: query_intent ∈ {chitchat, factual, lookup, discovery, realtime, unknown}
    │
    ├─ Step 2: 选择系统提示词变体
    │   根据 query_intent 选择对应的 intent_prompt
    │
    ├─ Step 3: 注入 critic_feedback（如果非空）
    │   REVISE 时附带上一轮 Critic 的具体改进建议
    │
    ├─ Step 4: 构建消息列表 → 调用 LLM（bind_tools）
    │
    └─ Step 5: 返回结果
        {messages: [AIMessage], last_tool_calls: [...], query_intent, iterations+1}
```

## Step 1: 意图分类器

### 1a. 规则层（零延迟，覆盖 80% 查询）

```python
# ⚠️ 关键：使用优先级列表（Priority List），而非无序字典遍历
# 复合意图（discovery, realtime）必须先于简单意图（lookup, factual）求值
# 否则 "找类似XX的番" 会被 lookup 的关键词"找"先拦截
INTENT_RULES: list[tuple[str, dict]] = [
    # 优先级 1: 复合意图 — 包含多个语义维度
    ("discovery", {
        "keywords": ["类似", "推荐", "差不多", "像.*一样", "还有什么",
                      "冷门", "小众", "神作", "评分最高", "最好看"],
        "patterns": [r"(类似|推荐|像.*一样|还有什么|找.*番|求.*番|跟.*差不多|和.*类似)"],
    }),
    ("realtime", {
        "keywords": ["今天", "本周", "这周", "放送", "播出", "排期", "日历",
                      "最近什么火", "最近流行", "热门", "趋势", "新番"],
        "patterns": [r"(今天|本周|这周|最近).*(放|播|火|流行|热门|排)"],
    }),

    # 优先级 2: 简单意图 — 单一查询维度
    ("lookup", {
        "keywords": ["搜索", "找", "查", "声优", "角色", "详情",
                      "评价", "评论", "吐槽", "几集", "多少集"],
        "patterns": [r"^(搜|找|查|帮我).*(评分|声优|角色|详情|评论|评价|多少|几集)"],
    }),
    ("factual", {
        "keywords": ["什么是", "什么叫", "定义", "解释", "三集定律", "作画崩坏",
                      "是谁", "哪一年", "什么时候出的"],
        "patterns": [r"^(什么是|什么叫|谁是的|解释一下)"],
    }),

    # 优先级 3: 兜底
    ("chitchat", {
        "keywords": ["你好", "谢谢", "再见", "嗨", "hello", "hi", "晚安", "早安"],
        "patterns": [r"^(你好|谢谢|再见|嗨|hello|hi|晚安|早安)$"],
    }),
]

def classify_intent_rule(user_message: str) -> str | None:
    """规则分类（优先级队列），返回 intent 或 None（需要 LLM fallback）

    关键设计：
    1. 使用有序列表（list[tuple]）而非字典，保证匹配顺序 == 优先级顺序
    2. 复合意图（discovery, realtime）排在前面，防止被简单意图的关键词"劫持"
    3. chitchat 排在最后作为兜底——只有更具体的意图都不匹配时才命中
    """
    msg = user_message.strip().lower()

    for intent, config in INTENT_RULES:
        # 关键词匹配
        for kw in config["keywords"]:
            if kw in msg:
                return intent
        # 正则匹配
        for pattern in config["patterns"]:
            if re.search(pattern, msg):
                return intent

    # 短消息（< 5 字）且无明确工具意图 → chitchat
    if len(msg) < 5:
        return "chitchat"

    return None  # 需要 LLM fallback
```

### 1b. LLM fallback（规则无法匹配时）

```python
INTENT_CLASSIFIER_PROMPT = """将用户消息分类为以下类别之一，只回复类别名称：

- chitchat: 寒暄、问候、闲聊
- factual: 领域常识问题，不需要查询实时数据
- lookup: 精确查找特定条目、评分、声优、评论
- discovery: 模糊推荐、探索发现、"类似XX的番"
- realtime: 询问当前热门、放送排期、最新动态
- unknown: 无法明确分类

用户消息: {user_message}

类别:"""

def classify_intent_llm(user_message: str, llm: ChatOpenAI) -> str:
    """LLM fallback 分类"""
    response = llm.invoke(
        INTENT_CLASSIFIER_PROMPT.format(user_message=user_message)
    )
    intent = response.content.strip().lower()
    valid_intents = {"chitchat", "factual", "lookup", "discovery", "realtime", "unknown"}
    return intent if intent in valid_intents else "unknown"
```

## Step 2: 意图特定的 Prompt 变体

```python
INTENT_PROMPTS = {
    "chitchat": """
你正在和用户进行轻松对话。保持友好、简洁。
**禁止调用任何工具**——直接回复即可。
""",

    "factual": """
用户询问领域常识。基于你的训练知识回答。
**禁止调用任何工具**——除非用户明确要求查询最新数据。
""",

    "lookup": """
用户需要精确查找信息。
策略：先调用 search_bangumi_subject 定位条目 ID，再根据需要调用详情/角色/评论工具。
当需要条目的评分、详情、角色信息时，必须先拿到 subject_id。
""",

    "discovery": """
用户想发现新内容（推荐、类似作品、探索）。
策略：优先使用 search_local_bangumi（RAG 语义搜索），结果不足时再用 search_bangumi_subject 补充。
最终回复应包含：作品名称、评分、简短推荐理由。
""",

    "realtime": """
用户询问时效性数据。
策略：优先使用 get_calendar（放送排期）、get_trending_topics（热门趋势）、get_episode_comments（最新评论）。
不需要先搜索条目 ID——时效类工具直接可用。
""",

    "unknown": """
标准策略：根据用户需求自行判断是否需要工具。
常识问题直接回答，需要数据时选择合适的工具。
""",
}
```

最终 prompt 拼接方式：

```python
def build_messages_for_llm(state: AgentState) -> list:
    """构建发送给 LLM 的消息列表"""
    intent = state.get("query_intent", "unknown")
    intent_prompt = INTENT_PROMPTS.get(intent, INTENT_PROMPTS["unknown"])

    # 1. 基础系统提示词
    system_content = SYSTEM_PROMPT + "\n\n## 当前查询类型: " + intent + "\n" + intent_prompt

    # 2. 注入 critic_feedback（如果有）
    feedback = state.get("critic_feedback", "")
    if feedback:
        system_content += f"\n\n## ⚠️ 上一轮回复需要改进\n{feedback}\n请针对以上问题修正你的回复。"

    # 3. 构建消息列表
    messages = [SystemMessage(content=system_content)]

    # 4. 添加历史消息（不含之前的 SystemMessage，避免 prompt 叠加）
    for m in state["messages"]:
        if not isinstance(m, SystemMessage):
            messages.append(m)

    return messages
```

## Step 3-5: 核心推理逻辑

```python
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI
from core.config import get_settings

def reasoning_node(state: AgentState) -> dict:
    # Step 0: 兜底模式
    if state.get("error_flag"):
        return {
            "messages": [AIMessage(content="抱歉，系统当前繁忙，请稍后再试。")],
            "last_tool_calls": [],
        }

    settings = get_settings()

    # Step 1: 意图分类（仅第一轮执行）
    query_intent = state.get("query_intent", "unknown")
    if query_intent == "unknown" or state.get("iterations", 0) == 0:
        # 先从历史中提取用户原始输入
        user_msgs = [m for m in state["messages"] if isinstance(m, HumanMessage)]
        user_input = user_msgs[-1].content if user_msgs else ""

        query_intent = classify_intent_rule(user_input)
        if query_intent is None:
            # LLM fallback（用小参数、低延迟调用）
            llm = ChatOpenAI(
                model=settings.LLM_MODEL,
                api_key=settings.LLM_API_KEY,
                base_url=settings.LLM_BASE_URL,
                temperature=0,
                max_tokens=10,
            )
            query_intent = classify_intent_llm(user_input, llm)

    # Step 2 & 3: 构建消息（含 intent prompt + critic_feedback）
    messages = build_messages_for_llm(state)

    # Step 4: 调用 LLM（绑定工具）
    llm = ChatOpenAI(
        model=settings.LLM_MODEL,
        api_key=settings.LLM_API_KEY,
        base_url=settings.LLM_BASE_URL,
        temperature=0.3,
    )

    # chitchat 和 factual 不绑定工具
    if query_intent in ("chitchat", "factual"):
        llm_with_tools = llm  # 不 bind_tools
    else:
        tools = get_agent_tools()
        llm_with_tools = llm.bind_tools(tools)

    response: AIMessage = llm_with_tools.invoke(messages)

    return {
        "messages": [response],
        "iterations": state.get("iterations", 0) + 1,
        "last_tool_calls": response.tool_calls if hasattr(response, 'tool_calls') else [],
        "query_intent": query_intent,
        # critic_feedback 在本轮已被消费，清空
        "critic_feedback": "",
    }
```

## LLM 返回示例

**无工具调用（LLM 直接回答）：**
```python
AIMessage(
    content="顶上战争是白胡子海贼团与海军本部之间的大战...",
    tool_calls=[]
)
→ route_after_reasoning → "critic_node"（跳过工具）
```

**有工具调用：**
```python
AIMessage(
    content="",
    tool_calls=[
        {"name": "get_episode_comments", "args": {"episode_id": 1088, "comments_limit": 10}, "id": "call_1"}
    ]
)
→ route_after_reasoning → "tool_node"
```

## 路由逻辑

```python
def route_after_reasoning(state: AgentState) -> Literal["tool_node", "critic_node"]:
    if state.get("last_tool_calls"):
        return "tool_node"
    return "critic_node"
```

## LLM 配置

需要在 `core/config.py` 新增：

```python
LLM_API_KEY: str = ""              # OpenAI / DeepSeek / Qwen API Key
LLM_MODEL: str = "gpt-4o"         # 或 "deepseek-chat" / "qwen-plus"
LLM_BASE_URL: str = "https://api.openai.com/v1"
LLM_TEMPERATURE: float = 0.3
LLM_CRITIC_MODEL: str = ""        # Critic 专用模型（可选，默认同 LLM_MODEL）
```

## 调试日志

```python
logger.info(f"[Intent] query='{user_input[:50]}' → intent={query_intent} (method={'rule' if rule_match else 'llm'})")
logger.info(f"[Reasoning] intent={query_intent} tool_calls={[tc['name'] for tc in response.tool_calls]}")
# → "[Intent] query='类似命运石之门的烧脑番' → intent=discovery (method=rule)"
# → "[Reasoning] intent=discovery tool_calls=['search_local_bangumi']"
# → "[Intent] query='你好' → intent=chitchat (method=rule)"
# → "[Reasoning] intent=chitchat tool_calls=[]"
```

## 关键注意事项

1. **意图分类仅第一轮执行**：后续 REVISE 重试时复用第一轮的 `query_intent`
2. **chitchat/factual 不绑定工具**：节省 token，防止 LLM 对"你好"也去调搜索
3. **critic_feedback 消费后清空**：避免下一轮重复注入
4. **intent prompt 是附加的**：拼在 SYSTEM_PROMPT 之后，不是替换
5. **temperature = 0.3**：工具调用场景需要低温度，减少幻觉和错误参数
6. **重入安全**：reasoning_node 可能被多次调用（critic REVISE 后）。LLM 看到完整消息历史来理解上下文
