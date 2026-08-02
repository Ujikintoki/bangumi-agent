# 分离合成架构设计 v2（Decoupled Synthesis）

> 2026-08-02，与 Claude Code 的架构讨论。从 v1 的"prompt 搬运"重构为"控制流 + 系统边界"驱动。
> v1 被否决的原因：把架构问题当成了 prompt 拼接问题，忽略了控制流确定性和系统边界。
> 状态：**设计阶段，尚未实施。**

---

## 0. v1 为什么被否决（三个致命缺陷）

v1 方案的核心思路是"把 Character Card 从 reasoning System Prompt 移到 render System Prompt，让 reasoning 输出结构化 Markdown"。这犯了三个架构错误：

| 缺陷 | v1 的做法 | 为什么错 |
|------|----------|---------|
| **被动退出** | Aggregator "决定不调 tool 了，开始写 Markdown" | 退出依赖文本生成而非 function calling，不确定性高；LLM 不写 Markdown 就卡死循环 |
| **混淆边界** | Aggregator 直接输出 Markdown 给 Render | Aggregator 的输出对象是代码（编排层），不是 Render LLM；JSON 走 tool call 管道 >99% 可靠，自由文本不可控 |
| **LLM 自述状态** | Aggregator 总结 `## 检索状态` | LLM 会编造——迭代次数、工具调用列表、空结果计数在 AgentState 里是确定的，应由代码提取 |

**v2 的核心原则**：LLM 做判断，代码做执行。判断的边界用 function calling 标定，不用文本生成。

---

## 1. 要解决的核心矛盾

当前两层人格管线存在严重的信息不对称：

| 层 | 人格内容量 | 实际效果 |
|---|----------|---------|
| **System Prompt** (Reasoning) | Character Card ~530 字 | 风格内容和思考内容混在一起，LLM 调工具/消化数据时被"熨平" |
| **Render** (后处理) | Voice Hint ~55 字 | 55 字要承载全部人格表达差异，远不够 |

**结果**：`bangumi_cold` 和 `bangumi_cute` 的实际输出差异，主要靠这 55 个字。

## 2. 解决方案：分离合成

```
新架构:
  Aggregator LLM ← 纯工具指引 + depth_taste → submit_facts_to_render (function calling 退出)
  编排代码        ← 解析 JSON + 提取 AgentState → 确定性拼接 Markdown
  Render LLM     ← 完整人格 + 系统状态 + 数据 → 自然语言回复
```

### 2.1 Aggregator 是什么

**数据聚合引擎。** System Prompt 写："你是数据聚合引擎。调用工具获取数据。收集完毕后，调用 `submit_facts_to_render` 提交事实清单。不要尝试直接回答用户。"

### 2.2 编排代码做什么（新增，v1 缺失的部分）

**确定性数据中转。** 这是 v2 的关键——代码层负责：

1. 从 `submit_facts_to_render` 的 ToolMessage 中取出 JSON
2. 从 `AgentState` 中提取系统状态（iterations、tools_used、empty searches）
3. 用 Python 拼接 Markdown prompt
4. 将其喂给 Render LLM

### 2.3 Render 是什么

**真正的角色。** System Prompt 写完整 Character Card（530 字）+ snark/initiative 参数。消费的是代码拼好的 Markdown（确定性格式），输出的是自然语言。

## 3. 参数分配

```
depth_taste → Aggregator:  查什么数据、查多深（_DEPTH_LEVELS，文本从"人格侧写"转为"行为指令"）
snark       → Render:      用什么态度说（_SNARK_LEVELS）
initiative  → Render:      说多少、怎么收尾（_INITIATIVE_LEVELS）
depth 模式  → 两层共享:     机械约束（迭代上限、token 预算、字数上限）
```

### 为什么 snark 不控制数据偏向

毒舌是解读方式，不是筛选方式。同一个 "8.8 分"：cold 说"也就那样"，cute 说"超高的"。数据完整，差异在表达。

### 为什么 initiative 不控制搜索范围

