# Output Boundary — 输出边界渲染层 (设计规范 v2)

## 1. 核心思想

> **推理（Reasoning）与表达（Expression）解耦。采用六边形架构（Ports & Adapters），
> 将输出风格化视为独立的 output boundary，与 Agent 核心领域逻辑完全分离。**

### 1.1 架构类比

| 传统架构模式 | 在本项目中的映射 |
|-------------|-----------------|
| **Hexagonal / Ports & Adapters** | Agent 核心是 domain model，output boundary 是 port，每种风格是 adapter |
| **MVP (Model-View-Presenter)** | Model = 中性 AIMessage.content，Presenter = `render()`，View = 用户可见文本 |
| **Strategy Pattern** | `output_style` 选择具体策略，每种风格是一个 concrete strategy |

### 1.2 两个正交维度

**`agent_type` 决定"怎么想"（拓扑 + 工具策略），`output_style` 决定"怎么说"（表达风格）。**
两者完全独立，组合出四个合法象限：

```
                    agent_type (拓扑维度)
                    ─────────────────────
                    │ dialogue  │ research │
          ──────────┼───────────┼──────────┤
  output  │ neutral │ 中性快答   │ 中性深度报告│  ← 给 API/三方调用
  _style  │ bangumi │ 腹黑快答   │ 腹黑深度分析│  ← 面向 C 端用户
  (表达维度)│academic │ （未来）   │ （未来）   │
```

同一个 Bangumi娘风格，可以快速吐槽（dialogue + bangumi，现状），也可以深度锐评（research + bangumi，未来）——`render()` 逻辑不变，只是输入的中性文本更长。

### 1.3 Output Boundary 的未来扩展能力

渲染层作为唯一输出端口，未来所有"从 agent 核心到用户"的横切关注点都在此统一处理：

- 敏感信息过滤（R18 内容脱敏、个人信息打码）
- 多语言翻译（中性输出是中文，渲染层改写为日/英文）
- A/B 测试（同一回复多种风格并行渲染，测用户偏好）
- 速率限制/审计日志（在边界上记录每次输出的 token 消耗）

---

## 2. 架构分层

```
                         POST /chat
          { message, agent_type, output_style, ... }
                                 │
                                 ▼
┌────────────────────────────────────────────────────────────┐
│                    Agent 核心层（Domain Logic）              │
│                                                            │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐              │
│  │ Classifier│   │ Memory   │   │  Tools   │              │
│  │ 意图分类  │   │ 两层截断  │   │ 12 工具  │              │
│  └──────────┘   └──────────┘   └──────────┘              │
│                                                            │
│  ┌────────────────────────────────────────┐               │
│  │         StateGraph 拓扑（两个 Agent）    │               │
│  │                                        │               │
│  │  Research:  reasoning → tool → critic  │  ← 深度链式   │
│  │  Dialogue:  reasoning → tool           │  ← 浅层快速   │
│  │                                        │               │
│  │  System Prompt: 纯能力描述 + 工具策略    │               │
│  │  （无任何人设、语气、字数约束）            │               │
│  └────────────────────────────────────────┘               │
│                                                            │
│  agent_type 只控制走到哪个拓扑 —— 不影响输出表达             │
│                                                            │
│  输出: messages 列表 → _extract_final_reply() → 中性文本    │
└───────────────────────────┬────────────────────────────────┘
                            │
                    中性文本 (str)
                            │
          ╔═════════════════╪═════════════════╗
          ║         Output Boundary           ║
          ║     agent/personality/            ║
          ║                                  ║
          ║  render(content, style, llm)      ║
          ║  → "neutral" 透传（零延迟）        ║
          ║  → "bangumi" 轻量 LLM 改写        ║
          ║  → "academic"（未来注册）          ║
          ║                                  ║
          ║  render() 不关心 content 来自      ║
          ║  research 还是 dialogue            ║
          ╚═════════════════╪═════════════════╝
                            │
                     人格化文本 (str)
                            │
                            ▼
┌────────────────────────────────────────────────────────────┐
│                      ChatResponse                          │
│  { reply, iterations, tools_used, query_intent,           │
│    output_style }                                         │
└────────────────────────────────────────────────────────────┘
```

