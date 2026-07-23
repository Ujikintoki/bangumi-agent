# 架构宏观评审 — 设计决策层面的方向性问题

> 2026-07-22，继 Phase 5.5 人格化渲染评审之后，对 LangGraph Agent 架构的宏观设计审查。
>
> 此前发现并修复的问题：**"数据查询 + 人格刷漆"的架构倒置**——角色人格被当作事后油漆涂在信息检索系统上，而非第一性身份。
> 修复方案：角色优先（role-first）的 prompt 组装、意图扩展（debate/emotional）、tone profiling。
>
> 本次评审延续同一方法论：不是找具体 bug，而是找**设计决策层面的方向性倒置**。

---

## 发现一：双 Agent 分裂 —— 用路由层解决模型层的问题

### 现象

两个独立的 Graph（Research / Dialogue），两套 node 函数（`research_reasoning_node` / `dialogue_reasoning_node`），用户通过 `agent_type` 参数手动选择。

### 关键文件

| 文件 | 角色 |
|------|------|
| `main.py:74-76` | `agent_type` 参数定义，默认 `"dialogue"` |
| `main.py:187-189` | `if request.agent_type == "research"` 路由分支 |
| `agent/research/graph.py` | 3 节点 ReAct 拓扑，`_MAX_ITERATIONS=12` |
| `agent/dialogue/graph.py` | 2 节点 ReAct 拓扑，`_MAX_ITERATIONS=4` |
| `agent/research/nodes.py` | `research_reasoning_node` (~285 行) |
| `agent/dialogue/nodes.py` | `dialogue_reasoning_node` (~275 行) |
| `agent/research/state.py` | `AgentState` — 10 字段（含 critic_status, critic_feedback, error_flag） |
| `agent/dialogue/state.py` | `DialogueState` — 7 字段（无 Critic 相关） |

### 两个 Agent 的实际差异

所有差异都是**配置性的**，不是架构性的：

| 差异 | Dialogue | Research | 可统一为 |
|------|----------|----------|----------|
| 迭代上限 | 4 | 12 | `max_iterations: int` |
| Critic | 无 | 有（rule/llm） | `critic_mode: "none" \| "rule" \| "llm"` |
| 默认人格 | bangumi | neutral | `output_style: str` |
| Token 预算 | 4000 | 8000 | `max_context_tokens: int` |
| L2 语义阈值 | 0.35 | 0.50 | `memory_threshold: float` |
| L2 注入预算 | 300 | 700 | `memory_max_tokens: int` |
| 工具输出模式 | compact | full | 已通过 `_tool_agent_type` contextvar 统一 |
| 结尾收敛 | last-chance bailout | Critic REVISE | `convergence_strategy` |

它们共享：
- 同一套 14 个 `@tool`
- 同一个 `classify_intent` 分类器
- 同一个 `MemoryManager.recall_for_prompt()`
- 同一个 `build_system_prompt()` builder
- 同一个 `create_llm()` LLM 工厂
- 同一个 `SessionCache`
- 同一套 guardrails（`strip_tool_call_xml`, `is_terminal_response`, `check_duplicate_tool_calls`）

### 两个 reasoning node 的重复度

```
┌─────────────────────────────────────────────────────────┐
│ 意图分类：完全相同                                        │
│ 记忆召回：阈值和预算不同，其余相同                          │
│ System Prompt 构建：不同 builder，相同调用模式             │
│ SystemMessage 替换：完全相同                               │
│ LLM 工具绑定决策：相同逻辑（_NO_TOOL_INTENTS）              │
│ 记忆截断：预算不同，其余相同                                │
│ XML 泄漏防护：Research 多一个消化态检查                     │
│ 重复工具调用检测：Dialogue 在 node 内，Research 在 Critic   │
│ contextvar 注入：完全相同                                   │
│ 日志格式：相同                                             │
└─────────────────────────────────────────────────────────┘
```

### 为什么这是架构倒置

**核心错误：把"这次对话需要多深"的判断推给了用户。**

这类似于让用户在打电话前选择"我要简短通话"还是"我要详细讨论"——正常产品中，对方的智能程度应该自动适配对话深度。

具体后果：
1. **用户用 Dialogue 模式问研究级问题**（"帮我详细分析高达系列各作的评分趋势"）→ 被 4 轮限制卡住，且无 Critic 纠错
2. **用户用 Research 模式闲聊**（"你好"）→ 浪费 Critic LLM 调用，且 Research 默认 neutral 人格
3. **后续所有功能要做两次**——新 guardrail → 两个 node 各加一遍，新状态字段 → 两份 TypedDict 各加一遍
4. **调试 blame chain 变长**——输出不好时，是 agent_type 选错了，还是 LLM 用错了工具，还是分类器判错了意图？