搜索广度由 depth_taste + depth 模式决定。initiative 是纯风格——"说完就停 vs 留话头"。

### depth 的两层翻译

| depth | Aggregator | Render |
|-------|-----------|--------|
| quick | 3轮上限, 6000 tok, dt=0.35 | 120 字 |
| auto | 5轮上限, 10000 tok, 角色默认 dt | 200 字 |
| deep | 12轮上限, 16000 tok, dt=0.90 | 350 字 |

## 4. 控制流设计（v2 的核心）

### 4.1 专用终止工具

```python
@tool
async def submit_facts_to_render(
    facts: list[dict],
    intent: str,
    missing: str = "",
) -> dict:
    """数据收集完成。提交事实清单给下游系统。
    
    必须调用此工具来结束数据收集。调用后你的工作完成。
    
    Args:
        facts: 事实列表，每项 {"name": str, "score": float|null, "rank": int|null,
               "summary": str, "tags": [str], "source": "search"|"detail"|"opinions"}
        intent: 查询意图（3-10字）
        missing: 用户想要但你未找到的数据（空字符串=无缺失）
    """
    return {"facts": facts, "intent": intent, "missing": missing}
```

### 4.2 强制退出路由

```
START → reasoning_node ⇄ tool_node → [detect submit_facts] → END
```

```python
# graph.py
def route_after_tool(state: AgentState) -> Literal["reasoning_node", "__end__"]:
    """tool_node 后: submit_facts_to_render → 强制退出，其他 → 继续 ReAct"""
    messages = state.get("messages", [])
    if messages:
        last = messages[-1]
        if isinstance(last, ToolMessage) and getattr(last, "name", "") == "submit_facts_to_render":
            return END
    return "reasoning_node"

# 边: tool_node 根据路由决定去 reasoning 还是结束
graph.add_conditional_edges("tool_node", route_after_tool, {
    "reasoning_node": "reasoning_node",
    END: END,
})
```

退出不再是"LLM 决定不调 tool 了"，而是"LLM 调了一个特殊 tool → 代码识别 → 强制切断循环"。**控制流确定。**

### 4.3 Aggregator System Prompt 中的终止指令

```
## 终止条件
当你收集了足够的数据来回答用户的问题时，必须调用 submit_facts_to_render。
- 数据充分 → 立即提交
- 连续 2 次搜索空结果 → 立即提交（用 missing 注明未找到）
- 到达迭代上限前 1 轮 → 强制提交（用 missing 注明未完成）
不要尝试直接输出文本回答——你的唯一出口是 submit_facts_to_render。
```

## 5. 数据流与系统边界（v2 的核心）

```
用户消息
    │
    ▼
┌─────────────────────────────────────────────────────┐
│ Aggregator LLM (ReAct 循环内)                        │
│                                                     │
│ System Prompt: "数据聚合引擎" + TOOL_GUIDANCE +       │
│ _DEPTH_LEVELS[dt] + scene hints + 终止条件            │
│                                                     │
│ → 调 search / detail / characters / opinions          │
│ → 最后调 submit_facts_to_render(facts=[...])          │
│                                                     │
│ 输出: AIMessage(tool_calls=[{name: "submit_facts"}]) │
└───────────────────────┬─────────────────────────────┘
                        │
                        ▼ tool_node 执行 submit_facts_to_render
                        │
┌───────────────────────┴─────────────────────────────┐
│ ToolMessage(name="submit_facts_to_render",           │
│             content=JSON)                            │
│                                                     │
│ route_after_tool 检测到 submit_facts → 强制 END       │
└───────────────────────┬─────────────────────────────┘
                        │
                        │ 接口 A: ToolMessage.content = JSON string
                        │ (通过 function calling 管道，可靠性 >99%)
                        │
                        ▼
┌─────────────────────────────────────────────────────┐
│ main.py / 编排代码（确定性数据中转层）                  │
│                                                     │
│ 1. json.loads(ToolMessage.content) → facts dict      │
│ 2. 从 AgentState 提取系统状态（代码，不走 LLM）:        │
│    - iterations = state["iterations"]                │
│    - tools_used = 从 messages 提取 ToolMessage.name   │
│    - empty_searches = _count_consecutive_empty()      │
│ 3. Python 拼接 Markdown → render_input               │
└───────────────────────┬─────────────────────────────┘
                        │
                        │ 接口 B: Markdown string（Python 确定性生成）
                        │
                        ▼
┌─────────────────────────────────────────────────────┐
│ Render LLM                                          │
│                                                     │
│ System Prompt: 完整 Character Card (530字) +         │
│ _SNARK_LEVELS[snark] + _INITIATIVE_LEVELS[initiative]│
│ + _WORD_LIMITS[depth] + _CONSTRAINTS                 │
│ + {render_input}  ← 代码拼接的 Markdown               │
│                                                     │
│ → 自然语言回复                                        │
└───────────────────────┬─────────────────────────────┘
                        │
                        ▼
                 用户 + session cache
```

