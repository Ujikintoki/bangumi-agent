# Intent 路由层设计

> 状态：定稿，待实施
> 日期：2026-08-03
> 关联：[架构演进记录](architecture-evolution.md) · [分离合成设计 v2](decoupled-synthesis-design-v2.md)

## 1. 背景与问题

当前 DeepSeek 的架构中，所有"控制"都是 prompt 注入——分类器、scene hints、空结果警告、提前终止、最后一轮强制——全是往 prompt 里塞文本，赌 LLM 听话。没有一个是代码层的真正控制。硬熔断和 submit_facts 检测是唯二 LLM 无法反抗的机制，但它俩只负责"停下来"，不管"停得对不对"。

当前 4 intent（chitchat/lookup/discovery/realtime）的输出不控制任何代码路径——分类结果唯一的作用是选一段不同的 scene hint 文本注入 System Prompt。chitchat 和 lookup 走完全相同的 `reasoning_node → (tool_node) → render_node` 路径。

## 2. 产品定位

**Bangumi 的 AI 看板娘**：住在站内的、有性格的二次元损友。她可以查数据，但她存在的理由不是查——是陪你聊动画。

在 Agent 光谱上卡在 "ChatGPT 通用助手" 和 "Character.AI 角色扮演" 之间——有真实数据支撑的聊天角色（Companion Agent）。不是 Tool Agent，不是 Perplexity。

### Bangumi 平台属性

- 评分苛刻：注册用户才能评分，7 分以上已是广义"佳作"。agent 对评分的解读必须符合 Bangumi 的评分体系
- 讨论"浪人风格"：用户接受尖锐观点，不需要正能量过滤器
- 内容范围：以 ACGN 为核心，但群组帖子可能混入无关内容（金融、数码等）——正常处理即可
- 数据是配菜，人格是主菜。硬保证事实正确，软追求人格有趣

### 质量定义

| 维度 | 要求 | 不能忍 | 可以忍 |
|------|------|--------|--------|
| 数据 | 一定不能有事实性错误 | 评分写错、作品不认识、上映状态错 | 数据不够全（少查了一部类似的） |
| 表达 | 人格化、生动有趣、流畅有梗 | — | 分析简单了、不够深入 |
| 系统 | — | 响应慢、token 多 | — |

### 功能层级

- 第一层（默认）：ACGN 世界观 + 品位 + 立场。0-1 次工具调用
- 第二层（自然触发）：站内数据查询。1-2 次工具调用
- 第三层（显式激活）：深度分析（depth="deep"）。3-6 次工具调用

## 3. Intent 体系

### 设计原则

Intent = 用户想达成什么（goal），不是用户怎么说话（tone）。不同 intent → 不同处理策略 → 不同工具子集 + 不同迭代预算 + 不同终止条件。如果两个 intent 用完全相同的工具和迭代上限，它们应该是同一个 intent。

路由层是绝对互斥的 Action 分类，不是情绪分类。未来消息来源上下文（私聊 vs 群组帖子）可能影响默认意图假设，架构预留 `source` 字段。

### 6 Intent 定稿

#### chat — 无需工具

- **goal**: 社交连接、情绪表达、常识问答、纯人格互动
- **工具子集**: 无
- **迭代上限**: 0（跳过整个工具编排）
- **代码层行为**: `route_after_classify` → 直接进入 Render，不经过 tool_node
- **边界**: "你好"、"今天好累"、"什么是三集定律"、《日常》（2 char 短文本作品名——分类器需感知站内语境）、"EVA 真好看"（表达感受，不是获取数据）
- **群组无关帖子**: "你能接受霸凌者和被霸凌对象的CP吗"、"有什么动画以梦结局结尾" → chat 或按实际内容分类，正常处理

#### fetch — 获取单个确定实体的信息

