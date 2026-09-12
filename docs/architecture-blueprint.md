# 宏观架构蓝图 — 一个 Companion Agent 项目应该长什么样

> 2026-08-16 | 文档定位：系统级"地图"。回答三个问题——agent 项目由哪些板块组成（§2）、板块之间如何分层与互相依赖（§3）、我们的项目现在在哪、往哪走（§4-6）。
> 依据：Anthropic / OpenAI / Google / LangChain 官方设计指南（附录 A）+ 本项目代码现状（v0.1.1）+ 项目历史教训。
> 读者：未来的自己。感到 lost 时回到这里，而不是继续加功能。

---

## 0. 为什么需要这份文档

两个月独立开发的迷失不是能力问题，是缺一张"结构地图"：每个修改单独看都合理，但没有一个坐标系回答"这个功能属于系统的哪个位置、它依赖什么、被什么依赖"。叠加两个月后，结构失焦。

项目历史里最贵的教训（[`O-architecture-evolution.md`](design/O-architecture-evolution.md) 核心教训一节）：**架构假设必须与产品定位一致**。Tool Agent 的架构 + Companion Agent 的定位，造成了四个 Phase 的返工。所以本蓝图的第一锚点是产品愿景（[`claude-on-bangumi-vision.md`](design/claude-on-bangumi-vision.md)），第二锚点才是结构。

本文档给三样东西：

1. **板块清单**（§2）——agent 系统必备的组成，横向视图
2. **分层与接口**（§3）——依赖方向与每层契约，纵向视图
3. **演化地图**（§6）——从现状到愿景的路径，每个新功能的定位法

**文档体系（不 lost 的最小集合）**：

| 文档 | 回答 |
|------|------|
| `docs/design/claude-on-bangumi-vision.md` | 为什么做（产品定位） |
| **`docs/architecture-blueprint.md`（本文）** | **怎么搭（宏观结构）** |
| `docs/design/evolution-roadmap-phase7-9.md` | 分阶段怎么走到愿景（注意：部分旧路径需按 CLAUDE.md 文件地图换算） |
| `docs/design/macro-architecture-decisions.md` | 为什么选 X 而不是 Y（10 项宏观决策） |
| `docs/design/O-architecture-evolution.md` | 历史（Phase 1-10 演化与教训） |
| `CLAUDE.md` + `README.md` | 现状锚点（以代码为准） |
| `ROADMAP.md` | 近期任务清单（待解决 P0/P1/P2） |

---

## 1. 你的直觉是对的：板块 × 分层是两个正交视角

你提出的两种形式——"每层只留接口对上层负责"与"记忆/function calling/llm/编排/skills 等必备板块"——正是业界通用的两个正交视角，缺一不可：

- **板块（横向）**：回答"一个 agent 必须具备什么能力"。与官方指南的组成划分对应（§2）
- **分层（纵向）**：回答"谁依赖谁、谁对谁负责"。依赖方向不可逆（§3）

板块告诉你功能该放进哪个盒子；分层告诉你盒子之间的箭头只能朝一个方向。**Lost 的本质就是：写代码时既没有盒子（板块），也没有箭头（分层），两个月后所有东西缠成一团。**

---

## 2. 板块清单（横向视图）

官方共识（附录 A 来源）：一个可生产的 agent 系统 = **模型 + 工具 + 上下文/记忆 + 编排 + 护栏 + 评估**（Anthropic 官方指南反复使用的板块划分）；Google 白皮书简化为 **Agent = Model + Tools + Orchestration Layer**；LangGraph 把记忆外置为独立 store 并与图状态分离。加上本项目特有的**人格**板块（companion 定位的核心竞争力），共 **9 个板块**：

