# 04 — critic_node: 定向反馈质量评估

## 职责

评估当前对话是否充分回答了用户问题。输出不是简单的 PASS/REVISE，而是**定向反馈**——REVISE 时附带具体改进建议，指导下一轮 reasoning 的修正方向。

## 当前占位

```python
def critic_node(state: AgentState) -> dict:
    if iterations >= 3: return {"critic_status": "PASS", "error_flag": True}
    return {"critic_status": "REVISE" if iterations < 2 else "PASS"}
```

仅凭迭代次数判断，LLM 回答质量完全不参与决策。

## 目标设计：三元维度 + 定向反馈

### 评审维度

| 维度 | 检查内容 | 示例缺陷 |
|---|---|---|
| **完整性** | 是否回答了用户的所有子问题？是否有遗漏？ | 用户问了评分和声优，回复只有评分 |
| **具体性** | 是否包含具体数据（名称、数字、评分）而非笼统描述？ | "评价不错" vs "评分 8.5，热门评论正面居多" |
| **工具利用** | 是否充分利用了可用工具的结果？是否有更合适的工具未调用？ | 用户问评分但没调 `get_subject_detail`，只给了文字描述 |

### 输出格式

```
PASS: <一句话确认回复质量>

REVISE: <具体缺陷描述> | <建议操作> | <缺失信息类型>
```

示例：

```
PASS: 回复包含具体作品名称、评分和推荐理由，完整回答了用户的发现需求。

REVISE: 回复只引用了评论内容但没有给出条目的评分数据 | 调用 get_bangumi_subject_detail 获取 rating 字段 | 缺失评分
REVISE: 用户询问了声优信息但回复未涉及 | 调用 get_subject_characters 获取角色和声优列表 | 缺失角色/声优
REVISE: 回复过于笼统，建议列出具体作品名而非泛泛的类别推荐 | 调用 search_local_bangumi 获取语义匹配的具体条目 | 缺失具体条目
```

## Critic 节点实现

### critic_node 核心逻辑

```python
from langchain_core.messages import AIMessage, SystemMessage, HumanMessage

CRITIC_SYSTEM_PROMPT = """你是 Bangumi 助手的输出质量控制专家。按以下三个维度评估助手的最后一条回复：

1. **完整性**：是否回答了用户的所有子问题？
2. **具体性**：是否包含具体数据（名称、评分、数字），而非模糊描述？
3. **工具利用**：是否有合适的工具未被调用，导致信息不完整？

输出格式：
- 如果全部通过：PASS: <一句话确认>
- 如果需要改进：REVISE: <缺陷> | <建议操作> | <缺失类型>

注意：
- 对于寒暄和常识性问题（如"你好"、"什么是三集定律"），只要回复自然合理即可 PASS
- 不要因为"可以补充更多信息"而 REVISE——只修复真正的缺陷
- 当用户查询属于 discovery 类型时，必须包含具体作品名称和评分才算具体性通过

## ⚠️ 信息缺失免责条款（Escape Hatch）——最高优先级

**如果助手已经调用了合适的工具，并在回复中明确表示"数据中不包含该信息"（或其等价表述），则必须判定为 PASS，绝对禁止 REVISE。**

适用场景：
- API 返回空结果：助手调用 search 后回复"未找到匹配的条目"                     → 必须 PASS
- 数据确实不存在：助手调用 get_detail 后回复"该条目暂无评分数据"                  → 必须 PASS
- 角色信息缺失：助手调用 get_characters 后回复"此条目暂无角色信息"               → 必须 PASS
- 评论为空：助手调用 get_comments 后回复"该集暂无用户评论"                       → 必须 PASS

判断逻辑：助手已尽职调用工具 → 工具返回确实无数据 → 助手如实告知 → 必须 PASS。
**不要在信息客观上不存在时因为"不够具体"而打回——这会导致无意义的死循环。**"""

def critic_node(state: AgentState) -> dict:
    # 1. 熔断防御
    if state.get("iterations", 0) >= 3:
        return {
            "critic_status": "PASS",
            "critic_feedback": "",
            "error_flag": True,
        }

    # 2. 提取用户原始问题（第二条消息，跳过 SystemMessage）
    messages = state["messages"]
    user_query = ""
    for m in messages:
        if isinstance(m, HumanMessage):
            user_query = m.content
            break

    # 3. 提取最后一条 AI 回复（不含 tool_calls 的 AIMessage）
    last_ai = None
    for m in reversed(messages):
        if isinstance(m, AIMessage) and m.content and not (hasattr(m, 'tool_calls') and m.tool_calls):
            last_ai = m
            break

    if last_ai is None:
        return {"critic_status": "REVISE", "critic_feedback": "未找到有效的 AI 回复 | 重新调用 LLM 生成回复 | 系统错误"}

    # 4. LLM 评估
    settings = get_settings()
    critic_model = settings.LLM_CRITIC_MODEL or settings.LLM_MODEL  # Critic 可用小模型
    llm = ChatOpenAI(model=critic_model, temperature=0)

    eval_messages = [
        SystemMessage(content=CRITIC_SYSTEM_PROMPT),
        HumanMessage(content=f"""用户问题: {user_query}

助手回复: {last_ai.content}

请按三维度评估并给出结论："""),
    ]
    verdict = llm.invoke(eval_messages).content.strip()

    # 5. 解析 verdict
    if verdict.upper().startswith("PASS"):
        return {"critic_status": "PASS", "critic_feedback": verdict}
    else:
        return {"critic_status": "REVISE", "critic_feedback": verdict}
```

