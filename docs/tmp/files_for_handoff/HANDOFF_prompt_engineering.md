# HANDOFF — BGM Agent System Prompt 工程设计

> 2026-08-09 | 从 persona hardening 讨论中分离出来的独立话题
> 关联文档：`HANDOFF_persona_hardening.md`（第 3 批参数调整）、`../design/new-another-review.md`

## 背景

在推进 persona hardening（第 3 批参数调整）的过程中，用户发现当前的 system prompt 组织方式是 2 个月 vbe 迭代的自然产物——功能不断叠加、每个 section 独立添加，但没有经过统一的工程设计 review。用户希望了解：

1. 当前两条 System Prompt 的完整组成
2. 业界成熟项目（Anthropic 推荐、MemGPT/Letta、SillyTavern Character Card）的 prompt 设计模式
3. 当前设计的优势和差距
4. 改进方向和优先级

本文档作为独立的任务记录，供后续对话中推进。

---

## 一、当前 System Prompt 架构全貌

BGM Agent 有**两条 System Prompt**，分别注入两个独立的 LLM 调用：

### Prompt 1：Aggregator System Prompt（reasoning 层）

- **构建函数**：`agent/orchestrate/prompt_builder.py` `build_aggregator_prompt()`
- **注入位置**：`agent/orchestrate/nodes.py:274-280`
- **LLM 能力**：function-calling，全量工具 bind
- **职责**：调工具 → 收集数据 → 输出结构化文本摘要

```
┌──────────────────────────────────────────────────────────┐
│ Section 1: Aggregator Identity          ~80 字            │
│   "你是数据聚合引擎。你的工作：调用工具获取数据..."        │
├──────────────────────────────────────────────────────────┤
│ Section 1.5: Few-Shot Examples          5 条正路径示例     │
│   search→detail→输出 / 并行调用 / 空结果 / person detail  │
├──────────────────────────────────────────────────────────┤
│ Section 2: 搜索深度指令                 depth_taste 5 档   │
│   SHALLOW/BASIC/STANDARD/THOROUGH/EXHAUSTIVE              │
├──────────────────────────────────────────────────────────┤
│ Section 3: 工具指引 (TOOL_GUIDANCE)     ~200 字            │
│   何时查 / 多少算够 / 并行规则 / 数据真实性               │
├──────────────────────────────────────────────────────────┤
│ Section 4: 对话连续性 (_CONTINUITY_RULES)                 │
│   代词回指 / 新话题判断 / 模糊兜底                        │
├──────────────────────────────────────────────────────────┤
│ Section 5: Scene Hint                   按 intent 注入     │
│   "[当前：用户在查具体信息。快速定位...]"  ~60 字          │
├──────────────────────────────────────────────────────────┤
│ Section 6: Memory Context               仅首轮，L2 召回    │
│   格式化的用户历史文本（裸 Markdown）                      │
├──────────────────────────────────────────────────────────┤
│ Section 7: 输出约束                     按 depth 字数      │
│   "文本摘要不超过 {word_limit} 字"                        │
├──────────────────────────────────────────────────────────┤
│ Section 8: 终止规则                     放最后（近因效应）  │
│   "直接输出文本摘要 = 结束" / 数据足够的判断标准            │
└──────────────────────────────────────────────────────────┘
```

工具（16 个 `@tool` 函数）通过 LangChain `bind_tools()` 直接注入 function calling schema，不写在 System Prompt 正文中。

### Prompt 2：Render System Prompt（人格层）

- **构建函数**：`agent/persona/render.py` `build_render_prompt()`
- **注入位置**：`main.py:338` → `render_reply()` → 独立 LLM 调用
- **LLM 能力**：纯文本生成，不调工具
- **职责**：接收结构化数据 + 用户问题 → 输出人格化回复

```
┌──────────────────────────────────────────────────────────┐
│ Section 1: # 你是谁 (Character Card)     ~500 字           │
│   审美体系 / 数据态度 / 自我认知 / 语言约束                │
├──────────────────────────────────────────────────────────┤
│ Section 2: ## 今天的语气 (snark)         _SNARK_LEVELS     │
│   "今天你标准很高。有些作品该被 diss——因为你对媒介有要求"  │
├──────────────────────────────────────────────────────────┤
│ Section 3: ## 回复节奏 (initiative)      _INITIATIVE_LEVELS│
│   "今天节奏正常。有话说就说，没话说就停"                   │
├──────────────────────────────────────────────────────────┤
│ Section 4: ## 说话风格 (STYLE_BASE)                       │
│   评分随口带过 / 结论先行 / 不暴露内部信息 / "说人话"      │
├──────────────────────────────────────────────────────────┤
│ Section 5: ## 用户问题                                     │
│   用户原始问题原文注入                                     │
├──────────────────────────────────────────────────────────┤
│ Section 6: ## 系统数据                                     │
│   <system_retrieved_facts> XML 标签包裹的结构化数据         │
├──────────────────────────────────────────────────────────┤
│ Section 7: ## 硬约束                                       │
│   字数限制 / 不用 emoji / 不编造数据 / 不加前后缀          │
└──────────────────────────────────────────────────────────┘
```