**关键设计决策：output boundary 只有一个，两个 agent 共享同一个 `render()` 函数。**
风格注册表是 agent-agnostic 的——加一种新风格，两个 agent 都能用。

### 2.1 默认风格差异（配置层，非 render 层）

```python
AGENT_DEFAULTS = {
    "research": {"output_style": "neutral"},   # 深度搜索 → 默认中性
    "dialogue": {"output_style": "bangumi"},    # 快速对话 → 默认人格
}
```

用户显式传 `output_style` 时覆盖默认值。

---

## 3. 数据流（一次完整请求）

```
POST /chat { message, agent_type="research", output_style="bangumi" }

① Agent 核心（research 拓扑）
   SystemMessage(中性能力描述 + intent 策略)
   → LLM 推理 → ToolMessage(数据) → LLM 综合 → AIMessage(中性结论)
   → Critic 评估中性结论 → PASS/REVISE
   → 最终 AIMessage.content:
     "根据 Bangumi 数据，EVA 系列中《Air/真心为你》评分最高（8.8分，排名156），
      其次是《破》（8.6分，排名234）。TV版评分为8.5分。"

② 提取中性文本
   _extract_final_reply(messages) → 上述 content

③ Output Boundary（共享）
   render(content=中性文本, style="bangumi", llm)
   → LLM (轻量 prompt, ~100 tokens, temperature 0.9)
   → 输出:
     "哼，《真心为你》8.8 分，EVA 厨用脚投票的结果呗。
      不过说真的，破 8.6 分已经够水的了，TV 版才 8.5 就更说明问题了——老粉滤镜害人不浅啊。"

④ 返回
   ChatResponse(reply=人格化文本, iterations=3, tools_used=[...],
                query_intent="lookup", output_style="bangumi")
```

---

## 4. 需要修改的接口

### 4.1 `ChatRequest` — 新增 `output_style` 字段

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `output_style` | `Literal["neutral", "bangumi", ...]` | `"neutral"` | 输出渲染风格。`"neutral"` 跳过渲染层直接返回原始回复。三方开发者可注册新风格。 |

`agent_type` 语义不变：`"dialogue"` 走 2 节点拓扑，`"research"` 走 3 节点拓扑。**`output_style` 只控制输出端的改写风格，与拓扑选择正交。**

### 4.2 `ChatResponse` — 新增 `output_style` 字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `output_style` | `str` | 实际使用的渲染风格，透传回调用方以便调试 |

### 4.3 Agent 默认风格（main.py 入口层）

```python
# 概念示意 — 不在 render() 内部做判断，在调用侧决定
_DEFAULT_STYLES = {
    "dialogue": "bangumi",
    "research": "neutral",
}

def _resolve_output_style(request: ChatRequest) -> str:
    """若调用方显式指定则用它，否则走 agent 类型默认值。"""
    if request.output_style and request.output_style != "neutral":
        return request.output_style
    if request.output_style == "neutral":
        return "neutral"  # 显式要求不渲染
    return _DEFAULT_STYLES.get(request.agent_type, "neutral")
```

### 4.4 System Prompt — 剥离人格设定

**现状**：`DIALOGUE_SYSTEM_PROMPT` 含完整 Bangumi娘人格（Role、Profile、Skills、Constrains、风格约束），`BASE_SYSTEM_PROMPT` 含部分风格指令。

**目标**：两个 agent 共用一套中性 System Prompt 体系，只描述能力和工具策略，不含任何人设、语气、字数约束。

需要剥离的内容：

| 当前所在 | 剥离项 | 目标归宿 |
|----------|--------|----------|
| `DIALOGUE_SYSTEM_PROMPT` | Role/Profile/Bangumi娘人设 | `personality/prompts.py` 的 `bangumi` 风格 |
| `DIALOGUE_SYSTEM_PROMPT` | Constrains 字数限制/风格约束 | 渲染层 `bangumi` 风格 + Agent 级轻量约束 |
| `DIALOGUE_SYSTEM_PROMPT` | "黑色幽默""反讽"等表达指令 | `personality/prompts.py` |
| `BASE_SYSTEM_PROMPT` | "回答风格：简洁、具体、可操作" | 保留在 Agent 层（这是能力指令，不是人格） |
| `BASE_SYSTEM_PROMPT` | "用中文回复""优先使用中文名" | 保留在 Agent 层（数据展示规范，不是风格） |

