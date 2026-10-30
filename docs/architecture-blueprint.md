# 宏观架构蓝图 — 一个 Companion Agent 项目应该长什么样

> 维护职责：系统边界、依赖方向与请求路径。当前产品范围和开发顺序分别由 PRODUCT、ROADMAP 维护；
> 本文引用的旧设计仅作历史参考，阅读层级见[文档地图](README.md)。本轮未逐项复验蓝图中的实现快照。

> 2026-08-16 | 文档定位：系统级“地图”。当前事实以代码为准；本文回答系统有哪些板块、当前怎样分层、一次请求怎样运行以及层间怎样协作。
> 依据：当前代码与测试；外部设计指南和项目历史只提供解释背景，不决定当前产品范围。
> 读者：未来的自己。感到 lost 时回到这里，而不是继续加功能。

---

当前本机入口：`GET /` 与 `/web/chat.js` 由 `api/local_web.py` 提供；`GET /api/readiness`
沿用 Bearer 身份校验，仅返回缺项与能力开关，不返回凭据或供应商错误正文。
网页经 `api/sessions.py` → `application/conversations.py` → 现有 `_chat`/Agent/Render 调用，
`sessions/store.py` 是 SQLite 历史事实来源。`main.py` 组装执行回调；下层不反向导入 main 或 HTTP。
持久会话不写 L1/L2，已完成对话窗口与用户确认的背景由服务端装配；旧 `/chat`/SSE 保留 L1 兼容语义。
上下文经分类、推理期 SystemMessage 和最终 Render 传递；不从模型回答自动固化用户立场。
accepted 在执行前提交；应用持有独立任务，HTTP断开不取消。状态查询和明确取消与提交分离，
回答与结局事务提交后交付；版本检查及删除后的行缺失阻止在途写回。详见[生命周期合同](Architecture/turn-lifecycle.md)。
默认关闭 RAG/L2，只有显式开启才在生命周期初始化原有数据库；L1 导入不加载 L2 数据层。
观测由中立 `core.observability`、框架 adapter、本地 OTel processor/writer 组成，
模型、工具、上下文和生命周期共享 Trace；devtools 是兼容摘要视图。
查询、限时 scope 与删除合同见[统一 Trace 维护入口](Architecture/agent-trace-design.md#13-当前维护入口2026-10-30)。
Trace 不替代会话持久化或执行恢复。
基础local-web与执行生命周期增量已完成[集成检查与收口](Test/local-web-integrated-acceptance-2026-10-31.md)，
当前问题转向对象/材料在Agent中的延续、任务判断、观点归属和表达职责，问题状态集中在ROADMAP。
工具与工作上下文整体方案仍为[暂存设计](Architecture/agent-tools-and-working-context.md)，尚未实施；
不把候选方案当作当前依赖，也不把基础harness通过当作领域质量通过。

## 0. 为什么需要这份文档

两个月独立开发的迷失不是能力问题，是缺一张"结构地图"：每个修改单独看都合理，但没有一个坐标系回答"这个功能属于系统的哪个位置、它依赖什么、被什么依赖"。叠加两个月后，结构失焦。

项目历史里最贵的教训（[`O-architecture-evolution.md`](design/O-architecture-evolution.md) 核心教训一节）：**架构假设必须与产品定位一致**。[`PRODUCT.md`](../PRODUCT.md) 是当前产品范围和取舍的唯一依据，[`ROADMAP.md`](../ROADMAP.md) 记录当前状态和近期工作；7 月愿景和历史演化文档只提供长期或历史背景，不能直接生成当前架构任务。

本文档给三样东西：

1. **板块清单**（§2）——agent 系统必备的组成，横向视图
2. **分层与接口**（§3）——依赖方向与每层契约，纵向视图
3. **当前主请求路径**（§3.3）——一次请求实际经过哪些组件，分支在哪里

**文档体系（不 lost 的最小集合）**：

| 文档 | 回答 |
|------|------|
| `PRODUCT.md` | 当前产品范围和取舍的唯一依据 |
| `ROADMAP.md` | 当前状态和近期工作 |
| **`docs/architecture-blueprint.md`（本文）** | **当前系统板块、分层、主路径与层间契约** |
| `docs/design/claude-on-bangumi-vision.md` | 历史背景与长期北极星，不是当前实现清单 |
| `docs/design/evolution-roadmap-phase7-9.md` | 历史分阶段推演；其中旧路径必须按当前代码核对 |
| `docs/design/macro-architecture-decisions.md` | 为什么选 X 而不是 Y（10 项宏观决策） |
| `docs/design/O-architecture-evolution.md` | 历史（Phase 1-10 演化与教训） |
| `AGENTS.md` + `README.md` | 开发规则与使用入口 |

---

> 2026-10-02 运行边界补充：HTTP 入口已有私有预览 Bearer 身份校验、用户级 session key 和整轮互斥。
> Pipeline L2 注入各步骤prompt；其可选路径需独立验证。旧临时聊天与活动执行仍有单进程内存状态，
> 当前网页会话历史、显式背景和轮次结局持久化在SQLite；没有图级checkpoint或半途执行恢复。
> HTTP 边界允许结构化 401/403/409/503；“工具不抛异常”不等于认证失败返回 200。

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
| 2 | **工具层** Tools | 与外部世界交互的手。契约 = 描述 + schema + 错误约定 | `tools/bgm_tools.py` 的工具注册表 | ✅ |
| 3 | **知识与检索** Knowledge/RAG | 外部知识接入（Google 的 Data Stores 类工具）；Skills 概念（可移植程序性知识："MCP 连数据，Skills 教 agent 怎么用"） | `rag/` 三通道检索（知识）+ `prompts/`（程序性知识） | ✅ |
| 4 | **上下文与记忆** Context & Memory | 策展每次推理看到的全部 token；短期在线程内、长期在外置 store | L1 / L2 / session cache（§5.3） | ✅ |
| 5 | **编排层** Orchestration | 管理整轮请求生命周期与图内 Think-Act-Observe 循环 | `main.py` 整轮编排 + `agent/graph.py` 任务内编排 | ✅ |
| 6 | **人格层** Persona | 提供角色配置，并在部分取数路径影响数据态度、在所有成功路径影响最终表达 | `agent/persona/` + Aggregator / Render 调用点 | ✅ |
| 7 | **护栏** Guardrails | 在 harness 代码里而非只靠 prompt：预算封顶、输入校验、错误格式化 | `agent/guardrails.py` + 路由熔断 | 🟡 |
| 8 | **可观测性** Observability | 统一本地 Trace 与限时诊断；执行恢复由应用负责 | `core/observability` + `agent/observability.py` | 🟡 |
| 9 | **评估** Evaluation | 官方定位为"number one thing"：golden set + CI 回归 + judge | `eval/`（现状见 `eval/README.md`）+ `docs/eval/` 方法论 | 🟡 |

🟡 = 存在但未到生产级；§5 逐板块给出方向。

**关于 Skills**（你点名的板块）：Anthropic 的 Skills 是"指令文件夹 + 渐进式披露"的运行时机制，服务于 Claude 平台系 agent。我们的 agent 是自建 FastAPI 服务，不需要 SKILL.md 运行时——但概念已经以另一种形式存在：**程序性知识在 `prompts/`（怎么用工具、怎么结束、场景提示），领域知识在 `rag/`（三通道检索）**。新增"怎么用"类知识时，落点是 prompts/ 的 prompt 模板，而不是发明新的机制。

板块 7/8/9 是**横切**的——不属于任何层，被所有层使用（§3.1）。

---

## 3. 分层与接口（纵向视图）

### 3.1 当前运行分层与调用方向

```
HTTP 服务 / 整轮编排 main.py
├─→ 任务内编排 agent/graph + nodes + routing
│   ├─→ 人格配置 persona/profiles（ReAct 的数据态度与搜索深度）
│   ├─→ 记忆 memory（模型视图裁剪与 L2 召回）
│   └─→ 数据 tools → clients / rag / database / schemas
├─→ 人格渲染 persona/render（最终用户可见表达）
└─→ 记忆 memory（SessionCache 恢复/保存与 L2 后台写入）

   横切基础能力（被多层使用，不属任何层，也不感知任何层）：
   模型工厂 agent/llm.py · 护栏 agent/guardrails.py · 观测 core.observability（devtools 为兼容视图）
   层外：评估 eval/（不参与运行时，是开发期工具）
```

人格层和记忆层不是只能由 graph 间接到达：`main.py` 会直接调用 Render、SessionCache 和 L2
写入；任务内节点会调用角色配置、短期裁剪和 L2 召回。这里画的是当前调用关系，不是目标架构。

**依赖规则**（也是自查标准）：

1. **依赖只能从调用/编排方指向被调用能力**。数据、记忆和人格模块不得反向依赖 HTTP 或 `AgentState`
2. **层间通信只走接口函数**（§3.2 清单），不直接读对方的内部状态
3. **横切基础能力无方向性**，但不得感知任何层（llm.py 不知道 graph 的存在）

### 3.2 每层对上层负责的接口（当前，锚定代码）

| 层 | 接口 | 契约 |
|---|------|------|
| 数据层 → 任务内编排 | `get_agent_tools() → list[Tool]` | 工具返回结构化 dict（**无例外**）；工具失败返回 `{"_error": ...}`，不泄露异常正文；**返回体 ≤ L1 单条上限 2000 token，超限工具由产出侧自带整块预算**（`search_local_bangumi` 的 `{total, shown, results, note}` 信封）；不感知 `AgentState` |
| 记忆层 → HTTP/Application 与任务内编排 | `manage_memory(messages, max_tokens)`；`get_memory_manager().recall_for_prompt(...)` / `remember_session(...)`；`session_cache.load/store/claim` | graph 节点通过 `helpers.recall_memory_step()` 召回并在模型调用前裁剪；`main.py` 直接负责会话互斥、L1 恢复/保存和 L2 后台写入 |
| 人格层 → HTTP/Application 与任务内编排 | `get_character(style) → CharacterProfile`；`get_agent_profile() → AgentProfile`；`render_reply(...) → str \| None` | ReAct 读取角色配置影响部分取数行为；`main.py` 直接调用 Render 生成最终用户可见表达；人格层不访问数据库 |
| 任务内编排 → HTTP/Application | `agent_app.ainvoke(state) → final_state` | graph 返回分类、工具结果和聚合状态；最终 Render、降级、记忆保存与 HTTP 响应由 `main.py` 完成 |
| HTTP/Application → 客户端 | `/chat`、`/chat/stream` | 负责认证、session 生命周期、整轮超时与错误响应；JSON/SSE 都只交付最终回复，不暴露图内中间事件 |

**如果新功能无法归属到某层的接口之内 → 先重新评审分层，再写代码。这是防 lost 的第一道闸门。**

### 3.3 当前 Agent 主路径（按一次真实请求形态追踪）

本节只描述当前代码怎样运行。它不以历史设计名词推断实现，也不评价某个模块是否应该存在。
追踪样例采用第一条产品闭环的请求形态：用户在 `message` 中提供一个 Bangumi 条目和一段相关
内容，并要求 Agent 分析和讨论。当前 API 没有独立的“条目”或“内容”字段，两者都只能进入
`ChatRequest.message`。

```text
POST /chat 或 /chat/stream
  │
  ├─ HTTP 边界：Bearer 身份绑定 → 补 session_id → 同一用户/会话整轮互斥
  │
  ├─ _build_initial_state
  │    ├─ 从 L1 SessionCache 恢复旧消息（key = [user_id, session_id]）
  │    └─ seed SystemMessage + 历史 + 本轮 HumanMessage → AgentState
  │
  ├─ LangGraph 主图
  │    └─ classify_node：独立分类模型 → 7 intent + confidence
  │         ├─ chat ───────────────────────────────────────────────┐
  │         ├─ fetch/realtime/profile（高置信度）→ 对应 Pipeline ─┤
  │         └─ explore/discuss/fallback 或低置信度 → ReAct 循环 ─┤
  │                                                              │
  │    Pipeline：固定步骤 prompt → 指定工具 → ToolNode → 总结     │
  │    ReAct：人格的数据态度/搜索深度 + L2 → 动态工具 → ToolNode ↺ │
  │                                                              │
  ├─ 图外 _render_final_reply ◀───────────────────────────────────┘
  │    ├─ chat：根据当前消息与最近可见对话生成人格回复
  │    └─ 其他：把本轮摘要、结局与有预算的来源材料交给 Character Render
  │
  ├─ 用 Render 结果替换本轮无 tool_calls 的正文消息，或追加新消息
  ├─ 保存 L1 消息缓存
  ├─ 后台将整段会话摘要 UPSERT 到 L2（实名且 MEMORY_ENABLED）
  └─ JSON reply 或只有最终 render 事件的 SSE
```

这里的**运行主干**不是任意一条 intent 分支，而是：

> HTTP 会话生命周期 → 分类 →（确定性 Pipeline 或开放式 ReAct）→ 图外统一 Render
> → L1 保存 / L2 后台写入 → HTTP 响应。

`agent_app` 只覆盖中间的“分类—取数—聚合”部分；最终回答、会话互斥、缓存恢复/保存和长期
记忆写入均由 `main.py` 包住。因此“编译后的 graph 是唯一编排层”并不完全符合当前运行事实，
更准确的边界是：**LangGraph 负责任务内编排，HTTP 服务层负责整轮编排。**

#### 3.3.1 分类后的四条实际分支

| 分支 | 进入条件 | 图内路径 | 图外收尾 |
|---|---|---|---|
| 闲聊 | 路由后的 intent 为 `chat` | `classify_node → END`，本轮不产生图内 AI 回复 | Render 接收当前消息及独立的最近可见对话，生成后更新执行状态 |
| 确定性查询 | 高置信度 `fetch` / `realtime` / `profile` | 独立子图按固定步骤调用限定工具，再由 `synthesize_node` 输出摘要 | Render 改写摘要 |
| 开放式任务 | `explore` / `discuss` / `fallback` | `reasoning_node ⇄ tool_node`；首轮强制工具，之后由模型决定继续查或输出摘要 | Render 改写摘要 |
| 低置信度安全网 | classifier confidence 低于 Pipeline 阈值 | 即使 intent 名仍可能是 Pipeline 类，也进入父图 ReAct；父图使用专用 `route_after_tool_react` | Render 改写摘要 |

Pipeline 子图挂到父图时只回传新增消息，这是 `messages: operator.add` 下防止整段历史被重复追加的
边界适配，不是新的业务分支。硬迭代上限同时适用于 Pipeline 和 ReAct。ReAct 在每次工具返回后
还会检查连续空搜索和重复工具调用；Fetch Pipeline 则在第一次搜索为空时直接跳到 synthesize。
Pipeline 的固定步骤路由会先于通用软熔断返回，因此不能概括为父图和所有子图统一经过三类熔断。
重复调用检测在用户轮次边界重置，同一会话下一轮可重新读取相同对象。

`AgentState.turn_result` 是类型化的执行结局，不是产品验收判定。Pipeline 与 ReAct 的模型异常
都返回空消息和结构化失败；HTTP 层据此跳过二次扩写。JSON 与 SSE 同时返回本轮状态，
`task_status=unverified` 明确表示工具或生成成功仍需核对回答内容。

口碑和日志原文保留在原始工具转录中；推理上下文按记录预算缩减并保留有效 JSON。Render 单独
预算社区材料，传递实际保留条数与原文截断标记。评论引文通过显式 `comment:ID` 绑定本轮来源，
未知 ID、缺作者或缺定位链接会降级为来源未核实，不靠作者名称匹配。

#### 3.3.2 动态路由、人格、RAG、Memory 在哪里

| 能力 | 决策/执行位置 | 当前实际作用 |
|---|---|---|
| 动态路由 | `classify_node`、`route_by_classification()`、`route_after_classify()` | 分类模型先产出 intent/confidence；代码先改写低置信度 intent，再决定进入 Pipeline、ReAct 或直接结束 |
| 动态工具 | `TOOLS_BY_INTENT`、Pipeline 节点的 `tool_names`、各 `ToolNode` | ReAct 按 intent 绑定工具；Pipeline 按步骤绑定更小集合；模型只可调用当前绑定的工具 |
| 人格 | `reasoning_node` 的 Aggregator prompt + `main.py` 调用的 `render_reply()` | ReAct 中 `tool_behavior` 和 `depth_taste` 影响如何取数；所有成功分支最终由 Character Card、snark、initiative 和 guardrails 决定表达 |
| RAG | `search_local_bangumi` 工具 → `rag.retriever.RagEntityRetriever` | 不是每轮自动注入的检索层；目前仅作为工具暴露给 `explore`（及旧 `discovery`）ReAct 路径，三通道返回选择用索引卡，详情仍交给 API detail 工具 |
| L1 Memory | `SessionCache` + `AgentState.messages` + `manage_memory()` | HTTP 轮次间恢复/保存消息；节点调用模型前按 token 预算生成局部裁剪视图，SystemMessage 始终重建 |
| L2 Memory | `recall_memory_step()` / `remember_session()` | Pipeline 首步或 ReAct 首轮按当前问题语义召回并注入 System prompt；Render 完成后，HTTP 层后台把会话摘要按 `(user_id, session_id)` UPSERT |

两个容易混淆的边界：本地 RAG 是**领域知识检索**，L2 是**用户跨会话记忆**；二者都用向量能力，
但数据、触发方式和注入位置不同。L1 的“缓存”保存跨 HTTP 的原消息，`manage_memory()` 的“短期
记忆管理”只裁剪单次模型调用看到的副本，不回写图状态。

#### 3.3.3 复杂但职责仍单一的地方

以下复杂度能对应到一个明确约束，先记录为现状，不归为职责冲突：

1. **父图 + 三个子图 + ReAct 双拓扑**：确定性查询使用固定步骤，开放式讨论允许模型继续取数；
   子图增量包装专门解决 reducer 重复历史问题。
2. **父图与子图使用不同的 tool 后路由**：Pipeline 需要进入确定的下一步，ReAct 只能回到
   `reasoning_node` 或结束；分开可避免低置信度 Pipeline intent 在父图跳向不存在的节点。
3. **SystemMessage 每次重建，L1 只缓存非 SystemMessage**：让最新人格、场景提示和 L2 召回
   每轮生效，同时保证系统提示不被对话截断。
4. **图外 Render 的失败分级**：有聚合摘要时可清理后直出；闲聊没有可展示的中间文本时使用
   人格兜底；工具失败/空结果另有结局话术。
5. **RAG 只返回有整体预算的索引卡**：选择候选与获取详情分工明确，并避免大结果在 L1 中被
   从 JSON 中间截断。
6. **SSE 仍以整轮为互斥单位**：当前只流最终事件，但连接取消、状态抢占和 L1 紧急保存仍需
   覆盖整个响应生命周期。

#### 3.3.4 当前职责重复或边界不清处

这里只标出“同一个决定由多处共同拥有”或“文档分层与调用事实不一致”，不提出重构方案。

| 边界 | 代码事实 | 冲突/不清晰点 |
|---|---|---|
| 整轮编排归属 | graph 管分类与取数；`main.py` 管 Render、降级、L1/L2 和最终响应 | “HTTP 只调用编排层”与现状不符；服务层本身拥有关键编排语义 |
| 置信度路由 | `route_by_classification()` 先按 0.8/0.5 改写 intent；`route_after_classify()` 再按 Pipeline 阈值判断拓扑 | “分类语义降级”和“执行拓扑降级”分属两处，最终路径不能只看 state 中的 intent 名 |
| 工具可用集合 | 全局注册表、`TOOLS_BY_INTENT`、Pipeline builder 过滤、Pipeline step `tool_names` 都描述工具边界 | 同一“此路径能调用什么”的责任在注册、节点和 prompt 配置间重复；新增/移除工具需同步核对多处 |
| 人格影响范围 | ReAct Aggregator 读取角色的数据态度/搜索深度；Pipeline 使用固定步骤 prompt；所有分支再统一 Render | 人格既被定义为思考与表达两层，又没有一致进入全部取数路径；当前“人格是否影响检索”取决于拓扑 |
| 最终消息所有权 | 图内节点先产出摘要，图外 Render 仅替换本轮无工具调用的正文；否则追加 | 同一 `messages` 仍保存内部摘要与用户回复；本轮边界和 tool_calls 配对由程序保证 |
| L1 职责 | SessionCache 管跨请求历史，`AgentState` 管本轮累积，`manage_memory()` 管模型视图裁剪 | 三者都叫短期/会话记忆，但持久范围与是否改写 state 不同；接口名称没有直接表达差异 |
| L2 调用位置 | 图内首步同步召回，图外响应前创建后台写入任务 | 读取属于节点上下文装配，写入属于 HTTP 生命周期；“记忆层接口由编排层调用”实际有两个调用方 |
| 闲聊路径 | graph 只分类后结束，Render 基于当前消息与最近可见对话作答 | `chat` 的回答生成完全在 graph 外，历史可见对话作为背景传递，不当作本轮取数事实 |
| 响应提取 | `_render_final_reply()` 已返回文本并改写 messages，`_extract_final_reply()` 随后再次从 messages 取最终回复 | 最终文本既作为显式返回值又以消息位置隐式传递，存在两套结果通道 |

另外有两处**描述漂移**：`reasoning_node` docstring 仍写“意图分类（仅首轮）”，实际分类已经由独立
`classify_node` 完成；Aggregator prompt 自称“不是 Agent Persona”，但仍读取 CharacterProfile 的
`tool_behavior` 与 `depth_taste`。它们不改变运行结果，却会干扰后续边界判断。

#### 3.3.5 第一条候选产品闭环可复用的骨架位置

下面是依据当前代码做的**可复用性映射**，不是已经确定的产品路径，也不代表这些现有能力已经
满足闭环。最终输入、来源、阶段和输出仍以待编写的“问题—选择—验收”卡片为准。

```text
用户给出条目 + 内容
  → 现有 ChatRequest.message（已有承载，但未结构化区分来源）
  → 由现有分类器决定 intent（可能进入 discuss/explore，也可能进入其他路径）
  → 若进入 ReAct，可调用现有工具获取作品事实、社区信号或本地候选
  → Aggregator 形成事实摘要/判断素材（已有）
  → Character Render 给出人格化分析并继续对话（已有）
  → 同 session 的 L1 保持讨论连续性（已有）
  → 用户形成观点后要求整理草稿（现有 ReAct + Render 可以承载，但尚未验证是否适合）
  → 返回可编辑文本（当前只有通用 reply，没有“草稿”输出契约）
```

`discuss/explore → ReAct → Render → L1 续聊` 只是基于现有组件推导出的一个候选落点，不是
已经确认的请求路径。如果产品卡片最终选择这条交互，可以先复用这些组件做验证；实际 intent、
是否需要新的状态或路径，都要由结构化输入和真实请求验收后决定。当前代码能够承载、但尚未由
产品契约固定的部分是：

- 条目、用户提供内容及其来源都混在自由文本 `message` 中，无法在状态里稳定区分“作品事实、
  社区原文、Agent 判断、用户观点”；
- `discuss` 可调用评论工具，但本地 RAG 只绑定给 `explore`，同一闭环中“作品事实”和“社区内容”
  具体走哪类来源取决于分类结果与模型选工具；
- 多轮可以依靠 L1 延续，但没有显式的“理解 → 讨论 → 草稿”阶段状态；
- 最终只有通用 `reply: str`，尚无草稿类型、引用/来源边界或“用户可继续编辑”的验收信号；
- L2 会对实名会话做摘要写入，但第一版只应保存用户明确且值得长期保留的偏好；当前是否保存由
  服务配置与会话摘要流程决定，不由这条产品闭环显式确认。

这张映射只回答“现有骨架哪里可能复用”，不回答“闭环已经怎样落地”。输入、输出、来源边界和
成功条件仍应由独立的“问题—选择—验收”卡片定义后，才能据此判断最小实现改动。

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

候选方向：实例复用与模型降级策略；是否需要推进，按当前交付流程和实测缺口决定，不视为已排期。

### 5.2 工具层 ✅

官方硬规则（Anthropic *Writing tools for agents*）对照：

- ✅ **结构化错误返回**：我们的 `{"_error": ...}` 约定与官方"错误以 tool result 返回、不抛异常"一致
- ✅ **命名空间化**：`search_bangumi_subject`、`get_subject_*` 等按领域命名
- ✅ **search 优先 + token 上限**：检索型工具为主；L1 单条消息 2000 tok 截断兜底
- ⚠️ **描述质量**：官方要求 ≥3-4 句、写明"何时用**以及何时不用**"、错误文案指路（"告诉 agent 下一步做什么"而非诊断）——现状 docstring 偏简，错误文案偏诊断
- ⚠️ **`input_examples`**（复杂工具 1-5 个真实示例；官方评测复杂参数准确率 72%→90%）——未使用

方向：用 eval 数据迭代工具描述（官方方法论：把失败 transcript 反馈回工具重写描述）。新增工具的入口是 `schemas/tools_input.py` + `get_agent_tools()` 注册。

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

候选方向：LangGraph checkpointer用于图级断点续跑、回放等能力；当前未采用。
已有应用层轮次生命周期及SQLite会话恢复只恢复已完成对话、标明中断，不自动续跑半途工具。
是否引入checkpoint必须由具体任务需求证明，不作为基础harness收口后的默认下一步。

### 5.5 人格层 ✅（本项目特有板块）

官方指南没有这个板块——它是 companion 定位的差异化层，也是本项目最独特的资产。两层管线（Character Card 决定思考 / Render Node 决定表达）+ 参数化（`snark` 5 档 / `depth_taste` / `initiative`）与 vision 的"可调人格"方向一致。

扩展纪律：新人格 = 新 `CharacterProfile` + `CHARACTER_REGISTRY` 注册，**不复制编排逻辑**；改 Card 必须同步检查 Render（反之亦然）。

### 5.6 护栏 🟡

官方立场（Anthropic / OpenAI / Google 一致）：**护栏在 harness 代码里，不能只靠 prompt**；分层防御（LLM 分类器 + 规则 + moderation）。

现状：`guardrails.py`（XML 泄漏剥离、重复调用检测、错误格式化）+ 迭代上限/空搜索熔断 + `BaseClient` 指数退避重试。companion 是**只读 agent**（无支付/删除类动作），不需要审批点（human-in-the-loop），风险面主要在输出侧。

方向：输出护栏——**记忆内容泄漏防护**（用户评分与观看历史是对话的一部分，不是公共数据，vision 隐私原则）、日志脱敏。

### 5.7 可观测性 🟡

现状：应用持有执行身份，中立 OTel 接口与框架 adapter 将模型、工具、材料选择、Render 和提交连接到本地 Trace。
默认元数据，显式限时会话 scope 才采正文；日志带关联标识，旧 telemetry 从同一跨度派生。
故障、保留和删除见[维护合同](Architecture/agent-trace-design.md#13-当前维护入口2026-10-30)。
真实供应商边界仍待授权验证；Trace 不提供自动重试或 checkpoint 恢复。

### 5.8 评估 🟡（P0 级工程债）

官方定位：Google 白皮书称 eval 为 **"number one thing"且不可协商**；Anthropic："You cannot ship an agent without evals"。标准做法：golden set 30–100 条、**断言行为而非精确输出**、每次 prompt 变更跑 CI、LLM-as-judge 多准则打分、人工评测不可替代（Anthropic 发现人工评测能捕捉 LLM-judge 抓不到的偏差）。

现状：评测资产已归拢到根 `eval/` 一处（分类器 / RAG 检索 / 回复质量 / 记忆四条轴 + 一道图接线门禁，详见 `eval/README.md`），`docs/eval/` 是计划书，但评测**不在开发闭环里**——人格/提示词改动不强制跑评测。

维护方向：围绕当前固定场景保留可重复输入、真实Trace和人工判据；Prompt改动必须有相应回归证据。
历史`docs/eval/e2e-scenario-testing.md`中的更大场景集与自动评判是候选，不自动成为当前任务。
评测扩张及人格重设计是否启动，以ROADMAP和对应产品问题为准。

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

历史分阶段推演见 [`evolution-roadmap-phase7-9.md`](design/evolution-roadmap-phase7-9.md)；其中路径和假设必须按当前代码与产品基线重新核对，不能直接作为开发任务。

**地图使用规则**：每个新功能先在这张表上定位——属于哪个板块、动哪层、加什么接口。定位不进去的需求 = 重新评审地图，而不是硬塞进现有代码。这条规则就是"不 lost"的定义。

---

## 7. 不 lost 的操作清单

**新功能开工前四问**（任何一个答不出就停下来想，而不是开始写）：

1. 它属于 9 个板块里的哪个？（§2）
2. 它放在哪一层、通过哪个接口对外服务？（§3.2）
3. 它依赖哪些层/横切能力？有没有向上的箭头？（§3.1）
4. 它在演化地图（§6）上有位置吗？没有的话，地图缺什么？

**变更完成后三同步**：

1. 本蓝图（如果板块或接口变了）
2. `PRODUCT.md`（如果产品边界变了）或 `ROADMAP.md`（如果当前顺序变了）
3. 必要的独立决策记录（只有出现新的长期选择时才增加）

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
- [ ] 工具返回了非 dict？
- [ ] 工具抛了异常而不是返回 `{"_error": ...}`？

开发流程：

- [ ] 新功能在 9 板块清单里找不到位置？
- [ ] 新功能在演化地图（§6）里找不到位置？
- [ ] 改了 prompt 但没跑任何评测？
- [ ] 新决策没有写进 `docs/design/`？