| # | 板块 | 职责（官方定义） | 我们的对应 | 状态 |
|---|------|----------------|-----------|------|
| 1 | **模型层** Model | 推理、规划、工具选择。选型标准是"可靠的工具使用与多步推理"，而非 benchmark 分数 | `agent/llm.py` `create_llm()` 工厂（DeepSeek/OpenAI 兼容） | ✅ |
| 2 | **工具层** Tools | 与外部世界交互的手。契约 = 描述 + schema + 错误约定 | `tools/bgm_tools.py` 16 个 @tool | ✅ |
| 3 | **知识与检索** Knowledge/RAG | 外部知识接入（Google 的 Data Stores 类工具）；Skills 概念（可移植程序性知识："MCP 连数据，Skills 教 agent 怎么用"） | `rag/` 三通道检索（知识）+ `prompts/`（程序性知识） | ✅ |
| 4 | **上下文与记忆** Context & Memory | 策展每次推理看到的全部 token；短期在线程内、长期在外置 store | L1 / L2 / session cache（§5.3） | ✅ |
| 5 | **编排层** Orchestration | 管理 Think-Act-Observe 循环与上下文窗口；"engineer owns the graph" vs "model owns the graph" | `agent/graph.py` 异质拓扑 | ✅ |
| 6 | **人格层** Persona | 本项目特有：决定 agent 怎么思考（Card）与怎么表达（Render） | `agent/persona/` 两层管线 | ✅ |
| 7 | **护栏** Guardrails | 在 harness 代码里而非只靠 prompt：预算封顶、输入校验、错误格式化 | `agent/guardrails.py` + 路由熔断 | 🟡 |
| 8 | **可观测性** Observability | 全量 trace、checkpoint 持久执行、失败可续跑 | `agent/devtools.py`（DEV_MODE telemetry） | 🟡 |
| 9 | **评估** Evaluation | 官方定位为"number one thing"：golden set + CI 回归 + judge | `eval/` + `rag/eval/` + `docs/Eval/` 方法论 | 🟡 |

🟡 = 存在但未到生产级；§5 逐板块给出方向。

**关于 Skills**（你点名的板块）：Anthropic 的 Skills 是"指令文件夹 + 渐进式披露"的运行时机制，服务于 Claude 平台系 agent。我们的 agent 是自建 FastAPI 服务，不需要 SKILL.md 运行时——但概念已经以另一种形式存在：**程序性知识在 `prompts/`（怎么用工具、怎么结束、场景提示），领域知识在 `rag/`（三通道检索）**。新增"怎么用"类知识时，落点是 prompts/ 的 prompt 模板，而不是发明新的机制。

板块 7/8/9 是**横切**的——不属于任何层，被所有层使用（§3.1）。

---

## 3. 分层与接口（纵向视图）

### 3.1 我们的四层 + 依赖方向

```
                       HTTP 入口 (main.py: /chat /chat/stream)
                                  │
              ┌───────────────────▼────────────────────┐
              │  编排层 Orchestration                   │ 唯一被 HTTP 调用的层
              │  graph / nodes / routing / prompts /    │ 对外接口：编译后的 agent_app
              │  config / state / helpers               │
              └───┬─────────────┬──────────────┬────────┘
                  │             │              │
        ┌─────────▼───┐  ┌──────▼───────┐  ┌───▼──────────────────┐
        │ 人格层       │  │ 记忆层        │  │ 数据层                │
        │ persona/    │  │ memory/      │  │ tools / clients /     │
        │ Card+Render │  │ L1/L2/Cache  │  │ rag / database /      │
        └─────────────┘  └──────────────┘  │ schemas               │
                                           └───────────────────────┘

   横切基础能力（被多层使用，不属任何层，也不感知任何层）：
   模型工厂 agent/llm.py · 护栏 agent/guardrails.py · 观测 agent/devtools.py
   层外：评估 eval/（不参与运行时，是开发期工具）
```

（说明：人格层同时被编排层（reasoning_node 装配 Character Card）与 HTTP 入口（main.py render 后处理）调用——两层管线的下半段在 main.py。这不违反分层：调用方始终在上方。）

**依赖规则**（也是自查标准）：

1. **箭头只能向上**。任何模块 import 了更高层的东西 = 违规
2. **层间通信只走接口函数**（§3.2 清单），不直接读对方的内部状态
3. **横切基础能力无方向性**，但不得感知任何层（llm.py 不知道 graph 的存在）

### 3.2 每层对上层负责的接口（当前，锚定代码）