**保留在 Agent 层的**（与推理/工具/数据质量直接相关）：
- 能力描述（API 查询、语义搜索、常识推理）
- 工具依赖规则（`TOOL_DEPENDENCY_CONSTRAINT`）
- 数据模型约束（subject 有评分、character 无评分等）
- 输出格式规则（禁止 Markdown 表格、列表格式）
- 工具调用后必须生成文字回复的规则
- 退出条件（搜索无结果时诚实告知、不要无限重试）

### 4.5 新模块：`agent/personality/` — 唯一的 Output Boundary

```
agent/
├── personality/              ← 唯一的 output boundary（两个 agent 共享）
│   ├── __init__.py           # 公开接口: render(content, style, llm) -> str
│   ├── renderer.py           # 渲染引擎：调用 LLM 改写中性文本
│   ├── styles.py             # 风格注册表：{style_name: StyleConfig}
│   │                         #   + agent 默认值: AGENT_DEFAULTS
│   └── prompts.py            # 每种风格的渲染 System Prompt
│
├── research/                 ← domain logic（拓扑 + 中性 prompt）
│   └── ...
├── dialogue/                 ← domain logic（拓扑 + 中性 prompt）
│   └── ...
```

#### `renderer.py` — 渲染引擎接口（agent-agnostic）

```python
# 概念示意
async def render(
    neutral_content: str,
    style: str,
    llm: ChatOpenAI,
) -> str:
    """将中性回复改写为指定风格的文本。

    此函数不关心 neutral_content 来自 research 还是 dialogue agent。
    'neutral' 风格零延迟——直接返回输入内容，不调用 LLM。

    Args:
        neutral_content: _extract_final_reply() 提取的中性回复文本。
        style: 风格 key（"neutral", "bangumi", ...）。
        llm: LLM 实例（由调用侧注入，render 层不自建）。

    Returns:
        风格化后的文本。"neutral" 风格原样返回输入。

    Raises:
        RenderError: 渲染 LLM 调用失败时。调用侧应 catch 并降级为中性文本。
    """
```

#### `styles.py` — 风格注册表（agent-agnostic）

| 风格 key | 名称 | 说明 | System Prompt |
|----------|------|------|---------------|
| `neutral` | 中性 | 不做渲染，直接返回 Agent 原始输出 | 无（跳过 LLM） |
| `bangumi` | Bangumi娘 | 腹黑萝莉吐槽役，黑色幽默，30-150 字 | `RENDER_BANGUMI_PROMPT` |
| *(预留)* | | 三方开发者注册新风格 | 自定义 |

风格注册表对两个 agent 完全平等——**不存在"这是 dialogue 专用风格"或"这是 research 专用风格"的概念。**
如果未来某风格需要限制可用范围，在 StyleConfig 里加 `allowed_agents` 字段，render 逻辑不变。

#### `prompts.py` — 渲染 Prompt 约束

每个渲染 prompt 必须遵守以下契约：

1. **只能改写表达**：可以换措辞、换语气、加吐槽，但不能凭空添加新数据（评分、排名、名称等）
2. **不能删除数据**：Agent 给出的每条具体数据必须在渲染后保留
3. **保留引用格式**：如果中性输出含 `中文名 ⭐评分` 格式，渲染后仍保持可辨识
4. **字数约束由渲染层控制**：每种风格自带字数范围，Agent 核心不再关心

### 4.6 `main.py` — 响应构建管道

当前 `_chat_dialogue` / `_chat_research` 的流程：

```
ainvoke(graph) → _extract_final_reply(messages) → ChatResponse(reply=...)
```

目标流程：

```
ainvoke(graph)
  → _extract_final_reply(messages)         # 中性文本
  → _resolve_output_style(request)         # 确定风格（显式 or agent 默认）
  → render(content, style, llm)            # Output boundary（共享）
  → ChatResponse(reply=..., output_style=...)
```

`/chat/stream` 同理——在 SSE 流的末尾，用渲染后的文本替换原始内容，或同时推送 `neutral_reply` + `styled_reply` 两个字段。

### 4.7 跳过渲染的快捷路径

以下情况应跳过渲染层，直接返回中性文本：

