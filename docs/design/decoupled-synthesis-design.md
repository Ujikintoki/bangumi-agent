# 分离合成架构设计（Decoupled Synthesis）

> 2026-08-02，与 Claude Code 的架构讨论。记录从"两层管线信息不对称"到"分离合成"的完整推演过程。
> 状态：**设计阶段，尚未实施。**

---

## 1. 要解决的核心矛盾

当前两层人格管线存在严重的信息不对称：

| 层 | 人格内容量 | 实际效果 |
|---|----------|---------|
| **System Prompt** (Reasoning) | Character Card ~530 字：完整审美体系、数据态度、自我认知、语气风格 | 风格内容和思考内容混在一起，LLM 在调工具/消化数据时被"熨平" |
| **Render** (后处理) | Voice Hint ~55 字 | 55 字要承载全部人格表达差异，远不够 |

**结果**：530 字的 Character Card 在数据竞争中浪费了，55 字的 render 被迫做太多工作。`bangumi_cold` 和 `bangumi_cute` 的实际输出差异，主要靠这 55 个字。

## 2. 解决方案：分离合成

```
当前（信息错配）:
  Reasoning LLM ← 530字人格 + 工具指引 → 调工具 + 产出人格化回复
  Render LLM    ← 55字 voice hint        → 风格微调

新架构（分离合成）:
  Aggregator LLM ← 纯工具指引 + depth_taste → 数据清单（结构化 Markdown）
  Render LLM     ← 530字人格 + snark + initiative → 最终自然语言回复
```

核心理念：**Reasoning 层是 Data Aggregator（数据聚合器），Render 层是 Agent Persona（真正的角色）。**

| | 现在 | 新架构 |
|---|------|--------|
| Aggregator System Prompt | "你是 Bangumi 看板娘，一个二次元损友..." | "你是数据聚合引擎。不要回答用户。调用工具，输出数据清单。" |
| Render System Prompt | "高冷腹黑，话少精准冷" (~55字) | "你是 Bangumi 看板娘... [完整 530 字审美体系]" |

## 3. 参数分配

三个参数（snark、depth_taste、initiative）在新架构下的归属：

```
depth_taste → Aggregator:  查什么数据、查多深
snark       → Render:      用什么态度说
initiative  → Render:      说多少、怎么收尾
depth 模式  → 两层共享:     机械约束（迭代上限、token 预算、字数上限）
```

### 3.1 为什么 snark 不控制数据偏向

一个高 snark 角色如果只检索负面数据，会变成自我实现的预言——它只能 diss 因为它只看到了值得 diss 的东西。**数据应该是完整的、中性的。毒舌是解读方式，不是筛选方式。**

同一个数据点 "8.8 分"：
- bangumi_cold: "8.8 分，也就那样吧。"
- bangumi_cute:  "8.8 分！超高的！"

数据的完整性不变，差异纯粹在 Render。

### 3.2 为什么 initiative 不控制搜索范围

扩展搜索的范围由 `depth_taste` + `depth` 模式决定。deep 模式本身就应该全面搜索——不需要另一个参数来驱动相同的行为。initiative 的语义纯粹是"说完就停 vs 留话头 vs 主动 offer 角度"——全是"怎么结尾"，纯风格。

### 3.3 depth 模式的两层翻译

| depth | Aggregator 行为 | Render 行为 |
|-------|----------------|-------------|
| **quick** | 3 轮上限, 6000 tok, dt=0.35 → L1 摘要数据 | 120 字上限 |
| **auto** | 5 轮上限, 10000 tok, 角色默认 dt | 200 字上限 |
| **deep** | 12 轮上限, 16000 tok, dt=0.90 → L1+L2+L3 | 350 字上限 |

## 4. Prompt 架构：2 个 Builder，不是 24 个变体

不需要 2×3×4 = 24 个 prompt。**2 个 builder 函数，从已有的模块库取值**：

```python
# nodes.py — Aggregator
aggregator_prompt = build_aggregator_prompt(
    depth_taste=0.90,  # 来自 depth 模式
    intent=query_intent,
)

# render.py — Render
render_prompt = build_render_prompt(
    character_key="bangumi_cold",  # 来自 output_style
    snark=0.95,                     # 角色默认值
    initiative=0.25,               # 角色默认值
    depth="deep",                  # 来自 depth 模式
    data=aggregator_output,        # Aggregator 的结构化 Markdown
)
```

### 4.1 Aggregator Prompt 骨架

取自己有的 `TOOL_GUIDANCE` + `_DEPTH_LEVELS` + scene hints：