| 层 | 接口 | 契约 |
|---|------|------|
| 数据层 → 上 | `get_agent_tools() → list[Tool]` | 工具返回结构化 dict（唯一例外 `search_local_bangumi` 返回 str）；失败返回 `{"_error": ...}`，绝不抛异常；不感知 `AgentState` |
| 记忆层 → 上 | `manage_memory(messages, max_tokens)`；`get_memory_manager().recall_for_prompt(...)` / `remember_session(...)`；`session_cache.load/store` | 召回结果是一段可注入 prompt 的文本；记忆层不直接调用 Bangumi API；编排层通过 `helpers.recall_memory_step()` 适配 |
| 人格层 → 上 | `get_character(style) → CharacterProfile`；`get_agent_profile() → AgentProfile`；`render_reply(...) → str \| None` | 人格层不访问数据库；Card 与 Render 必须同步修改 |
| 编排层 → 上 | `agent_app.ainvoke(state) → final_state` | 唯一消费者是 main.py；state 是 TypedDict + 字段 reducer |

**如果新功能无法归属到某层的接口之内 → 先重新评审分层，再写代码。这是防 lost 的第一道闸门。**

---

## 4. 编排模式库与我们的位置

官方核心决策轴（Anthropic）：**engineer owns the graph（workflow）vs model owns the graph（agent）**。workflow 可预测、成本有界、可审计；agent 适合开放式问题，但失败模式难推理、成本可能失控。总原则：**"从最简单开始"**——能确定的逻辑放代码里，不要让 LLM 决定。

| 模式 | 图归谁 | 适用场景 | 本项目 |
|------|-------|---------|--------|
| **Prompt Chaining** | 工程师 | 线性步骤可枚举，可在步骤间加程序化质量门 | ✅ pipeline subgraph（步骤顺序编译时确定，每步含 LLM 决策） |
| **Routing** | 工程师 | 按类别分发到不同下游 | ✅ classify_node 7 intent + 置信度路由（<0.7 兜底 ReAct） |
| **Parallelization** | 工程师 | 分块 / 投票并发 | ⚠️ 工具级并行调用有，节点级无 |
| **Orchestrator-Workers** | 模型 | 动态拆解子任务派给专职 worker | ❌ 候选：vision 的社区参与场景 |
| **Evaluator-Optimizer** | 模型 | 生成-评估反馈循环 | ❌ 曾用 Critic 近似实现，已移除（增加延迟而无质量证据） |
| **Autonomous Agent** | 模型 | 开放式探索，任务长度可变 | ✅ ReAct 循环（explore/discuss/fallback） |

**我们的异质拓扑 = Routing + Chaining + Agent 的合法组合**——不是发明，是官方模式库的叠加。新增编排形态前，先在这个表里找到它；找不到，要么复用现有模式，要么写一篇设计决策（`docs/design/R-*` 传统）。

### 4.1 多 agent 什么时候引入（答案：现在不引入）

官方标准（Anthropic multi-agent 系统文章 + LangGraph 指南）：多 agent 只为三个可指名的理由引入——**specialization（专才分工）/ context isolation（上下文隔离）/ parallelism（并行）**——且必须先有单 agent 基线的失败证据。代价：Anthropic 实测 token 约为 chat 的 15×，社区共识多 agent 成本可达单 agent 5–20×。

我们的现状：**单一 Companion Agent 是正确的选择**（历史 Phase 6 的合并就是向这个方向收敛）。未来 vision 的**社区参与**（讨论帖场景）是唯一可能触发 orchestrator-worker 的场景——届时必须遵守 Anthropic 系统的关键协议（worker 回传压缩摘要而非完整 transcript），并先用单 agent 证明失败模式存在。

反模式警告：**multi-agent theater**——为分而分、无真实隔离/并行需求。不引入的理由永远是"没有证据表明单 agent 不够"。

---

## 5. 板块深度指南：官方标准 vs 我们的现状与方向

### 5.1 模型层 ✅

现状：`create_llm()` 单工厂，DeepSeek function-calling，`LLM_TEMPERATURE=0.3`，各节点独立实例。选型论证见 [`macro-architecture-decisions.md`](design/macro-architecture-decisions.md)（18× 成本差换取 95% 质量）。