| 条件 | 原因 |
|------|------|
| `output_style == "neutral"` | 显式要求不渲染 |
| Agent 返回异常/兜底消息 | 渲染可能扭曲错误信息，用户需要看到原始错误 |
| 渲染 LLM 调用失败 | 降级返回中性文本，不阻塞响应 |
| `query_intent == "chitchat"` 且中性回复 < 50 字 | 闲聊不必 double pass（可选优化） |

---

## 5. 不变的部分（开发者可放心）

以下模块本次设计**不涉及任何修改**：

| 模块 | 原因 |
|------|------|
| `agent/research/graph.py` | 拓扑不变 |
| `agent/research/state.py` | 状态字段不变 |
| `agent/research/nodes.py` | reasoning/critic 节点逻辑不变 |
| `agent/dialogue/graph.py` | 拓扑不变 |
| `agent/dialogue/state.py` | 状态字段不变 |
| `agent/dialogue/nodes.py` | reasoning 节点逻辑不变（prompt 内容变化但由 `build_*_prompt()` 透明处理） |
| `agent/classifier.py` | 意图分类不受影响 |
| `agent/memory.py` | 两层截断不变 |
| `agent/llm.py` | LLM 工厂不变（渲染层由调用侧注入 LLM 实例） |
| `tools/bgm_tools.py` | 工具定义不变 |
| `rag/` | RAG 检索管线不变 |
| `database/` | 数据库层不变 |
| `schemas/tools_input.py` | 工具输入合约不变 |

---

## 6. 实施步骤（建议顺序）

| Step | 内容 | 影响范围 |
|------|------|----------|
| 1 | 新建 `agent/personality/` 模块，实现 `render()` + `bangumi` 风格 prompt + `STYLE_REGISTRY` | 新增，不影响现有代码 |
| 2 | `ChatRequest` / `ChatResponse` 加 `output_style` 字段，默认 `"neutral"` | `main.py` 请求/响应模型 |
| 3 | `main.py` 响应管道插入 `render()` 步骤 + `AGENT_DEFAULTS`，`/chat` + `/chat/stream` | `main.py` |
| 4 | 剥离 `DIALOGUE_SYSTEM_PROMPT` 中的人格内容，迁移到 `personality/prompts.py` | `agent/dialogue/prompts.py` |
| 5 | 剥离 `BASE_SYSTEM_PROMPT` 中的风格指令，保留纯能力/策略描述 | `agent/research/prompts.py` |
| 6 | 更新测试：中性输出正确性 + 渲染风格一致性 + 四个象限组合 | `test/` |
| 7 | 更新 CLAUDE.md 架构文档 + 本设计文档 | `CLAUDE.md` |

Step 1-3 可以独立完成并上线（`output_style` 默认 `"neutral"`，行为与当前完全一致），Step 4-6 是渐进式剥离，可在后续迭代中逐步推进。Step 3 完成后即可验证"agent_type × output_style 四象限"的可用性。

---

## 7. 设计约束 & 风险

| 约束 | 措施 |
|------|------|
| 渲染 LLM 调用增加延迟 | 渲染 prompt 极短（~100 tokens system + ~200 tokens neutral content），预计 300-500ms 增量。对 research（2-5 次 LLM 调用）可忽略；对 dialogue（1 次 LLM 调用）影响约 30-50% 延迟。**可接受**——dialogue 当前预算 <2s，增加后仍在预算内。 |
| 渲染层可能编造数据 | 渲染 System Prompt 硬约束："只能改写表达，禁止新增评分/排名/名称等具体数据"。必要时做 diff 校验——渲染输出中的数字必须全部出现在中性输入中。 |
| Critic 评估对象改变 | Critic 仍然评估中性输出（Agent 层的 AIMessage），不受渲染影响。如果未来想在渲染后再评估整体质量（含风格表现），加一个可选的 post-render style critic。 |
| `render()` 共享但两个 agent 输入差异大 | research 的中性输出可能很长（包含多条工具数据），dialogue 的较短。渲染 prompt 需对长输入做长度自适应——超过 500 字的输入不做完整改写，改为"压缩 + 风格化"。 |
| 简单闲聊 double pass | `query_intent == "chitchat"` 且中性回复 < 50 字时跳过渲染层（规则化，不调 LLM）。 |
