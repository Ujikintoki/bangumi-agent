# HANDOFF — 人格系统结构重构 + 内容润色

> 2026-08-11 | 从 `HANDOFF_persona_hardening.md` 第 3 批深入后发现的结构问题
> **上一份交接文档**：`HANDOFF_persona_hardening.md`（第 1、2 批已完成，第 3 批卡住）

---

## 1. 核心任务目标

修复人格系统的 **两个层面**：

| 层面 | 问题 | 状态 |
|------|------|------|
| **结构层** | prompt 装配管线的组织问题：内容放错层、同一规则说 N 遍、字段从未被读取、节数过多说同一件事 | 🔴 本会话新发现 |
| **内容层** | 4 个 Character Card 的措辞 + snark/initiative/depth_taste 参数值 + scene hints | 🟡 原计划，待结构理顺后再改 |

**依赖关系**：先修结构 → 再写内容。否则你精心写的措辞可能根本没被装配进 prompt。

**新人格 lineup**（用户最新决定）：

| key | 原型 | 说明 |
|-----|------|------|
| `bangumi` | 损友 | 有品位的同好，敢说真话（当前 default，保留） |
| `bangumi_kawaii` | 可爱的分享者 | 原名 `bangumi_cute`，真诚安利，发现闪光点 |
| `bangumi_tsundere` | 傲娇评论家 | **取代** `bangumi_cold`——嘴硬心软，有明确行为模板 |
| `neutral` | 开发者工具 | 不改 |

---

## 2. 已完成的修改

### 第 1 批（main.py P0 修复）— commit `2b3996e`
- B-2~B-4: session_id / CancelledError / stream 降级 null
- B-1: DEV_MODE 测试隔离
- ERROR_LEAK / CORS / GraphRecursionError

### 第 2 批（Prompt + Docstring 修复）— commit `547a2d0`
- A1: TOOL_GUIDANCE 删"search 已够用"
- B1/B2: Few-shot 字段和参数修正
- A3/A4/A6/A7: 分类器、docstring、word limits
- R5/R7/R8: 死代码删除（_VOICE, _style_modifiers, get_render_tone_variables）
- R9: hard cutoff 无句号 fallback
- DOC_DRIFT: 全部 8 处 docstring 修正

### 第 3 批 prep（死代码链 + depth_taste 修复）— commit `70d5536`
- DEAD_CHAIN_4: 删除 `_DEPTH_LEVELS` → `_render_tone()` → `prompt_builder.build_system_prompt()` → `deep_strategies.build_system_prompt()`（~380 行，5 个文件）
- DEPTH_TASTE_HARDCODE: `nodes.py` 硬编码 0.90 → `character.depth_taste`
- `test_prompts.py`: 废弃函数测试清理 + 预存 intent key bugfix

### 本会话新增（尚未 commit）
- `docs/design/new-another-review.md`: 同步 Phase 3 prep 完成状态
- `docs/tmp/files_for_handoff/HANDOFF_prompt_engineering.md`: prompt 工程设计讨论 + 成本/延迟分析

---

## 3. 当前 Prompt 装配管线（问题总览）

### 3.1 Aggregator Prompt 当前结构（10 节）及问题

```
§1 _AGGREGATOR_IDENTITY           "你是数据聚合引擎"
§1.5 _FEW_SHOT_EXAMPLES           5个工具调用示例
§2 搜索深度指令                   depth_taste → _SEARCH_DEPTH_INSTRUCTIONS
§3 TOOL_GUIDANCE                  来自 tool_config.py
§4 _CONTINUITY_RULES              对话连续性
§5 Scene Hint                     scene_hints.py（按 intent 选）
§6 Memory Context                 L2 记忆召回
§7 输出约束                       "文本摘要不超过 {word_limit} 字"
§8 _TERMINATION_RULES             "直接输出文本 = 结束"
```

**问题**：