方向：实例复用（当前每次调用新建 client，ROADMAP P2 已记录）；模型降级策略（主模型失败切备选）。

### 5.2 工具层 ✅

官方硬规则（Anthropic *Writing tools for agents*）对照：

- ✅ **结构化错误返回**：我们的 `{"_error": ...}` 约定与官方"错误以 tool result 返回、不抛异常"一致
- ✅ **命名空间化**：`search_bangumi_subject`、`get_subject_*` 等按领域命名
- ✅ **search 优先 + token 上限**：检索型工具为主；L1 单条消息 2000 tok 截断兜底
- ⚠️ **描述质量**：官方要求 ≥3-4 句、写明"何时用**以及何时不用**"、错误文案指路（"告诉 agent 下一步做什么"而非诊断）——现状 docstring 偏简，错误文案偏诊断
- ⚠️ **`input_examples`**（复杂工具 1-5 个真实示例；官方评测复杂参数准确率 72%→90%）——未使用

方向：用 eval 数据迭代工具描述（官方方法论：把失败 transcript 反馈回工具重写描述）。16 个工具的规模健康——OpenAI 指南观点：15+ 个定义清晰的工具优于 10 个以下的重叠工具；新增工具的入口是 `schemas/tools_input.py` + `get_agent_tools()` 注册。

### 5.3 上下文与记忆 ✅（方向明确）

官方定义（Anthropic *Effective context engineering*）：context engineering = **策展每次推理看到的全部 token**（system prompt、工具 schema、检索数据、历史工具结果）。Context window 是**有限注意力预算**而非免费存储——填满陈旧工具输出会导致 context rot（准确率下降），只保留"让模型正确行动的最小高信号 token 集"。

五项官方技术对照我们的现状：

| 技术 | 我们 |
|------|------|
| **System prompt 校准**（分区、Goldilocks 区） | ✅ 人格两层管线 + scene hints + TOOL_GUIDANCE 分区清晰 |
| **Compaction**（摘要化 + 保留关键信息） | ✅ L1 工具结果压缩 + 滑动窗口 |
| **Context retrieval**（检索注入） | ✅ L2 recall-before-reason（推理前召回注入 system prompt）+ RAG |
| **Structured note-taking**（跨轮持久笔记） | ⚠️ 无——vision 的"叙事性回忆"正是此项的 companion 化形态 |
| **Prompt 优化** | ✅ 持续迭代中 |

记忆体系对官方模型的映射（LangGraph 记忆官方文档：短期在线程内由 state+checkpointer 管理；长期在线程外独立 store，namespace 隔离；**长期记忆永不进 graph state**）：

| 官方分类 | 我们 | 状态 |
|---------|------|------|
| 短期（thread 内） | L1 滑动窗口 + session cache（跨 HTTP 的 thread 等价物） | ✅ |
| 长期（thread 外，独立 store） | L2 pgvector 语义召回 + 时间衰减（半衰期 14 天） | ✅ 架构正确 |
| 写入 hot path（同步）vs background（异步） | fire-and-forget 后台写入 | ✅ 生产标配做法 |
| 显式"记住这个"同步快路径 | 无 | 方向 |
| 周期性 consolidation（去重、淘汰过时） | 无 | 方向 |
| 记忆类型 semantic / episodic / procedural | 当前只有语义型 session 摘要 | **方向：episodic 是 companion 的关键增量**（"你说过 EVA 是神作"而非"用户偏好机战"） |

### 5.4 编排层 ✅

原则（Anthropic）：**把逻辑从 prompt 移到代码**——能确定的事情不要让 LLM 决定。我们的 pipeline subgraph（编译时步骤 + 运行时工具路由）正是这条原则的产物；路由熔断（硬迭代上限 / 连续 2 次空搜索 / 重复调用检测）是"engineer owns the graph"的护栏面。

方向：引入 LangGraph checkpointer（持久执行）——官方把 super-step 级 checkpoint 定位为可靠性底座（失败续跑、可回放、可 fork）；当前我们只有 `recursion_limit=50` + 应用层 session cache，没有图级断点。