- **goal**: "告诉我 X 的 Y"。用户知道要查谁，查什么。获取数据，不是形成观点
- **工具子集**: `search_bangumi_subject` + `get_subject_detail` + `get_subject_persons`
- **迭代上限**: 2
- **数据够了信号**: search 找到了 + detail 返回了 → 停
- **空结果处理**: 重试 1 次不同关键词 → 还空就停
- **包含**: "EVA 评分"、"杉田智和配过什么"、"进击的巨人讲什么"、裸标题 "EVA"（默认假设用户在查信息）

#### explore — 探索/发现/比较多实体

- **goal**: 用户没有确定目标（或目标有多个），需要搜索、筛选、比较。推荐、找同类、排行榜、对比
- **工具子集**: `search_bangumi_subject` + `get_subject_detail` + `get_subject_persons` + `get_subject_comments`（可选）+ `get_trending_subjects`
- **迭代上限**: 3（非 deep）/ 5（deep）
- **数据够了信号**: ≥2 个有效实体 + 各自的 detail
- **包含**: "推荐治愈番"、"类似日常的作品"、"EVA 和巨人哪个好"、"评分最高的 10 部"、"2024 年最佳动画"

#### discuss — 观点驱动的讨论

- **goal**: 用户的 goal 不是获取数据，而是围绕作品进行观点交流。争论、分析、二创、角色扮演式吐槽、回应观点
- **工具子集**: `search_bangumi_subject` + `get_subject_detail` + `get_subject_persons` + `get_subject_comments`（**必须拉**）
- **迭代上限**: 4（非 deep）/ 6（deep）
- **数据够了信号**: 实体数据 + 至少 1 批 comments → 够形成观点了，不需要"查全"
- **空结果处理**: 实体搜不到 → 诚实告知，"bangumi 上没收录，但就我的了解..."
- **包含**: "EVA 被高估了"、"以瓶子君口吻吐槽高达"、"巨人的结局真的很烂"、"分析一下芙莉莲为什么火"

**Bangumi 平台属性决定了 discuss 是核心场景而非边缘场景**——用户来 Bangumi 不只是查数据（那去豆瓣就够了），他们来这里是因为这里的评分有参考价值、讨论有信息密度、文化接受尖锐观点。comments 在 discuss 场景下不是"可选的附加数据"，而是核心食材。不拉评论就无法参与一个有 Bangumi 味的讨论。

#### realtime — 时效信息

- **goal**: 获取当前热门、放送排期、最新动态等时效性信息
- **工具子集**: `get_calendar` + `get_trending_subjects`
- **迭代上限**: 2
- **数据够了信号**: calendar 或 trending 任一返回 → 停，不需要串行深入
- **失败处理**: 工具没数据就诚实说"bangumi 上暂无"
- **为什么是独立 intent**: 工具集完全不同于 fetch（calendar/trending vs search/detail），训练数据不可用——必须查最新数据

#### fallback — 兜底

- **goal**: 分类器无法确定意图
- **工具子集**: 同 fetch
- **迭代上限**: 2
- **策略**: 保守——不假设用户想深度讨论

### Intent → 处理策略总览

| intent | 经过 tool_node | 工具子集（核心） | 迭代硬上限 | 数据够了信号 |
|--------|---------------|-----------------|----------|------------|
| chat | ❌ 跳过 | 无 | 0 | — |
| fetch | ✅ | search + detail + persons | 2 | search✓ + detail✓ |
| explore | ✅ | + trending + comments（可选） | 3/5 | ≥2 实体 + detail |
| discuss | ✅ | + comments（必须） | 4/6 | 实体✓ + comments✓ |
| realtime | ✅ | calendar + trending | 2 | 任一返回✓ |
| fallback | ✅ | 同 fetch | 2 | 同 fetch |

## 4. 架构原则

### 核心改动方向

把当前 while 循环从"LLM 自主驾驶"变成"代码层规划 + LLM 执行 + 代码层判断"。

1. **分类器 → 真正的闸门**：intent 决定图的边经过哪些节点。chat 跳过 tool_node，其余进入工具编排
2. **数据充分性 → 代码判断**：在 `route_after_tool` 中按 intent 检查数据是否足够，不靠 prompt 里的"够了请提交"
3. **迭代上限 → 按 intent 分层**：代码层的硬限制，不是 prompt 建议
4. **工具子集 → per-intent 过滤**：每个 intent 只暴露相关工具给 LLM，减少选择空间 → 减少出错概率