Render 不调工具、不访问数据库。

---

## 二、当前设计的优势（保留！）

1. **两阶段分离（Aggregator → Render）**。让 reasoning 和 persona 分属两个 LLM 调用，每个有单一职责——这是正确的架构决策。MemGPT/Letta 也是类似思路。
2. **终止规则放最后**。LLM 对 prompt 末尾注意力权重最高，"输出文本 = 结束"写在末尾利用了近因效应。
3. **Character Card 作为完整人格块**。SillyTavern 的 Character Card spec 就是这个思想——角色定义不是碎片化指令，是一个有审美体系的人设文本。你的 ~500 字比大多数开源项目的人设都更有深度。
4. **`<system_retrieved_facts>` XML 标签**。Anthropic 官方推荐用 XML 包裹结构化数据，让模型区分"指令"和"数据"。

---

## 三、与业界成熟模式的差距

### 差距 1：缺少 section 边界标记

**现状**：所有 section 用 `\n\n` + Markdown header（`## xxx`）拼接，模型看到的是连续文本流。

**业界做法**（Anthropic 推荐、MemGPT 采用）：用 XML 标签创建闭合的 section 边界。

```xml
<system>
  <role>你是数据聚合引擎</role>
  <capabilities>...</capabilities>
  <limitations>你不能表达观点，不输出人格化语言</limitations>
</system>

<instructions>
  <search_depth>...</search_depth>
  <tool_rules>...</tool_rules>
  <termination>...</termination>
</instructions>

<context>
  <user_intent>...</user_intent>
  <memory>...</memory>
</context>
```

XML 标签比 Markdown header 更强——闭合标签告诉模型"这个 section 结束了"。

### 差距 2：Aggregator Identity 缺少 negative definition

**现状**（80 字）：
> 你是数据聚合引擎。你的工作：调用工具获取数据，整理后输出一份简洁的数据摘要。

**遗漏**：没有告诉模型"你不是什么"。negative definition 在 prompt engineering 中往往比 positive definition 更有效。

**应该加**（40 字）：
> 你不是 Bangumi 看板娘，不陪用户聊天。你的输出会被下游 Render 系统改写——你只需要准确的数据，不需要考虑语气。

### 差距 3：Memory Context 无结构化包裹

**现状**：L2 记忆以裸 Markdown 文本注入（`## 用户历史\n- [昨天] 讨论了高达SEED`），混在连续文本流中。

**业界做法**（MemGPT core memory block）：
```xml
<memory>
  <recent>- [昨天] 用户说 EVA 是神作</recent>
  <user_profile>偏好机战和原创动画</user_profile>
</memory>
```

让 memory 和 instructions 在结构上隔离，避免模型把"用户历史"误解为"系统指令"。

### 差距 4：Few-shot 只有正路径

5 条示例全是 `search → 成功 → 输出`。缺：
- 空结果尽早终止的示例
- 用户意图模糊时追问的示例
- 工具调用失败后诚实回退的示例

### 差距 5：缺少显式 output format schema

**现状**：
> 直接输出文本摘要——你的工作就完成了

模型不知道"文本摘要"的具体格式要求。

**业界做法**：
```xml
<output_format>
每条信息一行。缺失标注"暂无"。不加修饰语、不加"根据搜索结果"、不加个人判断。
</output_format>
```

### 差距 6：Render 层没有 few-shot 对话示例

Character Card 规范（SillyTavern 生态）的 `example_messages` 字段是角色一致性的最强约束——比任何规则描述都有效。你的 Render 层只有抽象的人格描述，没有具体的对话范本。

---

## 四、业界参考模式速览

### Anthropic System Prompt 三段式

```
System Prompt = <role> + <instructions> + <data>

role:      固定，定义"我是谁、边界在哪"
instructions: 半固定，按场景注入（≈你的 Scene Hint + search depth）
data:      动态，每次调用注入（≈你的 memory + system_retrieved_facts）
```

