# 从 Phase 6.5 到 Claude on Bangumi — 分层演进路线

> 2026-07-27 | 基于四层架构的演进推演，对应愿景文档 [`claude-on-bangumi-vision.md`](claude-on-bangumi-vision.md)

---

## 推演原则

当前四层架构（编排/人格/记忆/数据）设计良好——**每一层可以独立演化，不需要推到重来**。Phase 7 的所有改动都是在现有结构上加字段、加文件、加参数，reasoning → tool → render 管线不动。

---

## 编排层：从"等用户说话"到"感知用户行为"

**当前**：单一 `POST /chat` 触发。编排层只在收到 HTTP 请求时才醒来。

**目标**：被动触发——用户标记作品、浏览页面、参与讨论，都是触发点。

### Phase 7（近期可做）：context 参数 — 编排层感知页面场景

新增 `context` 参数，让编排层知道"这个请求来自什么场景"：

```json
POST /chat {
  "message": "帮我看看这部",
  "context": {
    "type": "subject_page",
    "subject_id": 265,
    "trigger": "user_mark",
    "user_action": "标记为看过，给了 9 分"
  }
}
```

- `context.type`：`subject_page` | `discussion` | `timeline` | `profile`
- `context.trigger`：`user_mark` | `user_browse` | `user_mention` | `manual`
- `context.user_action`：自然语言描述用户刚才做了什么

编排层的策略文件（`orchestrate/strategies.py`）不需要大改——`context.type` + `context.trigger` 作为 prompt 的额外注入维度。classifier 的意图分类可以多一个 `context_aware` 分支。

### Phase 8+（中远期）：事件驱动 — triggers.py

从 HTTP 到事件驱动。新增事件监听模块：

```
agent/orchestrate/triggers.py    # 事件监听 → 判断是否该说话
```

职责：
- 收到"用户标记了《千年女优》9 分"事件 → 判断是否值得说话
- 规则：评分 ≥ 9 且条目是经典 → 值得说；连续标记 5 部同类 → 值得说；普通标记 → 不说话
- 可以用轻量规则引擎或便宜的 LLM 调用来做"是否该说话"的决策

### Phase 9（远期）：社区参与

在讨论帖里以独立身份发言。有立场、有论据、可以被反驳。这需要：
- 讨论帖内容理解（context.type = "discussion"）
- 公开人格（和私聊人格可以不同——公开场合更克制）
- 时机判断（不是每个帖子都插嘴）

---

## 人格层：从"二元角色"到"多维人格参数"

**当前**：两个 CharacterProfile（bangumi/neutral），render_node 处理工具回复的风格转换。

**目标**：可调人格维度 + 人格一致性。

### Phase 7（近期可做）：人格参数化

在 `CharacterProfile` dataclass 中增加可调维度：

```python
@dataclass
class CharacterProfile:
    # 现有字段
    identity: str
    motivation: str
    expression_guide: str
    guardrails: str
    tool_behavior: str

    # 新增：人格维度（0.0 ~ 1.0），默认值匹配当前 bangumi 损友人格
    snark_level: float = 0.7        # 毒舌度——越高越损
    nostalgia_bias: float = 0.5     # 怀旧度——越高越偏爱老作品
    academic_depth: float = 0.3     # 学术度——越高越爱引用理论和历史
    initiative: float = 0.5         # 主动性——越高越爱反问和主动 offer
```

参数不直接暴露给终端用户（至少一开始不）。在 `prompt_builder.py` 中根据参数动态生成 `expression_guide` 的变体：

```
snark_level=0.3 → "语气友好，吐槽温和，多用'我觉得'而非'你竟然'"
snark_level=0.9 → "语气毒舌，可以犀利地指出作品的槽点，用反问句增加杀伤力"

academic_depth=0.3 → "用人话解释，不要引经据典"
academic_depth=0.9 → "可以引用动画史和导演作品序列，展开技术分析和叙事结构讨论"
```

`render.py` 的 `_RENDER_STYLE` 同步感知参数——高毒舌度下，数据呈现的吐槽味更重。

### Phase 8+（中远期）：人格一致性 + 人格学习

- 记忆层存储的不是"用户偏好机战"，而是**"和这个用户说话时，他喜欢我毒舌"** → 人格参数根据用户反馈自动微调
- 不同用户拥有不同的 Claude 人格——用户 A 的更毒舌，用户 B 的更温柔
- 公开讨论中的人格是"默认人格"，私聊中的人格随用户关系深度变化

---

## 记忆层：从"记住聊了什么"到"记住你是谁"