在 ChatGPT、Claude 等产品中，用户不需要选择模式——模型自己判断这条消息是闲聊还是研究。LangGraph 完全支持在**同一个 Graph 内**通过条件边实现不同的行为路径，不需要拆成两个完全独立的图。

### 正确的方向

一个统一的 Graph，模型自行决定行为深度：

```
同一推理节点:
  - 模型自己判断"这轮需要调工具还是直接回复"
  - 模型自己判断"信息够了该停了"
  - 模型自己判断"用哪种语气"

收敛策略由 Graph 的条件边在运行时根据实际状态决定，
不由用户提前声明。
```

双 Agent 可能在**非常后期的优化阶段**有意义（为 Dialogue 专门训练一个小模型），但在当前阶段——两个 Agent 用同一个 LLM、同一套工具——这是过早的架构分裂。

---

## 发现二：意图预分类 —— 最弱的组件决定了最强组件的能力

### 现象

用户在 Prompt 中输入的原始文本，先经过关键词 + 正则的分类器，做出**不可逆的工具绑定决策**，然后才到达 LLM。

### 关键文件

| 文件 | 角色 |
|------|------|
| `agent/classifier.py:58-271` | `INTENT_RULES` — 6 组关键词/正则列表，优先级有序 |
| `agent/classifier.py:305` | `_NO_TOOL_INTENTS = frozenset({"chitchat", "factual", "debate", "emotional"})` |
| `agent/research/nodes.py:169` | `if query_intent in _NO_TOOL_INTENTS: llm_to_use = llm` |
| `agent/dialogue/nodes.py:150` | 同上 |
| `agent/research/nodes.py:223-237` | chitchat/factual XML 泄漏自纠正（补救措施） |
| `agent/dialogue/nodes.py:214-229` | 同上 |

### 决策链

```
用户输入
    │
    ▼
classify_intent_rule()     ← Python for 循环 + regex（~50 行代码）
    │
    ▼
_NO_TOOL_INTENTS 判断      ← 硬编码 frozenset
    │
    ├─ intent in NO_TOOL → LLM 看不到工具 → 直接回复
    │                                          │
    │                           如有工具意图泄露 → XML 自纠正 → 再调一次 LLM（带工具）
    │
    └─ intent not in NO_TOOL → LLM 看到 14 个工具 → 自主决定
```

### 为什么这是架构倒置

**能力最弱的层做了不可逆决策，能力最强的层被动接受。**

```
能力最弱的组件                    能力最强的组件
（关键词 + 正则，                 （DeepSeek v4，
  ~50 行代码，                     1M 上下文窗口，
  零推理能力，                     强推理能力，
  硬编码规则）                     工具使用能力）
     ↓                                ↓
 决定 LLM 能不能                    被限制了选择空间
 看到工具
```

这里的隐藏假设是："LLM 不应该为闲聊绑定工具，浪费 ~2000 token 的工具 schema。" 但在 2026 年的 1M 上下文模型上，2000 token 的成本（约 $0.001）远低于**分类器误判 + 自纠正重试**的代价。

具体后果：
1. **"你好，EVA 评分怎么样？"** → 匹配 `keywords: ["你好"]` → chitchat → LLM 看不到工具 → 只能凭空说 → 用户得到没有评分的回复
2. **"无聊死了，有什么好看的番推荐吗"** → 匹配 `keywords: ["好无聊"]` → emotional → 不绑工具 → LLM 凭空推荐 → 没有评分、没有真实数据
3. **调试 blame chain**："为什么 LLM 没有调用搜索？" → 因为分类器判了 chitchat → "为什么分类器判了 chitchat？" → 因为关键词表里某条规则 → "那改关键词表" → 改了 → 可能破坏另一条规则的匹配
4. **关键词表维护成本**：新增一个意图 = 选关键词（需要考虑歧义、优先级、语言变体）+ 配正则 + 决定 NO_TOOL + 改 INTENT_PROMPTS + 改 LLM fallback prompt。一共改 5 个地方，一个意图。

XML 泄漏自纠正机制是**症状的证据**——系统的存在本身就说明设计假设有问题。如果分类器是正确且充分的，就不需要 LLM 来纠正它。

### 正确的方向

有三种方向，按激进程度递增：

**A. 保守：分类器只做 hints，LLM 始终有工具**

