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

## 五、改进路线图（按投入产出比排序）

### Tier 1 — 零风险、零参数变更、立刻见效

| # | 改进 | 位置 | 工作量 |
|---|------|------|--------|
| 1 | Aggregator identity 加 negative definition："你不是 Bangumi 看板娘，不陪用户聊天，输出会被 Render 改写" | `prompt_builder.py` `_AGGREGATOR_IDENTITY` | +40 字 |
| 2 | Memory Context 加 `<memory>` XML 包裹 | `prompt_builder.py` `build_aggregator_prompt()` | 1 行改动 |
| 3 | Aggregator 加 output format 约束："每条信息一行，缺失标注'暂无'" | `prompt_builder.py` 新增 `_OUTPUT_FORMAT` | +60 字 |

### Tier 2 — 需要少量设计决策

| # | 改进 | 位置 | 工作量 |
|---|------|------|--------|
| 4 | Aggregator prompt 用 XML 标签重构 section 分隔（内容不变，只改组织方式） | `prompt_builder.py` 整体 | ~50 行重构 |
| 5 | Few-shot 加 1-2 条反例（空结果终止、意图模糊追问） | `prompt_builder.py` `_FEW_SHOT_EXAMPLES` | +2 条示例 |

### Tier 3 — 较大改动，需要讨论设计

| # | 改进 | 位置 | 工作量 |
|---|------|------|--------|
| 6 | Render 层加 few-shot 对话示例（Character Card spec 的 `example_messages`） | `profiles.py` Character Card 扩展 + `render.py` 注入 | ~200 字 × 4 人格 |
| 7 | Memory 作为独立 block 而非追加 section（结构化 memory format） | `memory/long_term.py` + `prompt_builder.py` | 中等 |

---

## 六、与 persona hardening 的关系

本话题是独立的——它不阻塞当前的 persona hardening（第 3 批参数调整）。

- **persona hardening** 改 **参数值**（snark=0.55、cold kuudere redesign、cute 字数等）
- **prompt engineering** 改 **prompt 组织方式**（XML 分隔、negative definition、output format 等）

两个可以并行推进，但建议先完成 persona hardening（参数调整），再做 prompt engineering（结构调整）。理由：
1. 参数调整直接影响用户可见的行为——更紧急
2. prompt 结构调整如果和参数调整同时做，出问题时很难归因

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
| `docs/design/new-another-review.md` | 合并审查报告 |
| `docs/design/claude-on-bangumi-vision.md` | 产品愿景 |