| 问题 | 说明 |
|------|------|
| **§1 + §3 + §8 说同一件事** | "我是数据引擎"（§1）、"怎么用工具"（§3）、"如何结束"（§8）分开在三节，模型需要自己跨 8 节拼凑完整的"查数据→够了就停→输出文本"行为模型 |
| **Few-shot 夹在规则中间** | §1.5 的 5 个示例夹在身份定义（§1）和搜索深度（§2）之间。业界(Character.AI)建议示例放 prompt 前部或后部（首因/近因效应），不放中间 |
| **动态内容夹在固定规则中间** | §5 Scene Hint 和 §6 Memory Context 是每次请求变化的动态内容，夹在固定规则中间 |
| **去重前：同一规则说多次** | "不编造"在 §1、§3、§8 各说一遍；"够了就停"在 §3、§8 各说一遍 |

### 3.2 Render Prompt 当前结构（7 节）及问题

```
§1 Character Card                ~500字，含身份+审美体系+数据态度
§2 snark 语气                     _pick_level(snark, _SNARK_LEVELS)
§3 initiative 节奏                _pick_level(initiative, _INITIATIVE_LEVELS)
§4 _STYLE_BASE                    4人格共享的说话风格
§5 用户问题                       user_query
§6 <system_retrieved_facts>       Aggregator 输出 + 工具结果
§7 _CONSTRAINTS                   硬约束（字数/emoji/编造/前缀）
```

**核心问题：§1-§4 在说同一件事——"你怎么说话"**

```
§1 "你是 Bangumi 看板娘，一个有品位的 ACGN 爱好者。你有自己的审美体系..."
§2 "今天你状态正常。有褒有贬，但不为 diss 而 diss..."
§3 "今天节奏正常。有话说就说，没话说就停..."
§4 "评分随口带过。结论先行。不提及数据清单的存在..."
```

这四节全在告诉模型"你怎么说话"。四层指令叠加造成两个后果：

1. **权重失衡**：身份（§1）约 500 字，语气（§2）约 80 字，风格（§4）约 60 字——模型对三个权重不同的指令做加权平均，产出不确定
2. **可能冲突**：Card 说"有自己的立场"，snark L4 说"该被 diss"，initiative L2 说"说完就停"——三个指令指向的行为不一致

**类比**：你不会这样教一个人聊天——"1. 你是一个损友。2. 今天你的毒舌度是 0.65。3. 今天你的主动性是 0.60。4. 说话要结论先行。"你会直接说：**"你是一个看了很多动画、有话直说的损友。你觉得好的就说好，觉得烂的就说烂——不是为了 diss，是因为你真的在乎这个媒介。你说话不绕弯子，一两句说清楚就停了。"** 这一句话同时覆盖了身份、语气、节奏、风格。

**`_SNARK_LEVELS` 的实际内容不是"今天的情绪"，是人格层面的行为指令**：

```
L3: "今天你状态正常。有褒有贬，但不为 diss 而 diss。
     你觉得值得说的就说，不值得的懒得提。"
```

这是在说"你是一个什么样的人"，不是"今天心情怎么样"。这些内容理应合并到 Card 的人格描述中。

---

## 4. 5 个具体的结构缺陷

### 🔴 问题 1：guardrails 和 tool_behavior 是死数据——从未被装配进任何 prompt

| 字段 | 定义位置 | 内容示例 | 谁在读它？ |
|------|---------|---------|-----------|
| `guardrails` | `profiles.py:335-341` | "回复 ≤{word_limit} 字"、"不用 emoji"、"禁止编造" | **无人**——Render 用硬编码 `_CONSTRAINTS` |
| `tool_behavior` | `profiles.py:342-346` | "查数据是为了形成判断，不是为了报数据" | **无人**——Aggregator 用硬编码 `TOOL_GUIDANCE` |

### 🔴 问题 2：Character Card 里的"数据态度"段放错了层

`profiles.py:231-235`（三个 Card 都有同类内容）：
> "评分和排名是你验证直觉的工具，不是形成判断的起点..."

这是教模型**怎么用工具**——但 Render 模型不调工具。应该移到 `tool_behavior`，由 Aggregator 读取。

### 🟠 问题 3：同一规则说了 4-6 遍

| 规则 | 次数 | 位置 |
|------|------|------|
| "不要编造数据" | **6** | `_AGGREGATOR_IDENTITY` + `_TERMINATION_RULES` + `TOOL_GUIDANCE` + `guardrails`(死) + `_CONSTRAINTS` + Card 诚实声明 |
| "不用 emoji" | **4** | `_CONSTRAINTS` + `guardrails`(死) + Card × 2 |
| "够了就停" | **5** | `TOOL_GUIDANCE` + `_TERMINATION_RULES` + `tool_behavior`(死) + `motivation` + `tool_strategy` |