### 5.1 为什么是两个接口，不是一个

| | 接口 A: Aggregator→代码 | 接口 B: 代码→Render |
|---|---|---|
| **格式** | JSON（ToolMessage.content） | Markdown（prompt string） |
| **传输** | Function Calling 管道 | Python 字符串拼接 |
| **可靠性** | 模型 fine-tune 过 tool call，成功率 >99% | 代码 = 100% 确定性 |
| **消费者** | `json.loads()` | Render LLM |

Aggregator 不直接输出 Markdown 给 Render。中间夹一层代码做中转：

1. **解析 JSON**——确定性的 `json.loads()`，不需要猜格式
2. **提取系统状态**——从 AgentState 拿，不需要 LLM 回忆
3. **拼接 Markdown**——Python 字符串操作，100% 可预测格式

### 5.2 编排代码示例

```python
def _build_render_input(
    facts_json: dict,
    state: AgentState,
    user_query: str,
    depth: str,
) -> str:
    """确定性拼接 Render 的输入 prompt。不走 LLM。"""
    facts = facts_json.get("facts", [])
    intent = facts_json.get("intent", "未知")
    missing = facts_json.get("missing", "")

    # ── 系统状态（代码提取，不靠 LLM）──
    iterations = state.get("iterations", 0)
    messages = state.get("messages", [])
    tools_used = _extract_tools_used(messages)
    empty_count = _count_consecutive_empty_searches(messages)
    data_level = _data_level_label(depth)

    # ── 拼接 ──
    lines = [
        f"## 用户问题",
        user_query,
        f"",
        f"## 数据清单（{intent}）",
    ]
    for i, f in enumerate(facts, 1):
        name = f.get("name", "?")
        score = f.get("score")
        score_str = f"{score}分" if score is not None else "暂无评分"
        rank = f.get("rank")
        rank_str = f"#{rank}" if rank else ""
        summary = f.get("summary", "")
        tags = " / ".join(f.get("tags", []))
        line = f"{i}. {name} | {score_str} | {rank_str}"
        if summary:
            line += f" | {summary[:200]}"
        if tags:
            line += f" | 标签: {tags}"
        lines.append(line)

    lines.extend([
        f"",
        f"## 检索概况",
        f"搜索深度: {data_level} | 共 {iterations} 轮 | 调用: {', '.join(tools_used) if tools_used else '无'}",
    ])
    if empty_count >= 2:
        lines.append(f"⚠ 连续 {empty_count} 次搜索返回空结果——该关键词可能不存在于数据库。")
    if missing:
        lines.append(f"数据缺失: {missing}")

    return "\n".join(lines)
```

## 6. Prompt 架构：2 个 Builder + 数据中转

不需要 24 个 prompt。2 个 builder 从已有模块库取值：

```python
# nodes.py — Aggregator
aggregator_prompt = build_aggregator_prompt(
    depth_taste=0.90,
    intent=query_intent,
)

# main.py — 代码中转（新增）
render_input = _build_render_input(facts_json, state, user_query, depth)

# render.py — Render
render_prompt = build_render_prompt(
    character_key="bangumi_cold",
    snark=0.95,
    initiative=0.25,
    depth="deep",
    render_input=render_input,
)
```

