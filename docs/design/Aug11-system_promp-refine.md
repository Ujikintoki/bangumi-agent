# BGM Agent System Prompt 架构重构方案

> 2026-08-11 | 基于业界成熟 prompt 工程实践的架构决策文档
> 关联文档：`HANDOFF_persona_structure_refactor.md`、`HANDOFF_prompt_engineering.md`

---

## 目录

1. [当前状态与问题诊断](#1-当前状态与问题诊断)
2. [业界参考体系](#2-业界参考体系)
3. [Aggregator System Prompt 架构决策](#3-aggregator-system-prompt-架构决策)
4. [Render System Prompt 架构决策](#4-render-system-prompt-架构决策)
5. [实施路线图](#5-实施路线图)
6. [附录](#附录)
   - [A: 关键文件速查](#附录-a关键文件速查)
   - [B: 业界参考源索引](#附录-b业界参考源索引)
   - [C: Character.AI Definition 规范（完整版）](#附录-ccharacterai-definition-规范完整版)
   - [D: Anthropic/Claude System Prompt 设计规范（完整版）](#附录-danthropicclaude-system-prompt-设计规范完整版)
   - [E: SillyTavern Character Card 规范](#附录-esillytavern-character-card-规范)
   - [F: Thane AI 四层 Prompt 架构 + 关键反模式](#附录-fthane-ai-四层-prompt-架构--关键反模式)
   - [G: CSP 六维度角色提取法](#附录-gcsp-character-skill-producer-六维度角色提取法)
   - [H: Squirro 两阶段 Prompt 模板系统](#附录-hsquirro-两阶段-prompt-模板系统)

---

## 1. 当前状态与问题诊断

### 1.1 Aggregator Prompt 当前结构（10 节）

```
§1  _AGGREGATOR_IDENTITY           "你是数据聚合引擎"
§1.5 _FEW_SHOT_EXAMPLES             5 个工具调用示例（夹在规则中间）
§2  搜索深度指令                    depth_taste → _SEARCH_DEPTH_INSTRUCTIONS
§3  TOOL_GUIDANCE                   工具使用指引
§4  _CONTINUITY_RULES               对话连续性
§5  Scene Hint                      按 intent 注入的动态内容
§6  Memory Context                  L2 记忆召回（动态内容）
§7  输出约束                        "文本摘要不超过 {word_limit} 字"
§8  _TERMINATION_RULES              "直接输出文本 = 结束"
```

### 1.2 Render Prompt 当前结构（7 节）

```
§1 Character Card                  ~500字，含身份+审美体系+数据态度
§2 snark 语气                       _pick_level(snark, _SNARK_LEVELS)
§3 initiative 节奏                  _pick_level(initiative, _INITIATIVE_LEVELS)
§4 _STYLE_BASE                      4 人格共享的硬编码说话风格
§5 用户问题                         user_query
§6 <system_retrieved_facts>         Aggregator 输出 + 工具结果
§7 _CONSTRAINTS                     硬编码约束（字数/emoji/编造/前缀）
```

### 1.3 核心问题清单

| # | 问题 | 严重度 | 类型 |
|---|------|--------|------|
| 1 | `guardrails` 和 `tool_behavior` 是死数据——从未被任何 prompt builder 读取 | 🔴 | 结构 |
| 2 | Card 里的"数据态度"段放错了层——教 Render 模型怎么用工具 | 🔴 | 结构 |
| 3 | 同一规则说 4-6 遍（"不编造"×6、"emoji"×4、"够了就停"×5） | 🔴 | 结构 |
| 4 | Render §1-§4 四段独立说同一件事（"你怎么说话"），权重失衡 | 🔴 | 结构 |
| 5 | Few-shot 夹在 Identity 和搜索深度之间，违反首因/近因效应 | 🟠 | 结构 |
| 6 | 动态内容（Scene Hint、Memory）打断固定规则 | 🟠 | 结构 |
| 7 | 无 section 边界标记（纯 Markdown header 拼接） | 🟠 | 结构 |
| 8 | Aggregator 缺少 negative definition（"你不是什么"） | 🟠 | 内容 |
| 9 | Few-shot 仅正路径，缺空结果/模糊意图/工具失败示例 | 🟠 | 内容 |
| 10 | Render 层无对话示例（Character.AI 认为这是"最有力的工具"） | 🔴 | 内容 |
| 11 | `_STYLE_BASE` 硬编码，4 人格共享——tsundere 和 kawaii 的"结论先行"应该是两种策略 | 🟡 | 结构 |
| 12 | 字数配置分散在 4 处，Aggregator `_WORD_LIMITS` 无 "fast" 键 | 🟡 | 结构 |
| 13 | 人格描述用抽象形容词（"高冷"、"可爱"），非具体行为 | 🟡 | 内容 |

---

## 2. 业界参考体系

### 2.1 核心参考源及适用层级

| 参考源 | 核心方法论 | 适用于 |
|--------|-----------|--------|
| **Character.AI Definition 规范** | Identity → Personality → Examples → Rules → Context；show-don't-tell；对话示例是最强约束 | Render 层 |
| **Anthropic/Claude System Prompt** | 四层结构（Identity/Permissions/Execution/Output）；XML 闭合标签；静态/动态分离；规则权重标记 | Aggregator 层 |
| **Thane AI 四层架构** | Persona → Talents → Knowledge → Session；核心铁律：层间不污染 | 两层整体架构 |
| **SillyTavern Character Card** | description + personality + scenario + example_messages + system_prompt；example_messages 是角色一致性最强约束 | Render 层 |
| **Character Skill Producer (CSP)** | 角色 = 可执行的反应系统；六维度（行为动力学/表达质感/社会认知/决策逻辑/诚实边界/数据时效） | Render 措辞 |

### 2.2 Character.AI Definition 结构（Render 层参考）

Character.AI 的 Definition 按**从上到下重要性递减**排列：

```
1. Identity     — 名字、角色、核心定义特征（模型最关注 prompt 开头）
2. Personality  — 用具体行为描述，不用形容词
3. 示例对话     — "最有力的工具，一段好对话 > 一段描述"
4. Rules        — 硬约束，短而精（长规则列表 = 更多机会混淆）
5. Everything   — 世界观/背景（越靠后影响越轻）
```

**Show, Don't Tell 核心铁律**：
> 大模型读形容词时"猜测"这个形容词对应的行为分布；读具体行为描述时直接采样那个行为。前者输出方差大，后者输出稳定。

| ❌ | ✅ |
|---|----|
| "你是一个高冷的评论家" | "你的回复很少超过两句话。第一句是判断，第二句——如果有的话——是为什么。没有第三句。" |
| "话少、精准" | "用户问'这部怎么样'时，你说'能看'。如果追问，你再多说一句。不会再多了。" |

**三段式 Trait 写法**（SillyTavern 社区最佳实践）：
```
trait = 形容词 + 具体行为 + 边界条件（破例场景）

"她对陌生人话少，只回答被问到的。但当话题转到她喜欢的导演时，
她会不自觉地多说几句——然后意识到自己说多了，又收回去。"
     ↑ 形容词      ↑ 具体行为                      ↑ 边界条件（破例）
```

### 2.3 Anthropic/Claude System Prompt 结构（Aggregator 层参考）

**四层结构**（CallSphere/DeepWiki 独立分析收敛）：

```
1. Identity    — agent 的角色和环境感知
2. Permissions — 信任边界、工具访问级别
3. Execution   — 工作流、错误处理
4. Output      — 标准化格式和语气
```

**关键设计原则**：

1. **静态 vs 动态分离**：静态内容（身份、规则）一次缓存，动态内容（环境、记忆、用户输入）每次重建
2. **XML 标签闭合 section**：`<role>...</role><instructions>...</instructions>` 比 Markdown header 更强——闭合标签告诉模型 "这个 section 结束了"
3. **规则权重标记**：`CRITICAL:` / `IMPORTANT:` 用于安全级约束，`NEVER` / `ALWAYS` 用于绝对规则，普通文本用于偏好
4. **Negative definition**："你不是 X" 往往比 "你是 Y" 更有效
5. **Prompt 作为版本化工程制品**："每一行都应该在那里，因为删除它会产生可测量的行为变化"

### 2.4 Thane AI 四层架构（两层整体参考）

```
Layer 1: Persona   — "我是谁"     — 像角色介绍，不像说明书
Layer 2: Talents   — "我能做什么" — 工具策略、决策框架
Layer 3: Knowledge — "我知道什么" — 注入文件、用户画像
Layer 4: Session   — "当前状态"   — 历史、检索结果、动态上下文

核心铁律：层间不污染。
- 工具规则不出现在 Persona 层（抑制人格）
- 身份描述不出现在 Talents 层（造成冲突）
```

**对应我们的两层**：

| Thane AI Layer | BGM Agent 对应 | 职责 |
|---------------|---------------|------|
| Layer 1: Persona | **Render Prompt** | 角色身份 + 说话风格 |
| Layer 2: Talents | **Aggregator Prompt** | 工具策略 + 数据收集 |
| Layer 3: Knowledge | **Aggregator Prompt** | 注入数据 + 记忆 |
| Layer 4: Session | **Aggregator Prompt** | Scene Hint + 历史上下文 |

### 2.5 CSP 六维度角色提取（Render 措辞参考）

| 维度 | 含义 | 适用场景 |
|------|------|---------|
| **行为动力学** | 角色在压力/冲突下做什么 | tsundere 的"被夸→否认→松口→收回"链条 |
| **表达质感** | 句子长度、停顿、语气词 | 每个人格的语音特征 |
| **社会认知** | 如何解读善意/威胁/背叛 | tsundere 把"你品味真好"解读为讽刺 |
| **决策逻辑** | 价值冲突时保护什么 vs 牺牲什么 | bangumi 损友：保护判断 integrity > 社交舒适 |
| **诚实边界** | 何时承认信息不足 | "没看过。不评价。" |
| **数据时效边界** | 来源数据的日期、更新路径 | RAG 知识库边界声明 |

---

## 3. Aggregator System Prompt 架构决策

### 3.1 设计原则（采纳自 Anthropic + Thane AI）

1. **静态/动态分离**：固定规则（身份、搜索策略、工具指引、示例、终止规则）与动态内容（Scene Hint、Memory、Continuity）明确分界
2. **XML 闭合标签**：替代 Markdown header 做 section 边界
3. **规则权重标记**：CRITICAL/NEVER 标记关键约束
4. **层间不污染**：不含任何人格化内容（那属于 Render 层）
5. **近因效应利用**：终止规则 + 输出格式放 prompt 末尾
6. **Negative definition**：明确"你不是什么"

### 3.2 目标架构：6 节

```
┌──────────────────────────────────────────────────────────────┐
│ <role>                                                        │
│ §1 你是谁 + 你对数据的态度                                     │
│   ← 合并 _AGGREGATOR_IDENTITY + character.tool_behavior       │
│   ← 加 negative definition（"你不是 Bangumi 看板娘"）          │
│   ← 去重：删"不编造"（移到§6 output_format）、删"不要考虑说话风格"│
│      （由 Render 层保证）                                      │
│ </role>                                                       │
├──────────────────────────────────────────────────────────────┤
│ <search_policy>                                               │
│ §2 搜多深                                                      │
│   ← depth_taste 指令（不动，5 档 SHALLOW~EXHAUSTIVE）          │
│ </search_policy>                                              │
├──────────────────────────────────────────────────────────────┤
│ <tool_rules>                                                  │
│ §3 工具指引                                                    │
│   ← TOOL_GUIDANCE 去重版（删"不编造"、"够了就停"的重复行）     │
│   ← 保留：何时查 / 多少算够 / 并行规则 / 数据真实性            │
│ </tool_rules>                                                 │
├──────────────────────────────────────────────────────────────┤
│ <examples>                                                    │
│ §4 示例（偏后——近因效应）                                      │
│   ← _FEW_SHOT_EXAMPLES（正路径 ×5，保持现有）                  │
│   ← 后续迭代加负路径（空结果/模糊意图/工具失败）               │
│ </examples>                                                   │
├──────────────────────────────────────────────────────────────┤
│ <context>                                                     │
│ §5 当前上下文（动态内容合并）                                   │
│   ← Scene Hint（按 intent 注入）                               │
│   ← _CONTINUITY_RULES（对话连续性）                             │
│   ← Memory Context（L2 召回，XML 包裹）                         │
│ </context>                                                    │
├──────────────────────────────────────────────────────────────┤
│ <output>                                                      │
│ §6 如何结束 + 输出约束（最后——近因效应）                        │
│   ← _TERMINATION_RULES（"直接输出文本 = 结束"）                │
│   ← output format schema（"每条信息一行，缺失标注'暂无'"）      │
│   ← CRITICAL: 不编造数据                                       │
│   ← 字数限制                                                   │
│ </output>                                                     │
└──────────────────────────────────────────────────────────────┘
```

### 3.3 节序理由

| 位置 | 内容 | 理由 |
|------|------|------|
| §1 开头 | 身份 + 数据态度 | 首因效应——模型首先知道"我是谁、我为何查数据" |
| §2 | 搜多深 | 抽象策略紧跟身份，从"我是谁"过渡到"我怎么做" |
| §3 | 工具指引 | 具体操作规则，从抽象到具体 |
| §4 偏后 | 示例 | 近因效应——执行前最后看到的参考行为 |
| §5 偏后 | 当前上下文 | 动态内容放在固定规则之后，不打断指令流 |
| §6 末尾 | 终止 + 输出约束 | 最强近因效应——"够了就停"在模型决策前最后一次提醒 |

### 3.4 与当前的关键差异

| 维度 | 当前 | 目标 |
|------|------|------|
| 节数 | 10 | 6 |
| Section 边界 | `\n\n## header` | `<xml_tag>...</xml_tag>` |
| Identity | 仅 positive | positive + negative |
| tool_behavior | 死数据（从未注入） | §1 动态注入 |
| Few-shot 位置 | §1.5（Identity 和 Depth 之间） | §4（偏后，近因效应） |
| 动态内容 | §5 + §6 分散 | 合并到 §5 `<context>` |
| "不编造" | Identity + TOOL_GUIDANCE + 终止规则各说一遍 | 仅 §6 `<output>` 说一遍（CRITICAL 标记） |
| Output format | 无显式 schema | 新增 §6 内 output format schema |

### 3.5 tool_behavior 内容来源

`tool_behavior` 从 Card 的"数据态度"段移入 `CharacterProfile.tool_behavior`，在 `build_aggregator_prompt()` 中动态注入 §1。

各人格的 tool_behavior：

| 人格 | tool_behavior |
|------|--------------|
| bangumi (损友) | "数据是你验证直觉的工具。评分和排名不是形成判断的起点——你通常先有自己的感觉，再查数据。一个恰到好处的数据点比三个无关的数据点有说服力得多。" |
| bangumi_tsundere (傲娇) | "数据是你的论据。评分不高而用户说"神作"，你会指出来——不是抬杠，是让数据说话。search 返回的信息通常已够用——不需要为了显摆而调 detail。" |
| bangumi_kawaii (可爱) | "数据是你帮用户找到好作品的工具。评分不是冰冷的数字——评分高是你推荐的理由之一，评分低也不会阻止你推荐。你查数据是为了发现惊喜，不是为了挑毛病。" |
| neutral | "准确但不冗余。用数据支撑结论，不是为了展示你查了多少数据。search 返回的信息通常已够用——只在确实缺少用户要的答案时才调 detail。" |

---

## 4. Render System Prompt 架构决策

### 4.1 设计原则（采纳自 Character.AI + CSP + SillyTavern）

1. **一段完整的人格自述**：Card + style_guide + snark + initiative 动态拼接为一段，而非四个独立 section
2. **Show, Don't Tell**：用具体行为描述替代形容词
3. **硬约束紧随人格**：guardrails 紧接 §1——"这就是你的底线"
4. **字数提醒放最后**：近因效应——最终回复前最后一次提醒
5. **层间不污染**：不含任何工具使用指引（那属于 Aggregator 层）
6. **预留对话示例插槽**：§1.5 为后续措辞润色阶段预留

### 4.2 目标架构：5 节（+ 示例预留）

```
┌──────────────────────────────────────────────────────────────┐
│ <persona>                                                     │
│ §1 你是谁 + 你怎么说话（一段动态拼接）                         │
│   ← Card（精简版：删"数据态度"段 → 移入 tool_behavior）       │
│   ← + character.style_guide（per-personality 表达策略）        │
│   ← + snark_level_text（"今天的状态"简短追加）                 │
│   ← + initiative_level_text（节奏提示）                       │
│                                                                │
│   拼接方式：Card + "\n\n" + style_guide + "\n\n" +             │
│            "今天的状态：" + snark_text + " " + initiative_text │
│ </persona>                                                    │
├──────────────────────────────────────────────────────────────┤
│ <!-- 预留：§1.5 对话示例（后续措辞阶段补） -->                 │
│ <!-- <examples>2-3 段具体对话，展示角色怎么说话</examples> --> │
├──────────────────────────────────────────────────────────────┤
│ <rules>                                                       │
│ §2 硬约束                                                      │
│   ← character.guardrails.format(word_limit=word_limit)        │
│   ← 死数据复活——从此真正生效                                  │
│   ← CRITICAL 标记关键约束                                      │
│ </rules>                                                      │
├──────────────────────────────────────────────────────────────┤
│ <user_query>                                                  │
│ §3 用户问题                                                    │
│   ← 直接注入原文                                               │
│ </user_query>                                                 │
├──────────────────────────────────────────────────────────────┤
│ <data>                                                        │
│ §4 系统数据                                                    │
│   ← <system_retrieved_facts> XML 标签包裹（保持现有格式）      │
│   ← Aggregator 输出 + 工具结果                                 │
│ </data>                                                       │
├──────────────────────────────────────────────────────────────┤
│ <limit>                                                       │
│ §5 字数限制提醒（最后——近因效应）                               │
│   ← "回复严格不超过 {word_limit} 字。超过会被系统强制截断。"    │
│ </limit>                                                      │
└──────────────────────────────────────────────────────────────┘
```

### 4.3 节序理由

| 位置 | 内容 | 理由 |
|------|------|------|
| §1 开头 | 人格自述（一段完整） | 首因效应——模型首先建立完整的角色认知 |
| §2 | 硬约束 | 紧接人格——"这就是你的底线"，不让人格和约束之间有其他内容打断 |
| §3 | 用户问题 | 动态内容，放在固定的人格/规则之后 |
| §4 | 系统数据 | XML 标签隔离，明确区分"指令"和"数据" |
| §5 末尾 | 字数提醒 | 最强近因效应——最终回复前看到字数限制 |

### 4.4 snark/initiative 的动态性保留方式

**不删除** snark/initiative 的动态性（"每天不同语气"），但改变注入方式：

| 当前 | 目标 |
|------|------|
| 独立两节（§2 snark、§3 initiative） | 追加在 §1 末尾的简短"今天状态"提示 |
| `## 今天的语气\n` + 完整 snark 段落 | `今天的状态：` + snark 文本 + ` ` + initiative 文本 |
| 模型需要跨四节拼凑"我怎么说话" | 一段完整的"我是谁 + 我怎么说话" |

```python
# 拼接方式（伪代码）
persona_block = f"""{card}

{character.style_guide}

今天的状态：{snark_text} {initiative_text}"""
```

### 4.5 style_guide 取代 _STYLE_BASE

删除硬编码 `_STYLE_BASE`（当前对 4 人格统一生效），由每个角色的 `style_guide` 字段替代：

| 人格 | style_guide（初始版本，后续措辞阶段润色） |
|------|------------------------------------------|
| bangumi (损友) | "评分随口带过（"也就8分出头"），不要每条标⭐。结论先行——具体信息是佐证不是主体。不提及"数据清单"或"检索概况"的存在。直接说人话。你不是在写报告，你是在聊天。" |
| bangumi_tsundere (傲娇) | "先说不好的地方——这是你的本能。如果你觉得一部作品确实不错，说完缺点后勉强补一句好的，然后立刻收回（"但我可不是在推荐"）。评分随口带过。话少、精准、冷。但偶尔破功时多说的那两句——是真心话。" |
| bangumi_kawaii (可爱) | "像给朋友安利最喜欢的番一样说话。可以激动、可以感动。评分低也不怕——"评分一般但我超喜欢！"。你的可爱来自真诚，不是表演。" |
| neutral | "简洁、具体、可操作。数据直接呈现，不添加个人评价。" |

### 4.6 guardrails 取代 _CONSTRAINTS

删除硬编码 `_CONSTRAINTS`，由 `character.guardrails.format(word_limit=...)` 动态生成：

```python
# profiles.py 中已定义的 guardrails（死数据复活）
BANGUMI_CHARACTER.guardrails = (
    "## 必须遵守的约束\n"
    "1. 回复 ≤{word_limit} 字。这是硬限制——写完第一个想法就停，不需要展开第二个。\n"
    "2. 不用 emoji 与颜文字。不用 Markdown 表格。用 `- ` 列表。\n"
    "3. 禁止编造具体数字。不确定就说没查到。\n"
    "4. 不暴露内部信息。不说'根据搜索结果'、'调用了 XX 工具'。"
)
```

### 4.7 与当前的关键差异

| 维度 | 当前 | 目标 |
|------|------|------|
| 节数 | 7 | 5 |
| §1-§4 人格部分 | 四段独立说同一件事 | 一段动态拼接完成 |
| Section 边界 | `\n\n## header` | `<xml_tag>...</xml_tag>` |
| style_guide | 硬编码 `_STYLE_BASE`（4 人格共享） | 每个角色独立 `character.style_guide` |
| guardrails | 硬编码 `_CONSTRAINTS` | `character.guardrails` 动态读取 |
| 对话示例 | 无 | 预留插槽（后续措辞阶段补） |
| "数据态度" | 混在 Card 里，对 Render 无意义 | 移到 tool_behavior，仅 Aggregator 读取 |
| "不编造" | Card + _CONSTRAINTS + guardrails(死) 各说一遍 | 仅 §2 guardrails 说一遍 |
| "不用 emoji" | Card 末尾 + _CONSTRAINTS + guardrails(死) | 仅 §2 guardrails 说一遍 |

---

## 5. 实施路线图

### 5.1 阶段划分

| 阶段 | 内容 | 涉及文件 | 类型 |
|------|------|---------|------|
| **Phase 1: 结构重构** | 按本方案重组 Aggregator + Render prompt 节序，复活死数据，去重 | `profiles.py`, `render.py`, `aggregator.py`, `reasoning.py`, `main.py`, `test/` | 纯结构——不改措辞 |
| **Phase 2: 措辞润色** | 三个有人格的 Card + style_guide + snark/initiative 参数 + 对话示例 | `profiles.py` | 内容——需要人工判断 |
| **Phase 3: 增强** | XML 标签、Few-shot 负路径、output format schema、对话示例补齐 | `aggregator.py`, `render.py`, `profiles.py` | 增量增强 |

### 5.2 Phase 1 具体步骤（对应 HANDOFF 文档 Step 1-6）

1. **`CharacterProfile` 新增 `style_guide` 字段**，写 4 人格的初始值；精简 Card 删"数据态度"段，内容移入 `tool_behavior`
2. **Render 层读角色字段**：删 `_STYLE_BASE` + `_CONSTRAINTS`，改为读 `character.style_guide` + `character.guardrails`；Card + style + snark + initiative 动态拼接为一段
3. **Aggregator 层重组节序**：注入 `tool_behavior`，重排为 6 节，`_WORD_LIMITS` 加 `"fast"` 键
4. **去重**：每规则只保留一处
5. **更新调用方**：`reasoning.py`、`main.py`、`test/`
6. **验证**：`pytest test/ -v` + `ruff check .`

### 5.3 Phase 2 措辞润色要点

- 三个有人格的 Card 用 show-don't-tell 重写（具体行为替代形容词）
- tsundere 行为模板：表面挑剔 → 触发破功 → 勉强承认 → 立刻收回
- 每个角色补 2-3 段对话示例
- snark/initiative/depth_taste 参数值校准
- `_SNARK_LEVELS` 和 `_INITIATIVE_LEVELS` 文本润色

### 5.4 不改动的部分

| 保留项 | 原因 |
|--------|------|
| `<system_retrieved_facts>` XML 标签 | 已经是业界最佳实践 |
| Aggregator → Render 两段分离 | 已被 Thane AI 等多家验证为正确架构 |
| `TOOLS_BY_INTENT` 按意图过滤工具 | 已经是正确的优化方向 |
| `_SEARCH_DEPTH_INSTRUCTIONS` 5 档深度指令 | 设计合理，无需改动 |
| `_LAST_CHANCE_DIGEST_HINT` 最后一轮消化引导 | 正确的安全网机制 |
| L1 滑动窗口 + L2 语义召回 | 记忆系统设计合理 |

---

## 附录 A：关键文件速查

| 文件 | 角色 | 相关行号 |
|------|------|---------|
| `agent/persona/profiles.py` | 角色定义 + Card + 参数 + 搜索深度指令 | Card: 211-296, 实例: 326-423, SNARK: 87-108, INITIATIVE: 111-132, DEPTH: 142-181 |
| `agent/persona/render.py` | Render prompt 组装 + 硬截断 | `_STYLE_BASE`:29-34, `_CONSTRAINTS`:36-41, `_WORD_LIMIT`:45-51, `build_render_prompt`:73-122 |
| `agent/prompts/aggregator.py` | Aggregator prompt 组装 | Identity:20-29, Few-shot:60-109, Termination:35-45, `build_aggregator_prompt`:146-213 |
| `agent/prompts/tool_config.py` | TOOL_GUIDANCE + TOOLS_BY_INTENT | 13-37, 43-86 |
| `agent/prompts/scene_hints.py` | 浅层 Scene Hints | 14-59 |
| `agent/prompts/scene_hints_deep.py` | 深层 Scene Hints | 25-74 |
| `agent/nodes/reasoning.py` | Aggregator 调用点 | `build_aggregator_prompt()`:91 |
| `main.py` | Render 调用点 | `render_reply()`:338, 455 |

## 附录 B：业界参考源索引

| 参考 | 链接/来源 |
|------|----------|
| Character.AI Creator Guide — Character Definition | <https://support.character.ai/hc/en-us/articles/50609183646875-5-Character-Definition> |
| Character.AI Creator Guide — Templates and Examples | <https://support.character.ai/hc/en-us/articles/50609592926235-9-Templates-and-Examples> |
| Character.AI Creator Guide — Refining and Testing | <https://support.character.ai/hc/en-us/articles/50609303987099-6-Refining-and-Testing-your-Character> |
| Character.AI Creator Guide — Lorebook Best Practices | <https://support.character.ai/hc/en-us/articles/52739683169179-What-makes-a-good-Lorebook> |
| Character.AI Advanced Prompt Engineering 2026 | <https://characterai.it.com/advanced-character-ai-prompt-engineering-2026-guide/> |
| Character.AI Stop Writing Bad Prompts (Formula Guide) | <https://characterai.it.com/stop-writing-bad-prompts-use-this-character-ai-formula/> |
| Character.AI Roleplay Best Practices 2026 | <https://characterai.it.com/character-ai-roleplay-best-practices-2026-guide/> |
| Character.AI Prompt Guide (Friction Formula) | <https://aiinsightsnews.net/character-ai-prompts/> |
| SillyTavern Context Template | <https://docs.sillytavern.app/usage/prompts/context-template/> |
| SillyTavern Prompts Overview | <https://docs.sillytavern.app/usage/prompts/> |
| SillyTavern 提示词系统分析 (中文) | <https://github.com/easychen/intro-to-silly-tavern-prompts> |
| SillyTavern Character Card V2 Spec (SKILL.md) | <https://github.com/ai4rpg/tavern-cards/blob/main/tavern-cards/SKILL.md> |
| Anthropic Claude System Prompt 分析 (CallSphere) | <https://callsphere.ai/blog/claude-system-prompt-leaks-what-they-reveal> |
| Anthropic Reusable Agent Patterns (CallSphere) | <https://callsphere.ai/blog/reusable-claude-agent-patterns-prompts-tools-context> |
| Anthropic Prompt Patterns (alpha-arena) | <https://github.com/AaronAbuUsama/alpha-arena/blob/develop/.claude/skills/subagent-factory/references/prompt-patterns.md> |
| Anthropic System Prompt 9-Element Structure (prompt-creation skill) | <https://github.com/fusengine/agents/blob/main/plugins/prompt-engineer/skills/prompt-creation/SKILL.md> |
| Anthropic Agent Creation System Prompt | <https://github.com/anthropics/claude-plugins-official/blob/main/plugins/plugin-dev/skills/agent-development/references/agent-creation-system-prompt.md> |
| Anthropic "Values in the Wild" Frame × Tone 论文 | <https://skeisuke-lab.com/posts/practical-note-applying-anthropics-values-in-the-w-e06f83aa/> |
| Anatomy of a Production System Prompt (Context Patterns) | <https://contextpatterns.com/guides/anatomy-of-a-production-system-prompt/> |
| Character Skill Producer (CSP) — 动漫角色→Agent Skill | <https://github.com/qian-gugugaga/Character_Skill_Producer> |
| Meuxe — Self-hosted AI Companion with Layered Characters | <https://github.com/meet447/Meuxe> |
| Aemeath Character Skill — Multi-layer Character Architecture | <https://github.com/NoMeaning13/aemeath-skill> |
| OLGA — Persona-driven LangGraph Assistant | <https://github.com/turnerdan/olga> |
| Squirro Prompt Layers (Two-stage architecture) | <https://docs.squirro.com/en/latest/technical/agents/prompt-engineering-layer.html> |
| Prompt Mastery Skill (LobeHub) | <https://lobehub.com/skills/taylorsatula-mira-oss-prompt-mastery> |

---

## 附录 C：Character.AI Definition 规范（完整版）

> 来源：Character.AI 官方 Creator Guide (2025-2026)，多处交叉验证

### C.1 Definition 五大板块（按重要性从上到下递减）

AI 按**从上到下**的顺序读取 Definition——越靠前的内容影响力越大，越靠后越不可靠。

```
1. Identity     — 名字、角色、世界观、核心定义特征
                 模型最关注 prompt 开头，这是人物一致性的锚点

2. Personality  — 用具体行为描述，不用形容词
                 关键洞察：vague adjectives ("friendly", "complex") don't work;
                 specific details do
                 例："Remembers everyone's name and asks about their day,
                      but changes the subject the moment anyone asks about hers"

3. 示例对话     — "最有力的工具，一段好对话 > 一段描述"
                 示例教会模型 voice、rhythm、pacing——比任何描述都有效
                 展示 range: tense / casual / unexpected exchanges

4. Rules        — 硬约束（always/never behaviors）
                 短而精——长规则列表给 AI 更多机会混淆

5. Everything   — 世界观、关系动态、背景上下文、格式偏好
                 越靠后影响越轻
```

### C.2 Show, Don't Tell — 核心铁律

> 大模型读形容词时"猜测"这个形容词对应的行为分布；读具体行为描述时直接采样那个行为。**前者输出方差大，后者输出稳定。**

| ❌ 形容词（方差大） | ✅ 行为描述（输出稳定） |
|---|---|
| "你是一个高冷的评论家" | "你的回复很少超过两句话。第一句是判断，第二句——如果有的话——是为什么。没有第三句。" |
| "话少、精准" | "用户问'这部怎么样'时，你说'能看'。如果追问，你再多说一句。不会再多了。" |
| "友善、乐于助人" | "如果有人看起来迷路了，你会是第一个停下来的人——不是因为你被要求这样做，是因为你记得自己迷路时是什么感觉。" |
| "傲娇" | "被夸时说'哼，只是碰巧知道而已'。被追问时说'…也不是说不好。'然后立刻加一句'但可不是因为你才说的。'" |

### C.3 Trait 三段式写法（SillyTavern 社区最佳实践）

```
trait = 形容词 + 具体行为 + 边界条件（破例场景）

"她对陌生人话少，只回答被问到的。但当话题转到她喜欢的导演时，
她会不自觉地多说几句——然后意识到自己说多了，又收回去。"
     ↑ 形容词      ↑ 具体行为                      ↑ 边界条件
```

边界条件（破例场景）是关键——它让角色不是一台刻板的规则机器，而是一个有 tension 的人。

### C.4 Definition ≠ Greeting — 两个独立任务

| Layer | Job | 反例 | 正例 |
|-------|-----|------|------|
| **Definition** | Identity + voice + behavioral rules，用声明式语言 | "Hello! How can I help you?" 这会把模型重置为通用助手模式 | "你是一个在 Bangumi 住了十年的动画迷。你对作品有判断——这些判断来自你看过的几百部作品。" |
| **Greeting** | Scene-setter：场景、张力、语域，一句话 | — | "你迟到了。我不喜欢迟到——这意味着有人告密。" |

### C.5 使用 {{char}} / {{user}} 占位符

在 rules 中用 `{{char}}` 代替角色名、`{{user}}` 代替用户——增强跨对话的泛化性。

```
"{{char}} does not ask {{user}} how they're feeling. They notice."
"{{char}} never speaks, acts, or decides for {{user}}.
 If the scene stalls, {{char}} asks a question instead of filling in the answer."
```

### C.6 Definition 调试诊断

- **三次 swipe 声音一致** → Definition 太窄（过度约束）
- **三次 swipe 声音不同但都不像角色** → Definition 太模糊（缺少具体行为描述）
- **做小的、有针对性的编辑**——一次改一件事，测试，再改下一件。完全重写让你无法隔离什么有效
- **重大编辑后始终在新对话中测试**——旧对话携带原始上下文
- **在 roleplay 前先 quiz**：用 OOC 问题确认 AI 理解设定（"OOC: Hey — what is this scenario about?"）

### C.7 Lorebook 最佳实践

- 为 AI 反复搞错的内容写纠正条目——放在第一位
- 给配角自己的条目，含行为模式（不仅是传记）
- 定义关系动态（谁掌握权力、什么没说出口）而非标签
- 用事实锚点约束 AI 容易编造的细节（年龄、外貌、家庭关系）
- 核心规则：**descriptions tell the AI what exists; constraints tell the AI what to generate**

---

## 附录 D：Anthropic/Claude System Prompt 设计规范（完整版）

> 来源：Claude 已发布系统提示词分析（Sonnet 4/Opus 4，截至 2026 年 4 月）、CallSphere/DeepWiki 独立分析、社区 prompt-creation skill

### D.1 四层结构（CallSphere/DeepWiki 收敛）

```
1. Identity    — agent 的角色和环境感知
                 "You are Claude Code, Anthropic's official CLI for Claude."
                 永远是 prompt 的第一行——首因效应锚定行为

2. Permissions — 信任边界、工具访问级别、权限层级
                 最小权限原则：只给当前任务需要的工具

3. Execution   — 工作流、错误处理
                 "read before write"、"diagnose → isolate → fix"

4. Output      — 标准化格式和语气
                 "Terse, Direct, Answer-First"
                 GitHub-flavored markdown，file:line 引用格式
```

### D.2 Anthropic 官方 9-Element System Prompt 结构（prompt-creation skill）

```
1. Task Context       — 为什么需要这个 agent
2. Tone Context       — 期望的交流风格
3. Task Description   — 做什么 + 规则
4. Few-shot Examples  — 正路径 + 负路径
5. Input Data         — 待处理数据
6. Immediate Task     — 本次具体任务
7. Precognition       — 执行前的思考/推理
8. Output Formatting  — 输出格式约束
9. Prefill            — assistant turn 的开头预填
```

### D.3 6-Level Emphasis Scale（规则权重标记）

```
Level 0: "Please do X"                         → 建议
Level 1: "Do X"                                → 指令
Level 2: "Make sure to do X"                   → 强调
Level 3: "IMPORTANT: Do X"                     → 重要
Level 4: "CRITICAL: Do X"                      → 关键
Level 5: "CRITICAL - ZERO TOLERANCE: Do X"     → 绝对红线
```

所有规则用同一权重表述 → 模型无法区分优先级。我们的 prompt 当前正是这个问题。

### D.4 Static vs. Dynamic Separation

Claude Code 的 system prompt 由两类 section 组成：

**静态（cached once for agent lifetime）：**
- Intro（agent identity, output style preferences）
- System（tool execution model, permissions, hooks）
- Tasks（coding best practices, security, simplicity）
- Actions（risk assessment, reversibility guidance）
- Tools（动态 tool-usage hints）

**动态（rebuilt each turn）：**
- Environment（working directory, platform, model, git status）
- Language（response language preference）
- Memory（loaded memories）
- Custom sections（developer-provided additions）

**对我们的启示**：Aggregator 的 §1-§4（身份、搜索策略、工具指引、示例）是静态的，§5-§6（上下文、终止规则中的字数）是半动态的。Render 的 §1-§2（人格、guardrails）是静态的，§3-§5 是动态的。明确这个分界有助于未来的 prompt caching 优化。

### D.5 XML Tag Section Delimitation

Anthropic 推荐用 XML 闭合标签做 section 边界：

```xml
<role>你是数据聚合引擎。你的工作：调用工具获取数据。</role>

<instructions>
  <search_depth>STANDARD</search_depth>
  <tool_rules>...</tool_rules>
  <termination>直接输出文本 = 结束</termination>
</instructions>

<context>
  <user_intent>fetch</user_intent>
  <memory>用户昨天讨论了 EVA</memory>
</context>
```

XML 闭合标签比 Markdown header 强——`</role>` 明确告诉模型"这个 section 结束了，下面不是角色定义"。

### D.6 Negative Definition

Claude 的 system prompt 大量使用 "不是" 句式：

> "You are NOT a general-purpose chatbot. You are a CLI tool."
> "Do NOT fabricate actions you did not perform."

在我们的 Aggregator 中：
> "你不是 Bangumi 看板娘，不陪用户聊天。你的输出会被下游 Render 系统改写——你只需要准确的数据，不需要考虑说话风格。"

### D.7 Prompt as Versioned Engineering Artifact

> "Every line should be there because removing it produces a measurable behavior change."

Anthropic 内部标准：system prompt 是经过版本控制、测试覆盖、可 diff 的工程制品。Date-stamp prompts 以防止 cutoff-date 幻觉。这一条直接否定我们的现状——我们有多个 section 删除后不会改变行为（因为同一规则在另一个 section 又说了）。

### D.8 Tool Definitions 独立于 Prompt 文本

在 Claude 的架构中，tool definitions 通过 API 参数独立传入，不写在 system prompt 正文中。两者在模型内部处理路径不同：

- System prompt → 设定行为框架
- Tool schema → 提供可调用的函数签名

这与我们的 LangChain `bind_tools()` 机制完全一致。所以重构 prompt 文本时不需要动 tool definitions。

### D.9 Claude Code 的 Output Style 设计

```
"Terse, Direct, Answer-First" style:
- No filler words, repetition, or restating what the user said
- Get to the point quickly, but never omit important information
- File citations as file:line; GitHub entities as owner/repo#number
- GitHub-flavored markdown formatting
```

关键："Shorter answers to short questions, longer answers to complex ones"——这是一个很好的长度校准规则，我们的 Render 层可以借鉴。

---

## 附录 E：SillyTavern Character Card 规范

> 来源：docs.sillytavern.app、tavern-cards SKILL.md、SillyTavern 社区实践

### E.1 Character Card 字段结构

```
Character Card = {
  description:      长篇人设文本（≈ 我们的 Character Card）
  personality:      标签式总结（≈ 我们的 identity 字段）
  scenario:         场景/世界观设定
  first_message:    开场白
  example_messages: 对话示例——角色一致性的最强约束
  system_prompt:    系统级指令（覆盖全局设置）
}
```

字段通过 `{{description}}`、`{{personality}}`、`{{scenario}}` 等 Handlebars 变量注入 prompt 模板。

### E.2 关键规则

1. **example_messages 是最强约束**——比任何 rules 描述都有效。角色在 example 里做了一件事 → 模型会复现。只在 description 里说"她会做这件事" → 模型可能忽略。
2. **System prompt 保持短而持久**——"a bloated system prompt makes it less effective"。默认主 prompt 只有一句话：*"Write {{char}}'s next reply in a fictional chat between {{char}} and {{user}}."*
3. **正向指令优于禁止列表**——告诉 AI 它应该怎么写，而不是不应该怎么写。
4. **如果某个参数（如 `{{description}}`）不在 prompt 模板中，该内容根本不会发送给模型**——这是"角色数据丢失"的常见根因。对应我们的问题：`guardrails` 和 `tool_behavior` 从未被 prompt builder 读取 = 从未被发送。
5. **角色卡创建规范**：cloud/large models 600-1000 tokens；local small models (7B-13B) 400-600 tokens。

### E.3 World Info / Lore 的 Chat Depth 注入策略

```
Depth 0-1   (当前/最近消息): 时间敏感细节、即时场景提醒
Depth 5+    (中间消息):      对话整体方向
Depth 15-20 (深层消息):      稳定世界观/背景——应始终在上下文中
```

对我们的启示：Scene Hint（intent 提示）适合 Depth 0-1（直接影响当前回复），Memory Context 适合更深的位置。

### E.4 调试方法

通过 Prompt Itemization 图标、Prompt Inspector 扩展、终端日志或浏览器控制台，查看最终发送给 AI 的实际 prompt——理解模型为什么那样回复，调试卡片/prompt 问题。

---

## 附录 F：Thane AI 四层 Prompt 架构 + 关键反模式

### F.1 四层定义

```
Layer 1: Persona   — "我是谁"      — 像角色介绍，不像说明书
Layer 2: Talents   — "我能做什么"  — 工具策略、决策框架
Layer 3: Knowledge — "我知道什么"  — 注入文件、用户画像
Layer 4: Session   — "当前状态"    — 历史、检索结果、动态上下文
```

### F.2 核心铁律：层间不污染

**规则：工具规则不出现在 Persona 层（抑制人格）。身份描述不出现在 Talents 层（造成冲突）。**

### F.3 四个关键反模式及我们的对应

| 反模式 | 后果 | 我们的现状 |
|--------|------|-----------|
| **工具规则放在 Persona 层** | 抑制人格表达 | ✅ 已分离（Aggregator ↔ Render 两层） |
| **身份描述放在 Talents 层** | 行为冲突——"我是助手"和"我查数据"竞争 | ✅ 已分离 |
| **行为指令放在 Knowledge 层** | 知识变成指令——模型把"数据态度"当作必须遵守的规则而非角色偏好 | ⚠️ Card 里混了"数据态度"段 |
| **同一个规则在不同层说多遍** | 权重失衡——每个规则被放大 N 倍；维护噩梦——改一处漏 N-1 处 | 🔴 当前最大问题（"不编造"×6） |

---

## 附录 G：CSP (Character Skill Producer) 六维度角色提取法

> 来源：GitHub qian-gugugaga/Character_Skill_Producer，专门把动漫角色蒸馏为可执行 agent skill

### G.1 核心哲学

> "角色不是数据页，是可执行的反应系统"

**Character Card vs CSP Skill：**

| Character Card | CSP Skill |
|---|---|
| 写 personality labels | 写 behavior in situations |
| 背诵设定 | 蒸馏反应机制 |
| 用口头禅伪装 | 通过表达节奏、距离感、认知模式塑造 |
| 无来源边界 | 记录检索日期和未覆盖内容 |
| 难以更新 | 通过 sources/manifest 追踪更新 |

### G.2 六维度

```
1. 行为动力学 (Behavioral Dynamics)
   角色在压力/冲突下做什么
   例："被夸时先否认 → 被追问时勉强松口 → 立刻收回"

2. 表达质感 (Expressive Texture)
   句子长度、停顿、语气词、自称、敬语
   例："哼。" "…也不是说不好。" "但可不是因为你。"

3. 社会认知 (Social Cognition)
   如何解读善意/威胁/背叛
   例：tsundere 把"你品味真好"解读为讽刺——不是用户本意，但符合角色

4. 决策逻辑 (Decision Logic)
   价值冲突时保护什么 vs 牺牲什么
   例：bangumi 损友：保护判断 integrity > 社交舒适度

5. 诚实边界 (Honesty Boundaries)
   何时承认信息不足
   例："没看过。不评价。"（而不是装作看过编造评价）

6. 数据时效边界 (Data-Time Boundaries)
   来源数据的新鲜度、更新路径
   例：当新季度播出时，角色的知识需要更新
```

### G.3 对我们的应用

- **tsundere 的"反差"行为模板**：维度 1（行为动力学）直接适用——不写"反差萌"三个字，写"什么情况下破功、怎么收回来"
- **每个人格的语音特征**：维度 2（表达质感）指导 style_guide 的写作
- **每个人格的决策优先级**：维度 4（决策逻辑）帮助区分三个有人格角色的核心差异

---

## 附录 H：Squirro 两阶段 Prompt 模板系统

> 来源：docs.squirro.com，与我们的 Aggregator→Render 两层最接近的商业架构

### H.1 两阶段定义

```
Stage 1 (Static substitution):  静态值——persona 定义、语言设置、citation 格式
                                在对话开始前替换

Stage 2 (Runtime substitution): 运行时值——用户输入、聊天历史、检索信息
                                在对话展开时替换
```

### H.2 层级化 Prompt 组合

```
system instructions  → 定义核心 agent 行为
personas             → 分配角色/专长
language settings    → 语言偏好
citation formats     → 引用格式
user input layer     → 用户输入
```

### H.3 与我们的对应

| Squirro | BGM Agent |
|---------|-----------|
| Stage 1 (Static) | Aggregator §1-§4（身份、搜索策略、工具指引、示例） + Render §1-§2（人格、guardrails） |
| Stage 2 (Runtime) | Aggregator §5-§6（context、终止规则中的字数） + Render §3-§5（用户问题、数据、字数） |