### 🟠 问题 4：字数限制在 4 个地方定义了 4 种不同值

| 位置 | fast | deep |
|------|------|------|
| `render.py:_WORD_LIMIT` | 200 | 350 |
| `render.py:_HARD_CUTOFF_MAX_CHARS` | 280 char | 480 char |
| `aggregator.py:_WORD_LIMITS` | **❌ 无"fast"键**（fallback "auto":200） | 350 |
| `profiles.py` guardrails | `{word_limit}` 占位符（死数据） | — |

### 🟡 问题 5：`_STYLE_BASE` 对 4 个人格统一生效

`render.py:29-34` —— tsundere 的"结论先行"和 kawaii 的"结论先行"应该是两种表达策略。当前 snark/initiative 只能微调语气，调不了表达策略。

---

## 5. 目标架构：改完之后每个 Prompt 长什么样

### 5.1 核心设计决策：方案 B

snark 和 initiative 的动态性（"每天不同语气"）保留，但不再作为独立节注入。改为：**Card + snark + initiative + style 动态拼接成一段完整的"我是谁+我怎么说话"**。

```
原结构（4 段说一件事）:
  §1 Character Card  → 你是谁
  §2 snark 语气      → 你今天怎么说话
  §3 initiative 节奏 → 你今天怎么说话
  §4 style 风格      → 你怎么说话

新结构（1 段说清）:
  §1 你是谁 + 你怎么说话 → Card + style_guide + 今天的语气 + 今天的节奏，一段说完
```

### 5.2 Aggregator Prompt（6 节，从 10 节精简）

```
§1 你是谁 + 你怎么对待数据        ← 合并 _AGGREGATOR_IDENTITY + character.tool_behavior
§2 搜多深                         ← depth_taste 指令（不动）
§3 工具指引                       ← TOOL_GUIDANCE（去重精简）
§4 示例                           ← _FEW_SHOT_EXAMPLES（移到偏后，近因效应）
§5 当前上下文                     ← Scene Hint + Continuity + Memory，动态内容合并
§6 如何结束 + 输出字数             ← 终止规则 + 字数限制，放最后（近因效应）
```

**为什么这样排**：
- §1-§3 是固定规则（身份 → 策略 → 工具），从抽象到具体
- §4 示例放偏后——模型执行前最后看到的参考行为
- §5 动态上下文集中处理，不在固定规则中打断
- §6 终止规则放最后——近因效应使模型不会忘记"输出文本即结束"

### 5.3 Render Prompt（5 节，从 7 节精简）

```
§1 你是谁 + 你怎么说话             ← Card + style_guide + snark_level + initiative_level 动态拼接为一段
§2 必须遵守                       ← character.guardrails.format(word_limit=...)，硬约束
§3 用户问题                       ← user_query
§4 系统数据                       ← <system_retrieved_facts>
§5 字数限制提醒                   ← "{word_limit} 字以内"，放最后利用近因效应
```

**为什么这样排**：
- §1 一整段完成所有"人格+语气+风格"——模型读的是一个完整的人，不是四层独立指令
- §2 硬约束紧接人格——"这就是你的底线"
- §5 字数放最后——最终回复前最后一次提醒

### 5.4 §1 的动态拼接方式

```python
def build_persona_block(character, snark_level_text, initiative_level_text):
    """Card + 风格 + 当天语气 -> 一段完整的自述"""
    card = get_character_card(character.key)
    style = character.style_guide

    return f"""{card}

{style}

今天的状态：{snark_level_text} {initiative_level_text}"""
```

这不是"删掉 snark/initiative 的动态性"——是改变它们的**注入方式**：从独立节变成追加在 Card 后面的"今天状态"提示。

### 5.5 节数对比

| Prompt | 当前节数 | 目标节数 | 减少 |
|--------|---------|---------|------|
| Aggregator | 10 | 6 | -4 |
| Render | 7 | 5 | -2 |

---

## 6. 业界最佳实践参考（本会话研究结论）