分类器结果作为 `query_intent` 注入 prompt，但不决定工具绑定。LLM 始终看到所有工具，自行决定是否调用。"你好"的 LLM 会自然跳过搜索——它不需要关键词来告诉它。

**B. 中等：分类器只决定 System Prompt 变体，不影响工具**

工具始终绑定，intent 只能改变 System Prompt 中的策略描述。`chitchat` 的 prompt 说"这是闲聊，尽量不调工具"，但 LLM 可以 override。

**C. 激进：分类器完全移除，LLM 自分类**

"意图分类"不是独立任务——它是推理过程的一部分。让 LLM 在 System Prompt 中看到"首先判断用户意图，然后决定策略"，在一次推理中完成。节约一次 LLM 调用（分类器），也消除了双组件协同的 failure mode。

---

## 发现三：记忆召回是一次性的、位置锁死的

### 现象

L2 记忆召回发生在推理节点入口，仅基于用户原始输入，之后永不更新。

### 关键文件

| 文件 | 角色 |
|------|------|
| `agent/memory_manager.py:125-284` | `recall_for_prompt()` — 完整召回管道 |
| `agent/research/nodes.py:102-130` | 召回触发逻辑 + `_memory_context` 缓存 |
| `agent/dialogue/nodes.py:83-112` | 同上（阈值更严格） |

### 数据流

```
┌─ reasoning_node 入口 ───────────────────────────────────────────┐
│                                                                  │
│  1. memory_context = state.get("_memory_context", "")           │
│  2. if not memory_context:                                       │
│       memory_context = await mm.recall_for_prompt(user_query)    │
│       // ↑ 只基于用户原始输入，只调用一次                           │
│  3. 注入 System Prompt                                           │
│  4. LLM invoke → tool_calls → tool_node → back to reasoning     │
│  5. memory_context 已缓存 → 跳过 → 不会基于工具结果重新查询         │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

### 具体场景

```
用户: "推荐类似高达的作品"
  → recall("推荐类似高达的作品")  → 召回: 上周聊 EVA ✅
  → Agent 搜索 → 发现 "机动战士高达SEED" → 推荐
  → 但用户三周前详细讨论过 SEED 的每个角色和评分！
  → 这条记忆可能被"高达"匹配召回，但排在后面被截断
  → 更关键的是：Agent 在执行过程中发现了"SEED"这个具体实体，
     但记忆系统不会再被查询，不会召回"三周前聊 SEED"的上下文
```

### 为什么这是架构倒置

**记忆被建模为"开门前拿的钥匙"——只在入口处使用一次——而非"口袋里随时摸的工具"——推理的任何阶段都可以触发。**

```
当前模型:
  入口 → [拿钥匙] → 进门 → 所有房间用同一把钥匙

正确模型:
  入口 → 进门 → 每个新房间 → [需要时摸口袋拿对应的钥匙]
```

这里的隐藏假设是："用户当前查询的措辞是唯一需要的记忆密钥。" 但实际上，推理过程中发现的新实体、新关系，往往比用户原始措辞更能触发有效记忆。

具体后果：
1. **多轮工具链中产生的中间实体无法触发记忆**：search → 发现作品 A → 用户三个月前聊过 A → 不会召回
2. **记忆注入的时机太早**：System Prompt 中最前面的记忆文本会被后续的 tool results 稀释注意力
3. **工具返回的结构化实体（subject_id, character_id）无法用于精确记忆匹配**：即使数据库里有该实体的历史对话记录，系统也不会去查

### 正确的方向

**记忆检索改为 on-demand 能力**，类似一个工具：

```
在 System Prompt 中给 LLM 一个"回忆"工具:
  recall_memory(entity_name: str) → "用户上次讨论该实体时的上下文"

或者更轻量：
  在每次 tool_node 返回结果后，自动基于工具结果中的实体名
  触发增量记忆召回，追加到上下文（非阻塞、低延迟）
```

最理想但最复杂的方案：记忆不是注入到 prompt，而是作为 pgvector 工具的一部分——`search_local_bangumi` 已经在查 RAG 实体表，可以同时查 `session_memories` 表，返回"用户上次关于这个实体的对话摘要"。这是真正的深度融合。

---

## 共同线索：上游弱组件对下游强组件做不可逆决策

三个问题共享同一个模式：

```
能力梯度:  弱 ──────────────────────────────→ 强

发现一:  用户选择 agent_type   →   限制 LLM 行为深度
              ↑                         ↑
         开关（1 bit 信息）          最强推理组件