### 已有的正确设计（保留）

- **submit_facts_to_render 机制**：事实清单 → Render 层的结构化传递，防止 LLM 编造数据
- **硬熔断 + submit_facts 检测**：route_after_tool 中的代码层终止判断
- **Aggregator + Render 分离**：Aggregator 负责"食材安全"（数据收集），Render 负责"烹饪"（人格表达）

### 不需要的设计（移除）

- **Render 层的字数硬限制和完整性约束**：产品上"分析简单了没问题"，不应捆住 Render
- **Critic 节点**：当前已从图谱中移除。discuss 场景下"有趣"无法自动评判，不应恢复
- **正能量/安全护栏**：Bangumi 用户群不需要

## 5. 图结构设计

### 当前状态：一个 while loop

```
START → reasoning_node ⇄ tool_node → END → main.py render
         ↑ 做所有事: classify, memory,
            prompt, LLM, guardrails,
            retry, 6 个 prompt 注入...
```

两个条件边 `route_after_reasoning` 和 `route_after_tool` 是仅有的"控制"——所有真正的控制逻辑都在 reasoning_node 内部通过 prompt 文本注入实现。

### 目标：异质拓扑 + 控制下沉

```
                        ┌─── [chat] ────────────→ END ─── main.py render ──→
START → classify_node ──┤
                        └─── [fetch|explore|discuss|realtime|fallback]
                                   │
                                   ↓
                             reasoning_node ⇄ tool_node
                                   │
                                   └───→ END ─── main.py render ──→
```

**结构质变**：

1. **classify_node（新节点）**：独立 LLM 调用，只做分类。输出 intent + confidence → 写入 state
2. **chat 走完全不同的路径**：`classify → END → main.py render`，不经过 tool_node，不经过 ReAct 循环
3. **5 个 tool intent 共享 ReAct 循环**，但按 intent 参数化

### 关键改动：reasoning_node 瘦身

所有"控制"逻辑从 reasoning_node 移到路由函数，reasoning_node 变成纯 LLM 交互节点。

**移出（→ route 函数）**：
- `_EARLY_TERMINATION_HINT`（提前终止） → **删除**：per-intent 硬上限在路由里
- `_LAST_CHANCE_INSTRUCTION`（最后一轮强制） → **删除**：同上
- 空结果升级 HumanMessage → `route_after_tool` 中硬检查，2 次空搜索直接 END
- 重复调用检测 HumanMessage → `route_after_tool` 中硬检查，重复直接 END
- 终端回复逃逸舱 → `route_after_reasoning` 中检查
- Deep 首轮重试 → `route_after_reasoning` 中检查，LangGraph 原生重路由

**保留（20% 软引导）**：
- 消化态引导（唯一保留的软注入）："工具数据已返回。够了就 submit_facts，不够继续"
- 其余全是纯 LLM 交互：build prompt → bind tools → invoke → XML guard

**route_after_tool 成为控制中枢**：

```
route_after_tool（代码层硬控制）:
  ├─ iterations >= per-intent max?     → END（硬熔断）
  ├─ submit_facts detected?            → END
  ├─ >= 2 consecutive empty searches?  → END
  ├─ duplicate tool calls detected?    → END
  ├─ per-intent data sufficient?       → END
  └─ else                              → reasoning_node
```

### 80% 硬控制 / 20% 软引导

| 层级 | 机制 | 类型 |
|------|------|------|
| classify_node | intent 决定走哪条路径 | 硬 |
| route_after_classify | chat → 直接 END | 硬 |
| reasoning_node | Dynamic Tool Binding（per-intent 工具子集） | 硬 |
| reasoning_node | Forced Tool Choice（最后一轮/异常 → 强制 submit_facts） | 硬 |
| route_after_tool | 迭代硬上限 × per-intent | 硬 |
| route_after_tool | 连续空搜索 → 直接熔断 | 硬 |
| route_after_tool | 重复调用 → 直接熔断 | 硬 |
| route_after_tool | 数据充分性检查 | 硬 |
| reasoning_node | TOOL_GUIDANCE / Scene Hints / 消化态引导 | 软 |
| reasoning_node | Continuity Rules / Depth Instruction | 软 |