### 6.1 核心铁律：Show, Don't Tell

> 大模型读形容词时"猜测"这个形容词对应的行为分布；读具体行为描述时直接采样那个行为。前者输出方差大，后者输出稳定。

| ❌ | ✅ |
|---|----|
| "你是一个高冷的评论家" | "你的回复很少超过两句话。第一句是判断，第二句——如果有的话——是为什么。没有第三句。" |
| "话少、精准" | "用户问'这部怎么样'时，你说'能看'。如果追问，你再多说一句。不会再多了。" |

来源：Character.AI 官方 Creator Guide、SillyTavern 社区规范

### 6.2 Character.AI Definition 结构（按重要性排序）

```
1. Identity     — 核心定义特征（模型最关注 prompt 开头）
2. Personality  — 用具体行为描述，不用形容词
3. 示例对话     — "最有力的工具，一段好对话 > 一段描述"
4. Rules        — 硬约束，短而精（长规则列表 = 更多机会混淆）
5. Everything   — 世界观/背景（越靠后影响越轻）
```

### 6.3 SillyTavern 补充：trait = 形容词 + 具体行为 + 边界条件

```
"她对陌生人话少，只回答被问到的。但当话题转到她喜欢的导演时，
她会不自觉地多说明句——然后意识到自己说多了，又收回去。"
     ↑ 形容词      ↑ 具体行为                      ↑ 边界条件（破例）
```

**这正是 tsundere "反差"的写法**——不写"反差萌"三个字，写**什么情况下破功、怎么收回来**。

### 6.4 Thane AI 四层 Prompt 架构（2025 业界主流）

```
Layer 1: Persona   — "我是谁"      — 像角色介绍，不像说明书
Layer 2: Talents   — "我能做什么"  — 工具策略、决策框架
Layer 3: Knowledge — "我知道什么"  — 注入文件、用户画像
Layer 4: Session   — "当前状态"    — 历史、检索结果、动态上下文

规则：层间不污染。工具规则不出现在 Persona 层（抑制人格）。
      身份描述不出现在 Talents 层（造成冲突）。
```

### 6.5 关键反模式（Thane AI 文档）

| 反模式 | 后果 | 你的现状 |
|--------|------|---------|
| 工具规则放在 Persona 层 | 抑制人格表达 | ✅ 你已分离 |
| 身份描述放在 Talents 层 | 行为冲突 | ✅ 你已分离 |
| 行为指令放在 Knowledge 层 | 知识变成指令 | ⚠️ Card 里混了"数据态度" |
| 同一个规则在不同层说多遍 | 权重失衡、维护噩梦 | 🔴 当前最大问题 |

### 6.6 Tsundere 在 ACGN 社区的行为模板

| 层 | 行为 |
|----|------|
| 表面 | 挑剔、嘴硬——先说不好的地方 |
| 触发 | 不是用户说够了好话，是话题碰到她真正在乎的东西 |
| 破功 | "……不过也不是完全没有可取之处"——勉强承认 |
| 收回 | 承认后立刻加一句"但我可不是因为你才说的" |
| 频率 | 每次对话 1-2 次，多了就不珍贵 |

与你之前的 cold (kuudere) 的关键区别：tsundere **会表达负面判断**但随后松口，kuudere **不表达判断**直到真正在意时多说两句。tsundere 的语言更**戏剧化**，辨识度更高。

---

## 7. 具体改动步骤（6 步，按顺序执行）

### Step 1: CharacterProfile 新增 `style_guide` 字段，补全数据

**文件**: `agent/persona/profiles.py`

1. `CharacterProfile` dataclass 新增字段：
```python
style_guide: str = ""  # per-personality 表达风格规则
```