### 6.1 Aggregator Prompt 骨架

```
你是数据聚合引擎。
- 不要尝试直接回答用户
- 调用工具获取数据
- 收集完毕后调用 submit_facts_to_render 提交事实清单
- 你的唯一出口是 submit_facts_to_render

## 工具
{TOOL_GUIDANCE}

## 搜索深度
{_DEPTH_LEVELS[depth_taste]}

## 场景提示
{scene_hints[intent]}

## 终止条件
数据充分 → 立即 submit_facts_to_render
连续 2 次空结果 → 强制提交（missing 注明）
```

### 6.2 Render Prompt 骨架

```
# 你是谁
{character_card}

# 今天的语气
{snark_tone}

# 节奏
{initiative_tone}

## 数据呈现规则
{_STYLE_BASE}

## 硬约束
{_CONSTRAINTS + word_limit}

{render_input}  ← 代码拼接的 Markdown，包含查询/数据/检索概况
```

## 7. 实施策略

### Phase 1：引入 submit_facts_to_render + 代码中转（核心改动）

1. 新增工具 `submit_facts_to_render`
2. 改 graph routing：`route_after_tool` 检测 `submit_facts` → 强制 END
3. 新增 `_build_render_input()` 在 main.py 中做确定性中转
4. Aggregator System Prompt：去掉 Character Card，加入聚合+终止指令
5. Render System Prompt：加入完整 Character Card
6. Aggregator 仍可调现有所有工具
7. session cache、L2 memory 写入逻辑不需要变（因为拿到的是最终回复）

### Phase 2：调优（可选）

1. 观察 tool call 成功率、Aggregator 是否稳定调用 submit_facts
2. 调优终止条件 prompt
3. 如果一切稳定，可以进一步精简 Aggregator 的 System Prompt

## 8. 不变的部分

| 组件 | 原因 |
|------|------|
| `tools/bgm_tools.py` | 所有工具不变，新增 submit_facts_to_render |
| `clients/` | HTTP 客户端不变 |
| `memory/short_term.py` | L1 滑动窗口不变 |
| `memory/long_term.py` | L2 不变 |
| `rag/` | RAG 不变 |
| `database/` | ORM 不变 |
| `schemas/tools_input.py` | Pydantic 输入不变 |
| `orchestrate/classifier.py` | 意图分类不变 |
| `orchestrate/guardrails.py` | XML 泄漏防护、重复调用检测不变 |

## 9. 要改的文件

| 文件 | 改动 |
|------|------|
| `tools/bgm_tools.py` | 新增 `submit_facts_to_render` 工具 |
| `agent/graph.py` | `tool_node → reasoning_node` 固定边改为条件边；新增 `route_after_tool` |
| `agent/orchestrate/prompt_builder.py` | 拆为 `build_aggregator_prompt()` + Aggregator 终止指令；Character Card 移除 |
| `agent/orchestrate/nodes.py` | tone_kwargs 只传 depth_taste |
| `agent/persona/profiles.py` | `_DEPTH_LEVELS` 文本转行为指令 |
| `agent/persona/render.py` | `build_render_prompt()` 接收完整 Character Card + render_input |
| `main.py` | 新增 `_build_render_input()` 中转函数；render 调用适配 |

## 10. v1 vs v2 对比

| | v1 | v2 |
|---|----|----|
| **退出机制** | Aggregator 决定不调 tool，开始写 Markdown | Aggregator 调 `submit_facts_to_render` → 代码检测 → 强制 END |
| **数据格式** | Aggregator 直接输出 Markdown 给 Render | JSON（Aggregator→代码）→ Python 拼 Markdown（代码→Render） |
| **检索状态** | Aggregator LLM 总结 `## 检索状态` | 代码从 AgentState 提取 |
| **系统边界** | 模糊（两个 LLM 之间直接传文本） | 清晰（代码层做中转，两个 LLM 不直接通信） |
| **控制流保证** | Prompt 约束（弱） | Function calling + 代码路由（强） |