**Prompt 不再当警察，只当教练。** 80% 的场景 LLM 碰不到边界，边界在 API/路由层就画死了。

### Dynamic Tool Binding

**v1 决策：只做 per-intent，不做 per-round。**

工具子集按 intent 过滤：

```python
TOOLS_BY_INTENT = {
    "fetch": [
        "search_bangumi_subject", "get_subject_detail",
        "get_subject_persons", "submit_facts_to_render",
    ],
    "explore": [
        "search_bangumi_subject", "get_subject_detail",
        "get_subject_persons", "get_subject_comments",
        "get_trending_subjects", "submit_facts_to_render",
    ],
    "discuss": [
        "search_bangumi_subject", "get_subject_detail",
        "get_subject_persons", "get_subject_comments",
        "submit_facts_to_render",
    ],
    "realtime": [
        "get_calendar", "get_trending_subjects",
        "submit_facts_to_render",
    ],
    "fallback": [   # 同 fetch，保守
        "search_bangumi_subject", "get_subject_detail",
        "get_subject_persons", "submit_facts_to_render",
    ],
}
```

**为什么 v1 不加 per-round 限制**：

- **收益甚微**：迭代上限已很紧（fetch=2, explore=3），LLM 跑不远；重复调用检测和空搜索检测已覆盖主要异常模式
- **会阻断合法的两阶段搜索**：explore 场景中 LLM 需要先用 search 定位实体了解属性（"EVA"→mecha, psychological），再基于属性搜同类作品（"mecha psychological"）——这是合法行为
- **拼写修正被阻断**：用户说的作品名和 bangumi 收录名不同（"EVA"→"新世纪福音战士"），Round 1 搜空，需要换关键词重试

per-round 可作为 v2 优化方向——如果生产环境观察到"LLM 搜了又搜"的具体 pattern 且无法被现有检测兜底时再考虑。

### Forced Tool Choice

**工具选择策略**：

```python
def get_tool_choice(state, intent, iterations, max_iter):
    # 1. 外部强制信号（数据充分性 / 空搜索 / 重复调用 → route 层设的 flag）
    if state.get("_force_submit"):
        return {"type": "function", "function": {"name": "submit_facts_to_render"}}

    # 2. 最后一轮 → 强制提交，保证结构化数据不丢失
    if iterations >= max_iter:
        return {"type": "function", "function": {"name": "submit_facts_to_render"}}

    # 3. 首轮（非 chat）→ 必须调工具，消灭 deep 首轮 0 工具调用的 hack
    if iterations == 1 and intent != "chat":
        return "required"

    # 4. 正常消化轮 → LLM 自主判断
    return "auto"
```

**三个值**：

| 值 | 效果 | 使用场景 |
|----|------|---------|
| `"auto"` | LLM 可输出文本或调工具 | 正常消化轮 |
| `"required"` | LLM 必须调工具，不能输出纯文本 | 首轮（deep 模式下防止 0 工具调用） |
| `{"function": "submit_facts_to_render"}` | LLM 只能提交事实清单 | 最后一轮、异常熔断 |

**为什么最后一轮强制 submit_facts_to_render**：不这么做，LLM 可能输出 AIMessage 文本直接回复用户——绕过 submit_facts → 结构化数据（评分、排名、简介）丢失 → Render 只能用 LLM 即兴文本，数据真实性无法保证。

**为什么首轮用 "required"**：替代当前 deep 模式下的 ugly hack——首轮 0 工具调用时手动 `messages.append(HumanMessage(...))` 然后 `return {"messages": []}` 自循环重试（reasoning_node 274-298 行）。现在 API 层直接保证首轮至少调一个工具。

### 收敛：reasoning_node 核心 15 行