### 5.5 人格层 ✅（本项目特有板块）

官方指南没有这个板块——它是 companion 定位的差异化层，也是本项目最独特的资产。两层管线（Character Card 决定思考 / Render Node 决定表达）+ 参数化（`snark` 5 档 / `depth_taste` / `initiative`）与 vision 的"可调人格"方向一致。

扩展纪律：新人格 = 新 `CharacterProfile` + `CHARACTER_REGISTRY` 注册，**不复制编排逻辑**；改 Card 必须同步检查 Render（反之亦然）。

### 5.6 护栏 🟡

官方立场（Anthropic / OpenAI / Google 一致）：**护栏在 harness 代码里，不能只靠 prompt**；分层防御（LLM 分类器 + 规则 + moderation）。

现状：`guardrails.py`（XML 泄漏剥离、重复调用检测、错误格式化）+ 迭代上限/空搜索熔断 + `BaseClient` 指数退避重试。companion 是**只读 agent**（无支付/删除类动作），不需要审批点（human-in-the-loop），风险面主要在输出侧。

方向：输出护栏——**记忆内容泄漏防护**（用户评分与观看历史是对话的一部分，不是公共数据，vision 隐私原则）、日志脱敏。

### 5.7 可观测性 🟡

现状：`DEV_MODE=true` telemetry（节点耗时 + token 统计），够开发态。

方向：全量 trace（官方"最小栈" = 每轮请求 trace 到可观测平台）；checkpoint 持久执行（§5.4）；生产日志规范。

### 5.8 评估 🟡（P0 级工程债）

官方定位：Google 白皮书称 eval 为 **"number one thing"且不可协商**；Anthropic："You cannot ship an agent without evals"。标准做法：golden set 30–100 条、**断言行为而非精确输出**、每次 prompt 变更跑 CI、LLM-as-judge 多准则打分、人工评测不可替代（Anthropic 发现人工评测能捕捉 LLM-judge 抓不到的偏差）。

现状：三套评估资产（根 `eval/` 分类器与 RAG 离线评测、`rag/eval/` 检索指标、`docs/Eval/` 方法论），但**不在开发闭环里**——人格/提示词改动不强制跑评测。

方向（下一步就该做）：把人格/端到端场景评测常驻化——20-30 条场景 golden set（`docs/Eval/e2e-scenario-testing.md` 已有设计），snark 风格 / 字数 / 意图分类三类行为断言，prompt 变更后一键回归。**评测常驻之前，人格层改动都应该视为高风险操作。**

---

## 6. 演化地图：从现状到愿景

愿景（vision 第六节）相对现状的增量，落到板块与接口上：

| 愿景增量 | 触及板块 | 新接口（示例） | 不破坏的边界 |
|---------|---------|---------------|-------------|
| **被动触发**（感知用户标记/浏览） | 编排层 | `context` 参数（`context.type` / `trigger` / `user_action`） | 现有 pipeline/ReAct 拓扑不动 |
| **页面语境**（知道用户在看什么） | 编排 + 上下文 | context 注入 prompt 的装配点 | 人格两层管线不动 |
| **社区参与**（讨论帖发言） | 编排 + 人格 | 公开人格 = 私聊人格的独立 CharacterProfile | 数据层工具复用（`get_entity_comments` 已存在） |
| **可调人格**（多维参数） | 人格层 | CharacterProfile 加维度（现有 `snark` 范式） | Render 管线不动 |
| **叙事性记忆**（"记得你说过"） | 记忆层 | L2 `memory_type` 扩展 + 召回注入格式 | `recall_memory_step` 签名不动 |

分阶段推演见 [`evolution-roadmap-phase7-9.md`](design/evolution-roadmap-phase7-9.md)（其中部分旧路径需按 CLAUDE.md 文件地图换算）。**所有 Phase 7 级改动都是增量式的——加字段、加文件、加参数，reasoning → tool → render 管线不动**。

**地图使用规则**：每个新功能先在这张表上定位——属于哪个板块、动哪层、加什么接口。定位不进去的需求 = 重新评审地图，而不是硬塞进现有代码。这条规则就是"不 lost"的定义。