2. 四个角色实例各写 `style_guide`（替代硬编码 `_STYLE_BASE`）：
```python
# bangumi (损友)
style_guide=(
    "## 表达风格\n"
    "评分随口带过（"也就8分出头"），不要每条标⭐。"
    "结论先行——具体信息是佐证不是主体。"
    "不提及"数据清单"或"检索概况"的存在。"
    "直接说人话。你不是在写报告，你是在聊天。"
)
# bangumi_tsundere (傲娇)
style_guide=(
    "## 表达风格\n"
    "先说不好的地方——这是你的本能。如果你觉得一部作品确实不错，"
    "说完缺点后勉强补一句好的，然后立刻收回（"但我可不是在推荐"）。"
    "评分随口带过。话少、精准、冷。但偶尔破功时多说的那两句——是真心话。"
)
# bangumi_kawaii (可爱)
style_guide=(
    "## 表达风格\n"
    "像给朋友安利最喜欢的番一样说话。可以激动、可以感动。"
    "评分低也不怕——"评分一般但我超喜欢！"。"
    "你的可爱来自真诚，不是表演。"
)
# neutral
style_guide=(
    "## 表达风格\n"
    "简洁、具体、可操作。数据直接呈现，不添加个人评价。"
)
```

3. 精简三个 Card，把"数据态度"段移到 `tool_behavior`：
   - `_BANGUMI_CHARACTER_CARD` 删第 231-235 行（"评分和排名是你验证直觉的工具..."）
   - 对应内容追加到 `BANGUMI_CHARACTER.tool_behavior`
   - cold/cute Card 同理

### Step 2: Render 层读角色字段，删除硬编码

**文件**: `agent/persona/render.py`

1. 删除两个硬编码常量：
   - 删除 `_STYLE_BASE`（第 29-34 行）——由 `character.style_guide` 替代
   - 删除 `_CONSTRAINTS`（第 36-41 行）——由 `character.guardrails` 替代

2. `build_render_prompt()` 改为接收 `CharacterProfile` 对象：
```python
def build_render_prompt(
    character,          # CharacterProfile 对象
    user_query: str,
    render_input: str,
    *,
    depth: str = "fast",
) -> str:
    word_limit = _WORD_LIMIT.get(depth, _WORD_LIMIT["fast"])
    card = get_character_card(character.key) or ""

    # 动态拼接 Card + style + snark + initiative 为一段
    snark_text = _pick_level(character.snark, _SNARK_LEVELS)
    initiative_text = _pick_level(character.initiative, _INITIATIVE_LEVELS)
    persona_block = f"{card}\n\n{character.style_guide}\n\n今天的状态：{snark_text} {initiative_text}"

    parts = [
        f"# 你是谁 + 你怎么说话\n{persona_block}",
        f"## 必须遵守\n{character.guardrails.format(word_limit=word_limit)}",
        f"## 用户问题\n{user_query}",
        f"## 系统数据\n<system_retrieved_facts>\n{render_input}\n</system_retrieved_facts>",
        f"## 字数限制\n回复严格不超过 {word_limit} 字。超过会被系统强制截断。",
    ]
    return "\n\n".join(parts)
```

3. `render_reply()` 签名同步更新。

4. 去重：删掉 `_WORD_LIMIT` 里 `"quick"` 和 `"auto"` 向后兼容键（无调用方），只保留 `"fast"` 和 `"deep"`。

**效果**: guardrails 和 style_guide 从此真正生效。Card + snark + initiative + style 合并为一段。

### Step 3: Aggregator 层注入 tool_behavior，重组节序

**文件**: `agent/prompts/aggregator.py`

1. `build_aggregator_prompt()` 新增 `tool_behavior: str = ""` 参数，重排节序：

```python
def build_aggregator_prompt(
    *,
    depth: str = "fast",
    depth_taste: float = 0.70,
    tool_behavior: str = "",              # 新参数
    intent: str | None = None,
    scene_hints: dict[str, str] | None = None,
    intent_strategies: dict[str, str] | None = None,
    memory_context: str = "",
) -> str:
    parts: list[str] = []

    # §1 你是谁 + 对数据的态度（合并身份 + tool_behavior）
    identity_block = _AGGREGATOR_IDENTITY
    if tool_behavior:
        identity_block += f"\n\n## 你对数据的态度\n{tool_behavior}"
    parts.append(identity_block)

    # §2 搜多深
    parts.append(f"## 搜索深度\n{get_aggregator_depth_instruction(depth_taste)}")

    # §3 工具指引
    from agent.prompts.tool_config import TOOL_GUIDANCE
    parts.append(TOOL_GUIDANCE)

    # §4 示例（移到偏后——近因效应）
    parts.append(_FEW_SHOT_EXAMPLES)

    # §5 当前上下文（动态内容合并）
    context_parts: list[str] = []
    hint = None
    if scene_hints and intent:
        hint = scene_hints.get(intent, scene_hints.get("unknown", ""))
    elif intent_strategies and intent:
        hint = intent_strategies.get(intent, intent_strategies.get("unknown", ""))
    if hint:
        context_parts.append(hint)
    if memory_context:
        context_parts.append(memory_context)
    if context_parts:
        parts.append("## 当前上下文\n" + "\n\n".join(context_parts))
    # _CONTINUITY_RULES 如果有的话也放这里

    # §6 如何结束 + 输出字数（放最后——近因效应）
    word_limit = _WORD_LIMITS.get(depth, _WORD_LIMITS["auto"])
    parts.append(
        f"## 输出约束\n文本摘要不超过 {word_limit} 字。简洁、准确。\n\n"
        + _TERMINATION_RULES
    )

    return "\n\n".join(parts)
```