三段用 XML 标签清晰分隔。

### Character Card 规范（SillyTavern 生态，200+ 开源人设卡）

```
Character Card = {
  name, description, personality, scenario,
  first_message, example_messages, system_prompt
}

description:     长篇人设（≈你的 Character Card）
personality:     标签式总结（≈你的 identity 字段）
example_messages: 对话示例（你缺少）
```

### MemGPT/Letta Memory Blocks

```
System Prompt =
  [persona block]    静态，角色定义
  [human block]      静态，用户定义
  [functions]        动态，工具定义
  [core memory]      动态，关键记忆（≈L2）
  [archival memory]  动态，长尾记忆
  [instructions]     半静态，行为规则
```

核心思想：**memory 是一等 citizen**，有独立的 block 和注入逻辑，不是"追加一个 section"。

---

## 五、成本与延迟：当前严重缺乏考虑

### 5.1 当前状态：零成本意识

**现状一句话**：prompt 拼完就发，不考虑用了多少 token、花了多少时间和钱。

具体表现：
- 无 prompt caching（DeepSeek 不支持，Anthropic 有 `cache_control` 但项目没用到）
- Few-shot 5 条示例**每轮推理都完整发送**（~500 tokens 每轮，deep 模式 12 轮 = 6,000 tokens 浪费在重复示例上）
- 工具返回结果**零压缩**——`get_bangumi_subject_detail` 返回完整 infobox（含所有评分分布、标签、简介），全量喂回 LLM
- 17 个工具的 JSON Schema **全量 bind**（约 1,400 tokens），即使 chat intent 一个工具都不调
- 无 token 预算分配到各组件——L1 memory 有预算，但 few-shot、tool schema、工具结果都没有
- DEV_MODE 虽然记录 token 消耗（`devtools.py`），但**从不分析**——记录了但不看

### 5.2 Token 成本估算（per-request，基于 DeepSeek V3 定价）

**各组件 token 占用实测：**

| 组件 | tokens | 发送频率 | 备注 |
|------|--------|---------|------|
| Aggregator Identity + 规则（静态） | ~400 | 每轮推理 | TOOL_GUIDANCE + 连续性 + 终止规则 |
| Few-shot 5 条示例（静态） | ~500 | 每轮推理 | **最大浪费源——除了首轮外完全重复** |
| 搜索深度指令（半静态） | ~100 | 每轮推理 | 按 depth_taste 变化 |
| Scene Hint（半静态） | ~25 | 每轮推理 | 按 intent 变化 |
| 工具定义 JSON Schema（静态） | ~1,400 | 每轮推理（首轮 bind） | 17 个工具，chat intent 浪费 100% |
| Memory Context（动态） | ~320 | 仅首轮 | L2 召回 |
| 对话历史（动态） | 1,000-3,000 | 每轮追加 | 由 L1 滑动窗口管理 |
| **Aggregator 小计（首轮）** | **~3,700** | | |
| **Aggregator 小计（后续轮）** | **~2,400** | | 无 memory context |
| Render Character Card + 规则（静态） | ~350 | 每次请求 | |
| Render 系统数据（动态） | ~400 | 每次请求 | Aggregator 输出文本 |
| **Render 小计** | **~750** | | |

**按场景估算总输入 token：**

| 场景 | 推理轮数 | LLM 调用数 | 估算输入 tokens | DeepSeek 成本 |
|------|---------|-----------|----------------|-------------|
| chat (fast) | 1 | 2 (Agg + Render) | ~4,500 | ~$0.0012 |
| fetch (fast, 1 search + 1 detail) | 2 | 3 | ~8,000 | ~$0.0022 |
| explore (fast, 3 轮) | 3 | 4 | ~12,000 | ~$0.0032 |
| discuss (deep, 6 轮) | 6 | 7 | ~25,000 | ~$0.0068 |
| discuss (deep, 最坏 12 轮) | 12 | 13 | ~50,000 | ~$0.0135 |

> 注：DeepSeek V3 约 $0.27/1M input tokens, $1.10/1M output tokens。当前成本极低。
> 但如果迁移到 Claude Sonnet（$3/1M input），同样最坏场景成本跳到 $0.15/请求——**涨 11 倍**。

**延迟（TTFT + 端到端耗时）：**

| 场景 | 典型耗时 | 瓶颈 |
|------|---------|------|
| chat | ~1.5s | Render LLM 调用 |
| fetch (fast) | ~3-5s | 串行 tool calls（search → detail） |
| discuss (deep) | ~8-15s | 多轮推理 + 大体积工具返回 |