---

## 7. 不 lost 的操作清单

**新功能开工前四问**（任何一个答不出就停下来想，而不是开始写）：

1. 它属于 9 个板块里的哪个？（§2）
2. 它放在哪一层、通过哪个接口对外服务？（§3.2）
3. 它依赖哪些层/横切能力？有没有向上的箭头？（§3.1）
4. 它在演化地图（§6）上有位置吗？没有的话，地图缺什么？

**变更完成后三同步**：

1. `CLAUDE.md` 文件地图/架构节（现状锚点）
2. 本蓝图（如果板块或接口变了）
3. `docs/design/` 设计决策（如果是新选择，沿用 `R-*` 传统）

**节奏纪律**：

- 每个 Phase 结束时做一次 30 分钟架构 review：对照 §3.1 检查所有 import 方向
- 评测常驻之后再动人格层（§5.8）
- 感到 lost 时：回到 §0 的文档体系表，而不是继续加功能

---

## 附录 A：参考来源（官方指南）

| 来源 | 对蓝图最有价值的要点 | URL |
|------|---------------------|-----|
| Anthropic · Building effective agents | workflow vs agent 决策轴、"从最简单开始"、编排模式清单 | https://www.anthropic.com/engineering/building-effective-agents |
| Anthropic · How to build agents with the Claude API | 裸循环起步、上下文管理、护栏在 harness、evals 是生产门槛 | https://www.anthropic.com/engineering/how-to-build-agents-with-the-claude-api |
| Anthropic · Multi-agent research system | orchestrator-worker + 压缩摘要协议、~15× token、何时才值得多 agent | https://www.anthropic.com/engineering/built-multi-agent-research-system |
| Anthropic · Effective context engineering | 注意力预算、五项上下文技术、context rot | https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents |
| Anthropic · Writing tools for agents | 描述=入职文档、错误文案指路、input_examples | https://www.anthropic.com/engineering/writing-tools-for-agents |
| Anthropic · Agent Skills best practices | Skills/工具/subagent 三者分工、渐进式披露 | https://docs.claude.com/en/docs/agents-and-tools/agent-skills/best-practices |
| LangGraph · Concepts（memory / multi_agent / durable_execution） | 记忆两级制、semantic/episodic/procedural、拓扑四形态、checkpoint 持久执行 | https://langchain-ai.github.io/langgraph/concepts/ |
| OpenAI · A practical guide to building agents | 单 agent 优先、Manager/Decentralized 两种多 agent 模式、工具三分类 | https://cdn.openai.com/business-guides-and-resources/a-practical-guide-to-building-agents.pdf |
| Google · Agents 白皮书 | Agent = Model + Tools + Orchestration、成熟度 Level 0-4、eval "number one thing" | https://www.kaggle.com/whitepaper-agents |

> 引用说明：本文档对官方材料的引用经 WebSearch 转述核实（2026-08-16 网络环境无法直接抓取原文页）。关键数据点（15× token 等）出自官方博客原文口径；个别社区共识数据（如"单 agent 适合 80% 场景"）未采用。正式对外引用前建议核对原文。

## 附录 B：分层违规自查清单

运行时代码：

- [ ] 任何 `tools/` `clients/` `rag/` `database/` 模块 import 了 `AgentState` 或 `agent.state`？
- [ ] 记忆层直接调用了 Bangumi API？（应经 `clients/`）
- [ ] 人格层访问了数据库？
- [ ] 修改了 `profiles.py` 的 Character Card 但没检查 `render.py`？
- [ ] 新节点绕过 `get_character()` / `get_agent_profile()` 自己拼人格文本？
- [ ] 工具返回了非 dict（除 `search_local_bangumi`）？
- [ ] 工具抛了异常而不是返回 `{"_error": ...}`？

开发流程：

- [ ] 新功能在 9 板块清单里找不到位置？
- [ ] 新功能在演化地图（§6）里找不到位置？
- [ ] 改了 prompt 但没跑任何评测？
- [ ] 新决策没有写进 `docs/design/`？