发现二:  关键词正则匹配        →   决定 LLM 能不能看到工具
              ↑                         ↑
         字符串匹配（零推理）          最强推理组件

发现三:  入口处一次性查询       →   锁定整次推理的记忆上下文
              ↑                         ↑
         单一 query embedding          最强推理组件 + 结构化工具数据
```

在每种情况下，信息量最少、推理能力最弱的组件，对能力最强的组件施加了不可逆的约束。

### 对比此前修复的人格化倒置

此前发现并修复的问题也遵循同一模式：

```
修复前: 数据查询结果 → 事后用"人格"刷漆 → 输出
           ↑                                  ↑
        信息检索系统                          LLM（被当作文案改写器）

修复后: 角色身份（第一层）→ 能力（附属）→ 数据查询 → 输出
           ↑
        LLM 看到的第一个东西就是"你是谁"，
        工具调用是人格的表达方式，而非独立层
```

### 诊断框架

要发现其他类似问题，可以问三个问题：

1. **"这个决策由谁在做？"** 如果答案是最弱的组件（regex、hardcoded constant、user flag），而最强组件（LLM）只能被动接受，可能是倒置。

2. **"这个决策可以被撤销吗？"** 如果下游组件无法推翻上游决策（除了用 hack 式的自纠正机制），可能是倒置。

3. **"这个组件做决策时，掌握了多少信息？"** 如果它只能看到用户原始输入（而非完整的推理上下文中涌现的信息），可能是倒置。

### 怀疑区域（未深入审查，按此框架可能也有问题）

| 区域 | 文件 | 为什么可疑 |
|------|------|-----------|
| **Critic 的 `< 10 chars` 硬阈值** | `agent/research/nodes.py:428` | 正则/数字阈值决定 LLM 输出是否"合格"，而非让 LLM 自行判断 |
| **`TOOL_DEPENDENCY_CONSTRAINT` 硬编码** | `agent/research/prompts.py:28-49` | 工具依赖关系由开发者手写维护，而非 LLM 从工具 docstring 自行推断 |
| **Sanitizer 的字段白名单** | `clients/sanitizers.py` | API 返回的哪些字段被保留/丢弃，由开发者硬编码决定。LLM 看不到被丢弃的字段，即使它可能对当前查询有用 |
| **`output_style` 四象限** | `agent/profiles.py` | "角色身份表达"的切换仍然是一个用户参数，而非 LLM 根据对话上下文自行切换 |
| **`_MAX_ITERATIONS` 硬数字** | `agent/research/state.py:79` | 循环上限是固定数字，而非 LLM 收到收敛信号（"我应该在 N 轮内完成任务"）后的自适应行为 |
| **`is_terminal_response` 正则列表** | `agent/guardrails.py:24-46` | "LLM 的回复是否算终端"由 ~10 条正则判定，而非 LLM 自身判断 |

---

## 讨论要点

在新 session 中继续分析时，可以按以下顺序：

1. **确认/质疑这三个发现**——是否存在我误判了的情况？是否存在已有设计理由推翻了这些判断？
2. **按诊断框架逐一审查"怀疑区域"**——每个怀疑区域是否符合"弱组件决定强组件"的模式？
3. **决定修复优先级**——哪些倒置是"能跑但哲学上不对"，哪些是"已经在产生可观测的劣化"？
4. **制定重构路径**——是否存在一个最小改动集合，能同时解决多个倒置问题？

---

## 相关文件索引

| 类别 | 文件 |
|------|------|
| Graph 拓扑 | `agent/research/graph.py`, `agent/dialogue/graph.py` |
| State 定义 | `agent/research/state.py`, `agent/dialogue/state.py` |
| 推理节点 | `agent/research/nodes.py`, `agent/dialogue/nodes.py` |
| 分类器 | `agent/classifier.py` |
| 记忆管理 | `agent/memory_manager.py`, `agent/memory.py`, `agent/session_cache.py` |
| Prompt 构建 | `agent/prompt_builder.py`, `agent/profiles.py`, `agent/research/prompts.py` |
| Guardrails | `agent/guardrails.py` |
| 入口 | `main.py` |
| 工具 | `tools/bgm_tools.py`, `clients/sanitizers.py`, `clients/client.py`, `clients/base.py` |
| RAG | `rag/retriever.py`, `rag/ingestion.py`, `database/rag_tables.py` |
| 记忆 ORM | `database/memory_tables.py` |
| 配置 | `core/config.py` |
| 此前评审 | `docs/design/personality-rendering-layer.md` |
| 此前修复 | `docs/design/ROADMAP.md` (Phase 5.5 section) |