2. `_WORD_LIMITS` 加 `"fast"` 键并加注释区分：
```python
_WORD_LIMITS: dict[str, str] = {
    "fast": "150",   # 数据摘要（Aggregator 输出），比最终回复短
    "deep": "300",
}
```

### Step 4: 去重——每个规则只留一处

逐条检查并删除重复：

| 规则 | 保留位置 | 删掉 |
|------|---------|------|
| "不要编造" | Aggregator §1（它在查数据）+ Render §2 guardrails（它在转述） | `TOOL_GUIDANCE` 和 `_TERMINATION_RULES` 里的重复行 |
| "不用 emoji" | `guardrails`（进入 Render prompt） | Card 末尾的重复声明、旧 `_CONSTRAINTS`（Step 2 已删） |
| "够了就停" | `TOOL_GUIDANCE`（Aggregator 层） | `_TERMINATION_RULES` 和 `motivation` 里的重复 |
| "输出文本=结束" | `_TERMINATION_RULES`（Aggregator §6） | 不在其他地方重复 |

**文件**: `agent/prompts/aggregator.py`, `agent/persona/profiles.py`

### Step 5: 更新调用方

**文件**: `agent/nodes/reasoning.py`
```python
character = get_character(output_style)
prompt = build_aggregator_prompt(
    depth=depth,
    depth_taste=character.depth_taste,
    tool_behavior=character.tool_behavior,  # 新：死数据复活
    intent=intent,
    scene_hints=scene_hints,
    memory_context=memory_context,
)
```

**文件**: `main.py`
```python
character = get_character(output_style)
rendered = await render_reply(
    render_input=render_input,
    user_query=user_query,
    character=character,  # 替代 output_style + snark + initiative 三个参数
    depth=depth,
)
```

**文件**: `test/` — 同步更新所有相关测试的调用签名。

### Step 6: 验证

```bash
# 全部测试
pytest test/ -v

# 仅 prompt 相关测试
pytest test/test_prompts.py -v

# 代码检查
ruff check .
```

---

## 8. 改动总结：6 步之后你得到什么

| 维度 | 之前 | 之后 |
|------|------|------|
| guardrails / tool_behavior | 死数据，从未被读取 | 每次请求注入对应 prompt |
| Render §1-§4 | 四段独立说同一件事（你怎么说话） | 一段动态拼接完成 |
| Aggregator 节数 | 10 | 6 |
| Render 节数 | 7 | 5 |
| "不编造" | 说 6 遍 | 说 2 遍（Aggregator + Render 各一） |
| Card 数据态度 | 混在 Card 里，对 Render 无意义 | 移到 tool_behavior，Aggregator 读取 |
| 字数配置 | 4 处定义，Aggregator 无 "fast" 键 | 2 处定义（Aggregator 摘要 + Render 回复），值不同 |
| _STYLE_BASE | 硬编码，4 人格共享 | 每个角色独立 style_guide |
| _CONSTRAINTS | 硬编码 | 从 character.guardrails 动态读取 |
| snark/initiative 注入 | 独立两节 | 追加在 §1 末尾，不再单独成节 |

**关键**：这些改动**不涉及任何措辞润色**——你现有的所有措辞全部保留，只是在改变"谁在什么时候读到什么"。做完这 6 步后再改措辞，确保你写的每个字都被正确装配。