当前没有 streaming 中间结果（reasoning 全 `ainvoke`），用户看到的是空白等待。

### 5.3 业界主流成本控制策略

**策略 1：Prompt Caching（Anthropic 原生，最省力的 90% 折扣）**

Anthropic 的 `cache_control` 允许标记 prompt 中的静态前缀（如 system prompt），后续请求的相同前缀只收缓存价（写入价格的 10%）。DeepSeek 暂无此能力——这是切换到 Claude 的最大动力。

```
System Prompt = [cache_control: 前 3,000 tokens 静态内容] + [动态内容]
首请求: 3,000 × $3/M = $0.009
后续请求: 3,000 × $0.30/M = $0.0009  → 省 90%
```

**策略 2：工具结果压缩（MemGPT 式）**

MemGPT 不把完整 API 响应喂回 LLM，而是提取关键字段后压缩注入。

```
当前: get_bangumi_subject_detail → 完整 infobox (800 chars) → LLM
优化: get_bangumi_subject_detail → extract(score, rank, director, tags) → "EVA: 9.0分 #6, 庵野秀明, 科幻/心理" (120 chars) → LLM
```

省 85% 的工具结果 token。

**策略 3：动态 Few-shot 选择（RAG for examples）**

不固定发送 5 条示例——根据当前 intent 从示例库中选 1-2 条最相关的。

```
当前: 每次 5 条 (~500 tokens) × 6 轮 = 3,000 tokens
优化: 每轮 1-2 条 (~150 tokens) × 6 轮 = 900 tokens  → 省 70%
```

**策略 4：按 Intent 的工具暴露（你已经做了一半！）**

你的 `TOOLS_BY_INTENT` 已经按 intent 过滤工具了——chat intent 绑 0 个工具，fetch 绑 4 个。这是正确的方向。但 schema 中的 tool description 仍在消耗 token。

```
chat:     0 tools, 0 tool tokens (当前已优化 ✅)
fetch:    4 tools, ~350 tool tokens
discuss: 12 tools, ~1,000 tool tokens
```

**策略 5：Token 预算分配到各组件**

为 prompt 的每个 section 设定硬 token 上限，类似 L1 memory 的 `DEPTH_TOKEN_BUDGETS`。

```
few_shot_budget:    200 tokens (从 500 压缩)
tool_result_budget: 500 tokens per iteration (截断超大返回)
memory_budget:      500 tokens (已存在)
```

**策略 6：语义缓存（同类查询复用）**

相同或近似的查询（如多个用户问"EVA 评分"）可以缓存 Aggregator 输出，跳过整个 reasoning 链。

```
用户 A: "EVA 评分如何？" → reasoning → render → 缓存 (query_embedding, result)
用户 B: "EVA 几分？"   → cache hit → 直接 render  → 省 1 次 LLM 调用
```

### 5.4 我们的差距总结

| 维度 | 当前状态 | 业界标准 | 差距 |
|------|---------|---------|------|
| Token 监控 | DEV_MODE 记录但不分析 | 每次请求自动记录到 DB/日志，有 dashboard | 有数据，没有"看" |
| Prompt Caching | 无 | Anthropic cache_control（省 90%） | 受限于 DeepSeek API 能力 |
| 工具结果压缩 | 零——全量返回 | MemGPT 式字段提取（省 85%） | 差很远 |
| Few-shot 管理 | 5 条固定，每轮发送 | 动态选择 1-2 条（省 70%） | 差很远 |
| 组件 Token 预算 | L1 memory 有预算 | 每个 section 都有预算 | 中等 |
| 工具按 Intent 过滤 | 已实现（TOOLS_BY_INTENT）✅ | 标准做法 | 无差距 |
| 语义缓存 | 无 | 同类查询缓存复用 | 需要建设 |
| 延迟优化 | ainvoke 同步等待 | streaming / 并行 tool calls | 中等 |

---

## 六、综合改进路线图（含成本维度）

### Tier 1 — 零风险、立刻见效（纯 prompt 文本改动）