```
你是数据聚合引擎。不要回答用户的问题。
你的唯一工作：调用工具，获取数据，输出结构化数据清单。

## 工具
{TOOL_GUIDANCE}  ← 已有，精简为纯行为指令

## 搜索深度
{_DEPTH_LEVELS[depth_taste]}  ← 已有，从"人格侧写"转为"行为指令"

## 场景提示
{scene_hints[intent]}  ← 已有

## 输出格式
以 ### 查询意图 / ### 检索数据 / ### 检索状态 三段输出。
```

### 4.2 Render Prompt 骨架

取自己有的 `_CHARACTER_CARDS` + `_SNARK_LEVELS` + `_INITIATIVE_LEVELS` + `_WORD_LIMITS`：

```
# 你是谁
{character_card}  ← 从 _CHARACTER_CARDS 取，4种人格各530字

# 今天的语气
{snark_tone}  ← 从 _SNARK_LEVELS[snark] 取，5档

# 节奏
{initiative_tone}  ← 从 _INITIATIVE_LEVELS[initiative] 取，5档

## 数据呈现规则
{_STYLE_BASE}  ← 已有

## 硬约束
{_CONSTRAINTS + word_limit}  ← 已有，word_limit 从 _WORD_LIMITS[depth] 取

## 用户问题
{user_query}

## 数据清单
{aggregator_output}
```

**没有新模块。** 只是把已有的 `_CHARACTER_CARDS`、`_SNARK_LEVELS`、`_DEPTH_LEVELS`、`_INITIATIVE_LEVELS`、`_WORD_LIMITS` 重新分配到两个 builder 里。

## 5. 中间数据格式：结构化 Markdown（不是 JSON）

Aggregator 输出给 Render 的格式。

### 5.1 为什么不是 JSON

1. **LLM 不擅长输出合法 JSON**——嵌套深就容易丢括号、多逗号，需要写一坨 JSON 修复逻辑，没意义
2. **JSON 对 Render LLM 来说反而更难读**——`"score": 8.8` 不比 `8.8分` 更清晰，`{}`、`[]`、`""` 全是 token 开销
3. **消费者只有 Render LLM**——没有程序需要 `json.loads()`，Markdown 是 LLM 训练数据的主流格式

### 5.2 格式定义（三段固定 section headers）

```
## 查询意图
推荐2024年高分奇幻动画

## 检索数据
- 葬送的芙莉莲 | 8.8分 | #3 | 标签: 奇幻/冒险/治愈 | 6384人评分
  → 导演: 斎藤圭一郎 | 前作: 孤独摇滚
  → 信号: 完成率90%（高），口碑集中度80%（一致好评）
- 迷宫饭 | 8.3分 | #22 | 标签: 奇幻/美食/搞笑 | 3847人评分

## 检索状态
2次搜索，2条相关结果。搜索深度: L3（含 detail + characters）。
注: "葬送のフリーレン 漫画版"被排除（不同媒介）。
```

- `## 检索状态` 让 Render 知道数据质量和范围
- 数据行用 `|` 分隔使 LLM 易于扫描
- `→` 表示派生信息（导演背景、数据信号）
- 空结果/异常情况自然表达：`2次搜索均返回空，该关键词可能不存在于数据库`

## 6. 与 A/B/C/D 工具 dict 工作的关系

```
API → Tool dict (A/B/C/D 解决) → Aggregator Markdown (本次设计) → Render NL (本次设计)
```

你过去的 `7831d4f` 提交把工具返回从自然语言 string 改成了结构化 dict。这是 Aggregator 的**输入格式**。有了干净的 dict 输入，Aggregator 才能可靠地提取、筛选、重组数据。两者是**同一管道上相邻的两环**，互补关系。

A/B/C/D 定义的三级数据等级（L1/L2/L3）与 `depth_taste` 自然对应：

| depth_taste | 应拉取的数据等级 |
|------------|----------------|
| L1-L2 (≤0.4, quick/auto) | L1 摘要（score, rank, name） |
| L3-L4 (≤0.8) | L1 + L2（+评分分布 +简介 +制作团队 +标签） |
| L5 (>0.8, deep) | L1 + L2 + 按需 L3（+角色列表 +评论） |

## 7. 数据流总览