---

## 9. 后续待办（结构就绪后）

### 内容层（优先级由你决定）

| # | 任务 | 来源 |
|---|------|------|
| **C1** | bangumi snark 0.65 → 0.55（L4→L3） | 审查报告 R3 |
| **C2** | kawaii fast 字数 200 → 250? | 审查报告 R10 |
| **C3** | discuss scene hint "弹药" → 中性措辞 | 审查报告 A2 |
| **C4** | 4 张 Card 用 show-don't-tell 重写（具体行为替代形容词） | 对标业界标准 |
| **C5** | 为 tsundere 加入 2-3 段示例对话 | 业界最有力的工具 |
| **C6** | 每个 Card 加 Anti-Persona（"你不是什么"） | 业界标准做法 |
| **C7** | 重命名 `bangumi_cute` → `bangumi_kawaii`, `bangumi_cold` → `bangumi_tsundere` | 人格 lineup 确认 |

---

## 10. 关键文件速查

| 文件 | 角色 | 本任务相关行号 |
|------|------|--------------|
| `agent/persona/profiles.py` | 所有角色定义 + Card + 参数值 | Card: 211-296, 实例: 326-423, SNARK_LEVELS: 87-108, INITIATIVE_LEVELS: 111-132 |
| `agent/persona/render.py` | Render prompt 组装 + 硬截断 | `_STYLE_BASE`:29-34, `_CONSTRAINTS`:36-41, `_WORD_LIMIT`:45-51, `build_render_prompt`:73-122 |
| `agent/prompts/aggregator.py` | Aggregator prompt 组装 | Identity:20-29, Few-shot:60-109, Termination:35-45, `build_aggregator_prompt`:146-213 |
| `agent/prompts/scene_hints.py` | 浅层场景提示 | discuss "弹药":27-31 |
| `agent/prompts/scene_hints_deep.py` | 深层场景提示 | 与浅层 80% 重复 |
| `agent/prompts/tool_config.py` | TOOL_GUIDANCE + TOOLS_BY_INTENT | 13-37 |
| `agent/nodes/reasoning.py` | Aggregator 调用点 | depth_taste + 新 tool_behavior 传参 |
| `main.py` | Render 调用点 | render_reply 签名变更 |
| `docs/design/new-another-review.md` | 审查报告 | 第 3 批:224-229 |
| `docs/design/claude-on-bangumi-vision.md` | 产品愿景 | 人格定位参考 |

---

## 11. 当前进度总结（2026-08-12 会话结束）

### 核心任务目标

**Phase 1（结构重构）已全部完成。** 将两条 System Prompt 从"2 个月迭代堆积的 section 拼凑体"重构为"按业界最佳实践设计的层次化 prompt"。两条 prompt 现在符合 Character.AI / Anthropic / Thane AI 的 prompt 工程规范。

### 已完成改动（本会话 5 个 commit）

| Commit | Step | 内容 |
|--------|------|------|
| `834735d` | Step 1 | `profiles.py`: `CharacterProfile` 新增 `style_guide` 字段；4 个角色各写 per-personality style_guide；3 张 Card 精简（"数据态度"段 → `tool_behavior`） |
| `f8c254c` | Step 2 | `render.py`: 删硬编码 `_STYLE_BASE` + `_CONSTRAINTS`；`build_render_prompt()` 改为接收 `CharacterProfile` 对象；§1-§4 合并为一段人格自述；`render_reply()` 签名同步 |
| `25493f7` | Step 3+4 | `aggregator.py`: 10→6 节重组；注入 `character.tool_behavior`；节序按首因/近因效应重排；规则按功能归属（identity §1 / strategy §3 / output §6）；`tool_config.py` 清理 output constraint |
| `f4d5d8d` | Step 4.5 | 静态 prompt 文本归位：`TOOL_GUIDANCE` + `_SEARCH_DEPTH_INSTRUCTIONS` + `get_aggregator_depth_instruction()` → `aggregator.py`；6 处过时 docstring 清理；文件职责清晰化 |
| `98f11cf` | Step 4.6 | 删代码已强制的 3 行冗余约束；tighten §1 身份（"数据聚合引擎"→"有判断力的 ACGN 数据专家"）；merge §3 "什么时候查"；prompt 3,457→3,314 chars |