**当前**：L1 滑动窗口 + L2 语义召回（~700 tokens 注入）。记住的是对话内容。

**目标**：全量上下文 + 叙事性记忆。

### Phase 7（近期可做）：记忆类型扩展

当前 L2 只记对话摘要。扩展 `session_memories` 表的 `memory_type` 字段：

| 类型 | 内容 | 示例摘要 |
|------|------|---------|
| `chat` | 对话摘要（现有） | "用户询问了类似星际牛仔的作品，助手推荐了混沌武士" |
| `user_taste` | 评分事件 | "用户给了《千年女优》9 分，评论'今敏最高杰作'" |
| `opinion` | 用户观点 | "用户认为 EVA 新剧场版结局比旧的好——更治愈" |
| `discovery` | 发现冷门 | "用户在讨论帖中了解到 8.5 分冷门 OVA《猫汤》" |

每种类型用专门的 prompt 模板生成摘要。召回时混合类型，优先召回同类型记忆。

### Phase 8（中期）：全量上下文窗口

当底层模型上下文足够大时（200K+），不走"召回 → 注入"模式，而是直接把最近的评分、对话摘要、核心观点全部塞进 System Prompt。架构不需要大改——记忆层的"注入"接口不变，只是注入量变大。

### Phase 9（远期）：叙事性记忆

不是"注入 5 条记忆"，而是让 LLM 在 reasoning 之前先做一次"回忆"：

> "我来想想……用户去年开始追原创动画，今年口味明显变重了。他给了《千年女优》9 分但对《红辣椒》只给了 7 分——这个人喜欢情感密度胜过技术炫技。他现在问'还有什么类似的'，我觉得《东京教父》可能更对味。"

这个"回忆"是一个轻量的 LLM 调用，结果是一段自然语言，比 JSON 列表更有人味。

---

## 数据层：从"工具查询"到"叙事性洞察"

**当前**：16 个工具，返回结构化 dict。工具依赖硬编码在 `deep_strategies.py`。

**目标**：分析型工具 + 工具组合引擎。

### Phase 7（近期可做）：新增分析型工具

在现有工具基础上增加 2-3 个"叙事原料"型工具。走同一套管线（schemas → sanitizers → client → @tool），不碰编排层：

| 工具 | 功能 | 实现方式 |
|------|------|---------|
| `analyze_user_taste(user_id)` | 评分分布、类型偏好、评分趋势——不是图表数据，是结构化的叙述原料 | Bangumi API 拉收藏 → Python 聚合统计 → 返回 dict |
| `get_director_filmography(person_id)` | 按时间排序的全部作品 + 评分趋势 + 风格演变关键词 | person API + subject API 组合 |
| `find_similar_users(user_id, dimension)` | Top-N 相似用户 + 差异点（评分/类型/时间线维度） | 需要一定量的公开用户数据 |

### Phase 8+（中期）：工具组合引擎

当前工具依赖硬编码在 `TOOL_DEPENDENCY_CONSTRAINT`。可以让编排层自己做工具链规划：

> 用户说"分析导演风格演变" → LLM 自己规划：search → director_filmography → 逐个 subject_detail → 聚合分析

不需要开发者预定义链——LLM 看到工具的 docstring 就知道怎么组合。当前 16 个工具规模下几乎已经可以做到（deep 模式本就是 LLM 自主规划），只是 prompt 里没有明确鼓励工具链的灵活组合。

---

## 汇总：三阶段时间线

```
Phase 7（近期，1-2 周可落地）              Phase 8（中期，需模型配合）        Phase 9（远期）
─────────────────────────────────────── ────────────────────────────── ─────────────────────
编排: context 参数感知页面场景            编排: 事件驱动 + triggers.py      编排: 社区参与
人格: CharacterProfile 人格参数化         人格: 一致性 + 人格学习           人格: 公开/私聊人格分离
记忆: memory_type 扩展                   记忆: 全量上下文窗口              记忆: 叙事性"回忆"
数据: 2-3 个分析型工具                    数据: 工具组合引擎                数据: 实时社区数据流
```

**所有 Phase 7 的改动都是增量式的**——加字段、加文件、加参数。现有的 reasoning → tool → render 管线不需要改动。四层架构的独立性让每一步都可以单独验证、单独上线。

---

## 与现有文档的关系

- [`claude-on-bangumi-vision.md`](claude-on-bangumi-vision.md) — 产品愿景（"是什么"）
- 本文档 — 演进路线（"怎么到那里"）
- [`ROADMAP.md`](ROADMAP.md) — 当前状态 & 近期路线图
- [`CLAUDE.md`](../../CLAUDE.md) — 四层架构详解