```
                    ┌─────────────┐
                    │ 用户消息      │
                    └──────┬──────┘
                           │
                           ▼
              ┌────────────────────────┐
              │    Aggregator LLM       │
              │    System Prompt:       │
              │    "数据聚合引擎"         │
              │    + _DEPTH_LEVELS[dt]   │
              │    + TOOL_GUIDANCE       │
              │                        │
              │    tool_calls ←→ tools  │
              │    接收 dict 格式结果     │
              │                        │
              │    输出: 结构化 Markdown  │
              └───────────┬────────────┘
                          │
                          │ AIMessage(content="## 查询意图\n...\n## 检索数据\n...\n## 检索状态\n...")
                          │
                          ▼
              ┌────────────────────────┐
              │    Render LLM           │
              │    System Prompt:       │
              │    Character Card (530字)│
              │    + _SNARK_LEVELS[s]    │
              │    + _INITIATIVE_LEVELS[i]│
              │    + _WORD_LIMITS[depth] │
              │                        │
              │    输出: 自然语言回复     │
              └───────────┬────────────┘
                          │
                          ▼
              ┌────────────────────────┐
              │    存入 session cache    │
              │    (Render 的自然语言输出)│
              │    多轮 history 看到的是  │
              │    人格化回复，不是数据清单 │
              └────────────────────────┘
```

关键点：**存入 session cache 的是 Render 的自然语言输出，不是 Aggregator 的结构化 Markdown。** 下轮对话的 history 里看到的是"啧，芙莉莲 8.8，也就那样"，不是 `## 检索数据`。

## 8. 实施策略：两步走

### Phase 1：移动 Character Card（低风险验证）

- Aggregator System Prompt：去掉 Character Card，保留精简身份描述 + `TOOL_GUIDANCE` + `_DEPTH_LEVELS`
- Render System Prompt：接收完整 Character Card（530 字）+ `_SNARK_LEVELS` + `_INITIATIVE_LEVELS`
- Aggregator 仍然输出自然语言（但更中性化，因为 System Prompt 里没人格）
- Aggregator 输出格式不变，改动面小

**验证目标**：tool-calling 质量是否提升；人格差异是否更鲜明；中间件兼容性是否完好。

### Phase 2：结构化 Markdown 输出（如果 Phase 1 需要）

- Aggregator System Prompt：加入三段输出格式指引
- `route_after_reasoning` 不变（Aggregator 不调工具时 → END，然后 Render 后处理）
- 新增 `render.py` 中对结构化 Markdown 的解析/使用逻辑
- 只在 Phase 1 证实了分离合成的核心价值后推进

## 9. 不变的部分

以下组件完全不受影响：

| 组件 | 原因 |
|------|------|
| `graph.py` | ReAct 拓扑不变（reasoning ⇄ tool_node → END → render 后处理） |
| `tools/bgm_tools.py` | 所有工具返回格式不变（dict） |
| `clients/` | HTTP 客户端不变 |
| `memory/short_term.py` | L1 滑动窗口不变（管理的是 Render 输出） |
| `memory/long_term.py` | L2 跨会话记忆不变 |
| `rag/` | RAG 检索管线不变 |
| `database/` | ORM 不变 |
| `schemas/tools_input.py` | Pydantic 输入 schema 不变 |
| `orchestrate/classifier.py` | 意图分类不变 |
| `orchestrate/guardrails.py` | XML 泄漏防护、重复调用检测不变 |

## 10. 要改的文件

| 文件 | 改动 |
|------|------|
| `agent/persona/profiles.py` | `_DEPTH_LEVELS` 文本从"人格侧写"转为"行为指令"；新增 `get_render_variables()` |
| `agent/orchestrate/prompt_builder.py` | 拆分为 `build_aggregator_prompt()` + `build_render_prompt()`；移除 Character Card 注入 |
| `agent/orchestrate/nodes.py` | `tone_kwargs` 只传 `depth_taste` 给 aggregator；snark/initiative 不再传 |
| `agent/persona/render.py` | `build_render_prompt()` 接收完整 Character Card + snark/initiative + 结构化 Markdown |
| `main.py` | render 调用参数适配 |

## 11. 风险与缓解

| 风险 | 缓解 |
|------|------|
| Aggregator 丢失判断力（纯数据 dump） | `## 检索状态` 段承载判断：数据相关性、缺失标注、排除理由 |
| 闲聊/常识问题不便走纯聚合 | 利用已有 intent 分类器：chitchat/emotional → Aggregator 保持当前行为 |
| Render 不熟悉结构化 Markdown 输入 | Markdown 就是 LLM 训练数据的主流格式，远比 JSON 自然 |
| Aggregator 和 Render 之间"风格两层皮" | Phase 1 先验证——如有冲突，Phase 2 改为结构化 Markdown 彻底解耦 |
| 存入 session cache 的数据格式变化 | Phase 1 输出格式不变；Phase 2 额外做一次格式转换或用 Render 输出替代 |