```python
# ── Dynamic Tool Binding ──────────────────
tool_names = TOOLS_BY_INTENT.get(intent, TOOLS_BY_INTENT["fallback"])
tools = [t for t in ALL_TOOLS if t.name in tool_names]

# ── Forced Tool Choice ────────────────────
tool_choice = get_tool_choice(state, intent, new_iterations, max_iter)

# ── LLM 调用 ──────────────────────────────
llm = create_llm().bind_tools(tools, tool_choice=tool_choice)
response = await llm.ainvoke(messages_for_llm)
```

两个纯函数决定一切 LLM 行为约束。独立可测试。

### DeepSeek v4-flash 兼容性验证

**2026-08-03 实测**：

| 配置 | `tool_choice="auto"` | `"required"` | `{"function": "..."}` |
|------|---------------------|-------------|---------------------|
| thinking ON（默认） | ✓ | ✗ "Thinking mode does not support this tool_choice" | ✗ |
| thinking OFF (`extra_body={'thinking': {'type': 'disabled'}}`) | ✓ | ✓ | ✓ |

**分类准确率**：thinking ON vs OFF 无差异（3 组 6 用例均 6/6 正确）。`tool_choice="auto"` 在 function calling 场景下已可靠——LLM 无需强制也会调用函数。

**`with_structured_output()` (Pydantic)**：不支持。DeepSeek 返回 "This response_format type is unavailable now"。分类器只能走 function calling 路径。

**决策**：

| 节点 | thinking | tool_choice | 理由 |
|------|----------|-------------|------|
| classify_node | OFF | `"auto"` | auto 已可靠；关 thinking 加快分类 |
| reasoning_node | OFF | 按策略切换 | 关 thinking 换取 forced tool_choice——API 层硬约束比 Aggregator 的 CoT 推理值钱 |
| render_node | ON（默认） | N/A（无工具） | Render 的深度推理、人格表达需要 thinking |

---

## 6. 分类器设计

### 问题

当前分类器：`max_tokens=10` 的 LLM 调用 → 输出一个单词 → 决定整个下游数据流。没有置信度、没有实体提取、默认 fallback 是 `chitchat`（最危险的误分类：用户要数据但被当成闲聊）。

尝试过的两种方案均失败：
- **正则 + LLM 两阶段**：正则太机械，"今天好累"被"今天"匹配 realtime；不可穷举
- **纯 LLM 单 token**：小马拉大车——极简判断背负巨大下游后果，错了没有兜底

### 方案：Structured Output + 置信度路由

**核心**：用 `tool_choice="required"` 强制 LLM 调用 `classify_intent` 函数——函数的参数就是分类结果。同一个 LLM、同延迟，但输出从"猜一个词"变成"带置信度的结构化判断"。

**Schema（精简到 2 字段）**：

```json
{
    "name": "classify_intent",
    "parameters": {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "enum": ["chat", "fetch", "explore", "discuss", "realtime", "fallback"]
            },
            "confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1
            }
        },
        "required": ["intent", "confidence"]
    }
}
```

两个字段，输出 ~5 tokens。和当前单 token 分类器开销持平，但多了置信度。

**为什么不做实体提取**：实体在 prompt 里作为推理引导（"先想想提到了什么作品"），不需要作为输出字段——减少 token，LLM 在内部思考实体时已获得准确率提升。

**System Prompt**：

```
你是 Bangumi 助手的查询路由器。分析用户输入，调用 classify_intent 输出分类。

意图判定：
- chat: 纯社交/感受/常识，不需要查站内数据。判定标准：没有在问具体作品信息/"你好""好累""EVA真好看（感嘆，不是查数据）""什么是三集定律（常识）"
- fetch: 查单个确定实体的属性。裸标题默认=查信息。"EVA""EVA评分""杉田智和配过什么""进击的巨人讲什么"
- explore: 多实体/无确定目标的探索。"推荐治愈番""2024最佳动画""类似日常的作品""EVA和巨人哪个好"
- discuss: 观点驱动讨论，需要站内社区内容支撑。"EVA被高估了""分析芙莉莲为什么火""以瓶子君口吻吐槽高达""你觉得EVA结局好吗"
- realtime: 时效信息查询。"今天星期几""这周新番""现在什么番最火""本季排期"
- fallback: 真的无法判断时才用

硬原则：
1. 识别到作品名/人物名 → 默认不是 chat（除非明显只是感叹）
2. chat 需要高把握，不确定时宁可走 fetch/fallback 给用户查数据
3. 裸短标题("EVA""86""K") → fetch，这是站内用户默认行为
4. "最近"看语境归类 explore，"今天/本周/当前在播" → realtime
5. "2024年"已过去 → explore，不是 realtime
```