### critic_feedback 如何在 reasoning 中使用

```
critic_node → REVISE, feedback="缺少评分数据 | 调用 get_subject_detail | 缺失评分"
    │
    ▼
reasoning_node (下一轮)
    │ build_messages_for_llm() 中检测到 critic_feedback 非空
    │ → SystemMessage 追加:
    │   "## ⚠️ 上一轮回复需要改进"
    │   "缺少评分数据 | 调用 get_subject_detail | 缺失评分"
    │   "请针对以上问题修正你的回复。"
    │
    ▼
LLM 看到具体指引 → 定向调用 get_subject_detail → 获取评分 → 修正回复
```

这就是从"盲猜重试"到"定向修正"的升级。

## Critic 版本策略：规则版 → LLM 版

两版接口相同，通过配置切换，先跑通流程再升级。

### 规则版 Critic（零 Token，先实现）

```python
def critic_node_rule(state: AgentState) -> dict:
    """规则版：快速检查，零 token 消耗"""
    iterations = state.get("iterations", 0)
    if iterations >= 3:
        return {"critic_status": "PASS", "critic_feedback": "", "error_flag": True}

    messages = state["messages"]

    # 检查 1: 是否有工具返回但 LLM 回复无实质内容
    has_tool_msgs = any(isinstance(m, ToolMessage) for m in messages)
    last_ai = None
    for m in reversed(messages):
        if isinstance(m, AIMessage) and m.content and not (hasattr(m, 'tool_calls') and m.tool_calls):
            last_ai = m
            break

    if has_tool_msgs and last_ai is None:
        return {
            "critic_status": "REVISE",
            "critic_feedback": "工具已返回数据但未生成有效回复 | 基于工具返回内容重新组织回答 | 回复缺失",
        }

    # 检查 2: 回复过短（少于 20 字且有工具调用）可能未充分利用工具结果
    if has_tool_msgs and last_ai and len(last_ai.content) < 20:
        return {
            "critic_status": "REVISE",
            "critic_feedback": "回复过短，可能未充分利用工具返回的数据 | 基于工具结果展开详细回答 | 不够具体",
        }

    return {"critic_status": "PASS", "critic_feedback": ""}
```

### 切换方式

```python
# core/config.py
CRITIC_MODE: str = "rule"  # "rule" | "llm"

# agent/nodes.py
def critic_node(state: AgentState) -> dict:
    settings = get_settings()
    if settings.CRITIC_MODE == "rule":
        return critic_node_rule(state)
    else:
        return critic_node_llm(state)
```

## 成本分析

### LLM 版

| 组件 | Token 消耗 |
|---|---|
| 系统提示词 | ~150 tokens |
| 评估消息 | ~300 tokens |
| 输出 | ~20-40 tokens（含具体反馈） |
| **每轮评估** | **~500 tokens** |

> 3 轮迭代的 critic 总消耗约 1500 tokens，约 $0.004 (GPT-4o)。定向反馈比二元判断多消耗 ~100 tokens/轮，但减少了无效重试的浪费，总体可能更省。

### 规则版

| 组件 | Token 消耗 |
|---|---|
| 全部 | **0 tokens** |

> 推荐先用规则版，确认 ReAct 循环和 feedback 注入机制正确后，切换到 LLM 版获得更精准的评估。

## 路由逻辑

```python
def route_after_critic(state: AgentState) -> Literal["reasoning_node", END]:
    if state.get("critic_status") == "REVISE" and state.get("iterations", 0) < 3:
        return "reasoning_node"
    return END
```

## 完整数据流

```
用户: "巨人最终季口碑如何"
    │
    ▼
reasoning (intent=lookup)
    │ search_bangumi_subject("进击的巨人 最终季") → subject_id=123
    ▼
tool_node → ToolMessage("找到条目: 进击的巨人 The Final Season")
    │
    ▼
reasoning (第二轮)
    │ get_episode_comments(episode_id=...) → 评论数据
    ▼
tool_node → ToolMessage("500条评论...")
    │
    ▼
reasoning (第三轮)
    │ LLM: "最终季口碑良好，观众认为制作精良、剧情紧凑"
    ▼
critic
    │ 评估: 引用了评论但没有给出评分，用户可能期望看到量化的口碑数据
    │ → REVISE: "仅引用了评论文字，缺少量化评分 | 调用 get_bangumi_subject_detail 获取 rating | 缺失评分"
    ▼
reasoning (第四轮，收到 feedback)
    │ get_bangumi_subject_detail(subject_id=123) → rating: 8.7
    ▼
tool_node → ToolMessage("评分 8.7, 排名 #15, ...")
    │
    ▼
reasoning (第五轮)
    │ LLM: "最终季评分 8.7，排名全站 #15，评论普遍认为..." ← 现在有具体数据了
    ▼
critic → PASS: "回复完整，包含具体评分、排名和评论摘要"
    │
    ▼
END
```

> 注意：此示例中 critic 触发了 2 轮额外推理。`_MAX_ITERATIONS` 需要考虑这个情况——如果迭代上限设为 3，这个场景会被熔断。建议评估实际使用后，考虑将上限调至 5 或保持 3 但在 critic 中动态判断（当工具调用有实质进展时允许更多轮次）。