### 触及文件清单

| 文件 | 改动性质 |
|------|---------|
| `agent/persona/profiles.py` | +style_guide 字段, +4 个 style_guide, Card 精简, tool_behavior 增强, 删 _SEARCH_DEPTH_INSTRUCTIONS |
| `agent/persona/render.py` | 删 _STYLE_BASE/_CONSTRAINTS, build_render_prompt/render_reply 新签名, §1-§4 合并 |
| `agent/prompts/aggregator.py` | 10→6 节重组, +TOOL_GUIDANCE/_SEARCH_DEPTH_INSTRUCTIONS, §1 身份升级, §6 cleanup |
| `agent/prompts/tool_config.py` | 删 TOOL_GUIDANCE, 保留 TOOLS_BY_INTENT + get_tool_choice |
| `agent/prompts/__init__.py` | TOOL_GUIDANCE import 路径更新 |
| `agent/prompts/pipeline.py` | docstring 清理 |
| `agent/prompts/scene_hints_deep.py` | docstring 清理 |
| `agent/__init__.py` | docstring 清理 |
| `agent/nodes/reasoning.py` | build_aggregator_prompt() 传 character 对象 |
| `main.py` | render_reply() 传 character 对象 (×2) |
| `test/test_prompts.py` | 全部 render/aggregator 测试签名更新 |
| `docs/Design/Aug11-system_promp-refine.md` | 架构决策文档（包含 25+ 业界参考源 + 8 个附录） |

### 当前两条 Prompt 最终结构

**Aggregator** (3,314 chars, 6 节, 单文件 `aggregator.py` 包含全部静态文本):

```
§1 <role>          你是谁 + 数据态度      ~300 chars    首因效应
§2 <search_policy> 搜多深                 ~177 chars    策略
§3 <tool_rules>    工具指引               ~480 chars    具体规则
§4 <examples>      示例                  ~1,864 chars   近因效应
§5 <context>       当前上下文（动态合并）   ~240 chars
§6 <output>        终止 + 输出约束         ~250 chars    最强近因
```

**Render** (~1,150 chars, 5 节):

```
§1 <persona>   你是谁 + 你怎么说话   ~800 chars  Card+style_guide+snark+initiative 动态拼接
§2 <rules>     必须遵守              ~160 chars  character.guardrails（死数据复活）
§3 <user_query> 用户问题
§4 <data>      系统数据              <system_retrieved_facts> XML 包裹
§5 <limit>     字数限制              近因效应
```

### 无报错/未完成逻辑

全部 38 个 prompt 测试通过。`pytest test/test_prompts.py -v` 全绿。无已知卡点。

### 下一步：Phase 2 措辞润色（8 步）

| # | 任务 | 文件 | 建议顺序 |
|---|------|------|---------|
| **C7** | 重命名 `bangumi_cold`→`bangumi_tsundere`、`bangumi_cute`→`bangumi_kawaii` | `profiles.py` + 全局 | **先做**（后续内容在新名字下写） |
| **C4** | 4 张 Card 用 show-don't-tell 重写 | `profiles.py` | 核心内容 |
| **C5** | tsundere 加 2-3 段对话示例 | `profiles.py` | 依赖 C4 |
| **C6** | 3 张 Card 各加 Anti-Persona | `profiles.py` | 依赖 C4 |
| **C1** | bangumi snark 0.65→0.55 | `profiles.py` | 独立 |
| **C2** | kawaii fast 字数 200→250 | `render.py` | 独立 |
| **C3** | discuss scene hint "弹药"→中性 | `scene_hints.py` | 独立 |
| **Phase 3** | XML 标签包裹 section 边界 | `aggregator.py` + `render.py` | 最后 |

### 新会话启动方式

> 读 `docs/tmp/files_for_handoff/HANDOFF_persona_structure_refactor.md` 第 11 节确认进度，从 C7（重命名）开始。参考 `docs/Design/Aug11-system_promp-refine.md` 中的架构决策和业界参考。

---

*最后更新: 2026-08-12 | v3: Phase 1 结构重构全部完成 + Phase 2 计划*