| # | 改进 | 位置 | 成本影响 | 工作量 |
|---|------|------|---------|--------|
| 1 | Aggregator identity 加 negative definition | `_AGGREGATOR_IDENTITY` | 无（+40 chars） | +40 字 |
| 2 | Memory Context 加 `<memory>` XML 包裹 | `build_aggregator_prompt()` | 微量（+20 chars） | 1 行改动 |
| 3 | Aggregator 加 output format 约束 | 新增 `_OUTPUT_FORMAT` | 微量（+60 chars） | +60 字 |
| 4 | **Few-shot 仅首轮发送**（后续轮跳过 Section 1.5） | `build_aggregator_prompt()` + `nodes.py` | **省 ~500 tokens × (N-1) 轮** | 2 行条件判断 |
| 5 | **工具结果压缩**：detail 返回只保留 score/rank/director/tags/summary（截断 >500 chars） | `tools/bgm_tools.py` | **省 ~600 chars/tool_call** | ~10 行 |

### Tier 2 — 需要少量设计决策

| # | 改进 | 位置 | 成本影响 | 工作量 |
|---|------|------|---------|--------|
| 6 | Aggregator prompt 用 XML 标签重构 section 分隔 | `prompt_builder.py` 整体 | 微量 | ~50 行重构 |
| 7 | Few-shot 动态选择：按 intent 选 1-2 条（非全量 5 条） | 新增 few-shot 选择器 | **省 ~350 tokens/轮** | ~30 行 |
| 8 | **Token 预算分配**：prompt 各 section 设定硬上限 | `prompt_builder.py` | **防止超支** | ~20 行 |
| 9 | **DEV_MODE token 分析**：加 intent/depth/user 维度的聚合统计 | `devtools.py` + 日志 | 无直接节省 | ~30 行 |

### Tier 3 — 较大改动，需要基础设施

| # | 改进 | 位置 | 成本影响 | 工作量 |
|---|------|------|---------|--------|
| 10 | Render 层加 few-shot 对话示例 | profiles + render | +200 chars/人格 | 中等 |
| 11 | 语义缓存：同类查询复用 Aggregator 结果 | 新增 cache 层 | **省 1-2 次 LLM 调用** | 较大 |
| 12 | Prompt Caching（切换 Anthropic 时启用） | `llm.py` | **省 90% 静态 token** | 中等 |
| 13 | Memory 作为独立 block（结构化 memory format） | memory + prompt_builder | 中等 | 中等 |

---

## 七、与 persona hardening 的关系

本话题是独立的——它不阻塞当前的 persona hardening（第 3 批参数调整）。

三个维度各自独立，但存在先后依赖：

| 维度 | 内容 | 紧急度 | 建议顺序 |
|------|------|--------|---------|
| **persona hardening** | 参数值调整（snark=0.55、cold kuudere、cute 字数） | 高——用户可见行为 | **先做（第 3 批）** |
| **prompt 质量优化** | 组织结构（XML 分隔、negative definition、output format） | 中——影响模型遵循度 | 第 2 |
| **成本/延迟优化** | 少发 token（few-shot 首轮、工具压缩、缓存） | 中——当前成本低但不可持续 | 第 2 |

建议理由：
1. 参数调整直接改用户看到的东西——最紧急
2. prompt 质量和成本优化如果同时做，出问题时三方归因（参数？结构？压缩？）太难
3. 成本优化在 DeepSeek 定价下不紧急——但如果计划迁移到 Claude，Tier 1 的成本项（few-shot 首轮、工具压缩）应该在迁移前完成

---

## 参考文件

| 文件 | 内容 |
|------|------|
| `agent/orchestrate/prompt_builder.py` | Aggregator prompt 构建（`build_aggregator_prompt()`，全量 section 常量） |
| `agent/persona/render.py` | Render prompt 构建（`build_render_prompt()`） |
| `agent/persona/profiles.py` | Character Cards + `_SNARK_LEVELS` + `_INITIATIVE_LEVELS` + `_SEARCH_DEPTH_INSTRUCTIONS` |
| `agent/orchestrate/nodes.py:270-280` | Aggregator prompt 注入点 |
| `main.py:335-346` | Render prompt 注入点 |
| `agent/orchestrate/strategies.py` | Scene Hints（按 intent） |
| `agent/orchestrate/deep_strategies.py` | Deep Scene Hints |
| `tools/bgm_tools.py` | 17 个 @tool 函数定义（token 消耗大户：~1,400 tokens 的 JSON Schema） |
| `agent/devtools.py` | Token 统计（DEV_MODE 记录但不分析） |
| `agent/llm.py` | LLM 工厂——无 prompt caching 配置 |
| `agent/memory/short_term.py` | L1 滑动窗口（唯一有 token 预算的组件：`DEPTH_TOKEN_BUDGETS`） |
| `docs/design/new-another-review.md` | 合并审查报告 |
| `docs/design/claude-on-bangumi-vision.md` | 产品愿景 |