~300 字中文，和当前 prompt 长度相当。

### 置信度路由逻辑

```python
def route_by_classification(intent, confidence, entities_present):
    if confidence >= 0.8:
        return intent           # 高置信度：直接使用

    if confidence >= 0.5:
        if intent == "chat":
            return "fetch" if entities_present else "fallback"
        if intent == "discuss":
            return "explore"    # 降级——仍然提供数据，只是少拉评论
        return intent           # fetch/explore/realtime 中置信度仍可用

    return "fallback"           # 低置信度：全部 fallback
```

**chat 是最严格的 intent，不是默认 fallback。** fetch 误分类：用户多等一轮工具调用；chat 误分类：用户在问数据却得到瞎聊——后者是不能忍的。

### Context 隔离

classifier prompt 只活在 `classify_node` 里——独立 LLM 调用，不进入 message history、不进入 aggregator prompt、不进入 render prompt。信号仅通过 state 字段传递。

**对比当前：6 个 prompt 注入点 → 1 个隔离 prompt + 1 个软引导（消化态）。**

---

## 7. 实施预览

### 目标图结构

```
                        ┌─── [chat] ────────────→ END ─── main.py render ──→
START → classify_node ──┤
                        └─── [fetch|explore|discuss|realtime|fallback]
                                   │
                                   ↓
                             reasoning_node ⇄ tool_node
                                   │
                                   └───→ END ─── main.py render ──→
```

- **classify_node**：独立 LLM 调用，structured output，输出 intent + confidence
- **route_after_classify**：chat → END；其余 → reasoning_node
- **reasoning_node**：纯 LLM 交互，Dynamic Tool Binding + Forced Tool Choice
- **route_after_tool**：控制中枢——迭代上限、空搜索、重复调用、数据充分性、submit_facts 检测
- **route_after_reasoning**：终端回复检测、deep 首轮重试
- **main.py render**：不变，无 facts 时纯人格回复

### 关键改动文件

| 文件 | 改动 |
|------|------|
| `agent/graph.py` | 新增 classify_node + route_after_classify + 条件边 |
| `agent/orchestrate/classifier.py` | structured output schema（2 字段）+ prompt 4→6 intent + 置信度路由函数 |
| `agent/orchestrate/nodes.py` | reasoning_node 瘦身：拆出 classify + 删除 6 个 prompt 注入 + Dynamic Tool Binding |
| `agent/orchestrate/guardrails.py` | `route_after_tool` 新增 per-intent 数据充分性检查、空搜索熔断、重复调用熔断 |
| `agent/state.py` | `query_intent` 类型 4→6，新增 `classifier_confidence` |
| `agent/orchestrate/prompt_builder.py` | per-intent 工具子集定义（`TOOLS_BY_INTENT`）+ tool_choice 策略（`get_tool_choice()`） |
| `agent/orchestrate/strategies.py` | scene hints 从 4→6 |
| `agent/orchestrate/deep_strategies.py` | deep scene hints 从 4→6 |
| `main.py` | chat intent 无 facts → 纯人格回复路径 |

### 不变的文件

- `tools/bgm_tools.py`：工具函数不变
- `clients/`：HTTP 客户端不变
- `memory/`：L1/L2 记忆系统不变
- `rag/`：RAG 检索管线不变
- `database/`：数据库层不变
- `agent/persona/profiles.py`：Character Card 不变
- `agent/persona/render.py`：逻辑不变（按 intent 微调后续讨论）
