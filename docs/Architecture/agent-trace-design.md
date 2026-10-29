# Agent Trace 统一设计与实施计划

> 状态：实现合同及当前维护入口，2026-10-30；离线与隔离启动验证见第 13 节。未据此宣称真实模型质量通过，不包含回复策略或 Render 拓扑调整。
> 范围：本机单用户、单应用进程。为 [C1–C5](../product/fixed-agent-scenarios.md) 的质量定位提供正式观测入口。
> 本文将“减低透明度”按讨论上下文解释为“降低黑箱程度、减少定位盲区”；不等于无条件保存敏感全文。

## 1. 交付目标与设计原则

给出一次轮次标识，用户/开发者应能取得一条完整、可关联、可说明缺口的执行记录，回答：

1. 本轮使用了什么历史、背景和引用材料？什么被裁剪或未提供？
2. 每次模型调用实际收到什么准备消息、哪些工具定义，返回什么可见输出或错误？
3. 模型提出什么工具调用？校验是否通过？实际执行和清洗后的返回分别是什么？
4. 资料经过选择、摘要、窗口和最终表达后，哪些身份、原文和限定仍然存在？
5. 结果在哪一步改变、失败或未保存？各阶段耗时和 token 消耗是多少？

Trace 提供观察证据，不自动证明外部事实正确，也不能仅凭一次轨迹证明某句 Prompt 是原因。
不记录模型隐藏推理，不把材料包含某个词当作模型理解或用户立场保持的证明。

原则：一个身份体系、一个事件格式、一个落盘入口、一个查询接口；业务事实仍由 sessions 管理。
日志、耗时、token 和详细内容具有不同用途，以统一 trace 关联，不能拼成互不关联的四套系统。
依赖方向与开工四问：本次涉及运行时/上下文/工具/验证板块；在调用 seam 上观察；
业务模块只依赖中立观测 Interface，观测 Implementation 不导入 AgentState、graph、main、HTTP 或会话存储。

## 2. 现有资产与真实缺口

依据：[devtools](../../agent/devtools.py)、[模型工厂](../../agent/llm.py)、[主入口](../../main.py)、
[运行时](../../application/conversations.py)、[临时观察脚本](../../scripts/trace_fixed_scenarios.py)、
[单次真实记录](../Test/c1-c5-real-trace-2026-10-29-run1.md)。

| 当前资产 | 已有价值 | 需要归并的缺口 |
|---|---|---|
| RequestTelemetry、LLMCallRecord、ToolCallRecord、NodeTiming | 调用统计、工具状态和脱敏参数名 | 缺统一父子关联、落盘及内容来源；节点耗时目前按更新间隔估计，不等于准确节点执行跨度 |
| TelemetryLLMWrapper / _telemetry_label | 模型 token 与标签 | 分类不走同样标签；不同调用点与绑定对象可能漏记录；失败调用没有完整结局 |
| instrument_tools | 工具计时与结构化错误判断 | 嵌套包装可能重复计数；参数校验前后的 seam 不完整；不能解释内容裁剪 |
| DEV_MODE + _run_with_telemetry | 开发摘要与图更新计时 | 开关改变 ainvoke/astream 路径并自行累积 state；不能作为长期观测方式 |
| 生命周期日志与持久轮次 | 请求ID、状态、原因、用户可见结局 | 不是完整模型/工具轨迹，缺模型输入及阶段关联 |
| trace_fixed_scenarios.py | 成对模型/工具输入输出、固定题目与冻结文件 | 临时 monkey-patch、阶段 unknown、同步文件写入；不能继续作为正式实现 |

已有脱敏、工具结果判断与 usage 读取能力可复用；不能批量删除现有测试或兼容入口。
“代码已有两个月”不作为淘汰依据，是否形成重复事实来源和定位盲区才是取舍依据。

## 3. Trace、日志、指标和快照各自负责什么

| 类型 | 职责与存放 | 不承担什么 |
|---|---|---|
| Trace / span / event | 一轮执行及其阶段的关联、时间、状态和程序决策；本地结构化库 | 不以执行成功推断回答正确 |
| 日志 | 运维事件和问题摘要，带 trace/span/request 关联 | 不再额外复制完整 Prompt、工具正文或另算 token |
| Metrics / summary | 从唯一模型/工具 span 聚合调用数、延迟、失败与 usage | 不建立第二套可写计数事实来源，不把会话ID作为聚合标签 |
| 内容快照 artifact | 详细诊断时的实际消息、绑定工具、原文与返回；私有文件 | 不默认保存所有用户全文，不把摘要标成完整内容 |
| 会话历史/来源 | 用户看到的对话、确认背景及依据，继续由 sessions 持久化 | 不能用 trace 代替恢复或反过来把历史冒充 trace |
| 质量标注 | 后续人工/规则的判定，引用 trace/span/artifact | 不由 span 状态或 token 数量自动生成质量分数 |

不要求独立部署日志、指标或追踪服务；首版从本地同一记录生成摘要与诊断导出即可。

## 4. 必要 Module 与依赖组织

拟定布局，只在对应增量实现时创建实际文件，不预建空包：

```text
main.py                     初始化、启停观测运行时；组装配置与存储位置
api/observability.py        身份/归属校验、诊断查询和脱敏导出
application/                轮次/尝试身份、执行跨度、持久提交与生命周期事件
agent/observability.py      LangChain 消息/usage 转换、模型 Callback、ToolNode 调用 Adapter
agent/nodes/、persona/      明确阶段 span；上下文准备及选择结果的观察点
core/observability/         统一 Interface；context、policy、schema、local processor/store 的内聚 Implementation
```

允许的依赖：

```text
main → api / application / agent → core.observability → 标准库 / OpenTelemetry
clients、tools、memory ───────────→ core.observability
```

- core.observability 接受普通结构化数据，不导入 LangChain、AgentState、HTTP 或业务存储。
- agent/observability 是 SDK Adapter，不创建第二套模型工厂、执行循环或错误处理策略。
- API 层从已认证身份构造查询归属；不能只凭 trace_id 访问其他用户记录。
- Span 与任务 Context 由应用层建立，ContextVar 只传播中立身份；不把观测状态塞进模型对话。
- 导入无磁盘/网络访问；应用显式 start 后才打开本地存储和后台写入，关闭后有界排空。

### 小而明确的 Interface

调用者只需要三类操作，以下为职责示意，不是冻结的 Python 签名：

```python
with observe.span(kind="context", name="reasoning.prepare", attributes=...) as scope:
    scope.event("context.selected", attributes=...)
    scope.capture(role="model_input", value=prepared_messages, references=...)
```

span 隐藏身份、父子关系、计时、异常结局和 Context 恢复；capture 隐藏开关、脱敏、限额、引用与写入。
无当前 trace/关闭观测时为 no-op。调用者不能直接打开文件、拼 trace JSON 或管理 writer 队列。
异常仍原样向业务传播；观测器只记录固定类别，不能吞业务异常、重试模型或改变状态流转。

### 标准基础与依赖决定

采用 OpenTelemetry API/SDK 的 trace/context/span 基础，不再自建一套父子跨度框架；
本地 processor/store 负责本项目的即时事件、artifact、保留与删除合同，不要求 Collector 或云账户。
OpenTelemetry 依赖须在 pyproject 显式声明并锁定，不能继续依赖 LangChain 的传递安装；不顺带升级模型框架。
已有标准库 SQLite/文件存储可满足本机需求，不新增数据库服务或前端构建依赖。

OTel 的全局 provider 不在导入时设置；本模块持有显式初始化的 provider，避免多次测试 lifespan 的全局替换冲突。
后续 OTLP/托管平台是导出选择，首版不为未使用的第二个平台建立一组抽象 Adapter。

## 5. 身份、跨度和事件合同

### 身份

- session_id：同一对话；内部仍校验归属，导出可替换为会话别名。
- request_id：同一次用户提交及幂等重放；网络重连查询不能生成第二次 Agent trace。
- trace_id：一次实际执行尝试；首版每个已接受请求只启动一次执行，不自动重试整轮。
- span_id / parent_span_id：具体阶段与所属调用；tool_call_id、模型 SDK run_id 是关联字段，不能直接混成同一种ID。
- attempt：若未来存在显式重试，应建立新的执行尝试并链接原请求；首版不借此扩大重试能力。

HTTP POST/GET/重连与执行分开：轮次 trace 由应用持有，不因202返回或浏览器断开而结束。
查询是观察访问，可记录访问日志或 link，不重复计算模型消耗。accepted但未进入执行即崩溃允许没有完整 trace，须显式报告记录缺口。

### 最小跨度树

```text
turn.execute
  context.assemble
  graph.execute
    node.classify
      model.invoke
    route.decide                       # 可作为事件，不额外调用模型
    node.reasoning / node.pipeline_step # 只出现实际路径
      context.prepare
      model.invoke
    tool.execute                       # 一个真实 tool_call 只计一次
      validation / HTTP / sanitize     # 首版用事件，必要时才增加子跨度
  response.render
    context.prepare
    model.invoke
  session.commit
```

同名循环跨度以不同 span_id/iteration 区分；并行工具是兄弟跨度，不能按事件到达顺序猜父子关系。
旁路、预算终止和未执行阶段记录原因，不用空跨度伪装执行过。

轮次的“已保存回复时间”和“物理执行完全退出时间”分开：迟到或未响应取消的子任务继续保留关联，
不能为了关闭漂亮的 trace 而释放业务占用；使用 commit / execution.quiesced 事件表达两种完成时间。

### 必须观察的语义

| 位置 | 必须记录的元数据 | 详细诊断快照 |
|---|---|---|
| 接受与生命周期 | 身份、实际状态转换、固定reason、是否重放/拒绝、提交结局 | 不复制整份会话库 |
| 上下文装配/窗口 | 历史候选/采用数量、背景版本、材料引用、预算、裁剪/缺失原因 | 装配前后及最终模型消息，明确其阶段 |
| 分类/路由 | 模型分类原值、confidence、实际路由与程序规则/阈值版本 | 分类器实际准备输入和可见返回 |
| 模型调用 | 明确stage、请求/响应模型、有效参数白名单、工具定义版本、tool_choice、usage、finish_reason、错误/取消 | 送入SDK前的完整消息、工具定义、可见输出/tool_calls |
| 工具 | 调用身份、工具/schema版本、参数键、验证结局、实际执行标志、结构化结果类别、耗时 | 原始调用参数、清洗后返回；不把HTTP原始报错正文当数据 |
| 来源整理/Render | 输入/保留/排除的材料ID、来源与截断信息、最终回复对应的引用ID | 图输出、Render实际输入、输出和来源处理后的最终文本 |
| 持久化 | 提交尝试、失败类别、重试保存/核对终态、保存时间 | 不复制其他用户或其他轮次 |

“HTTP成功/工具有数据/回答完成/产品通过”必须分别表达，不能都映射成 span=success。
保留执行状态与资料结局；产品判定以后通过 annotation 加入，不在本轮引入新的评价 Agent。
SDK内部网络重试若不可见则记录 unknown，不把一次应用调用声称为一次供应商请求。

### 基础 schema 与版本

每条记录包含 schema_version、record_id、trace/span关联、kind/name、UTC时间、进程实例ID、单调时长、状态和白名单属性。
即时事件有独立 event_id；span结束导出与已落盘事件按ID合并，避免同一事件或 usage 重复计数。
代码提交/dirty标记、Python与相关依赖版本、模型有效配置摘要、Prompt模板/实际准备输入版本、工具schema版本可关联。
只对公开模板/schema使用普通SHA；私有内容元数据使用不可公开比较的内容引用，不把裸用户文本hash当匿名化。

采用固定本地 schema v1，OTel属性映射固定受支持版本；GenAI约定正在演进，不能把“追随latest”变成业务升级要求。

## 6. 上下文与证据快照是首版必要项

只记录调用名不足以诊断本产品。capture需要保留中立的内容结构：

- message_id、role、内容种类、来源轮次/材料引用；原始项到裁剪/摘要项的 derived_from 关系。
- origin_kind 区分用户陈述、用户引文、Bangumi事实、社区原文与历史助手输出；无法确定则 unknown，不让观测器猜测立场。
- 实体类型/ID、comment/review身份、作者与定位、取得时间、截断状态；只使用现有材料已有字段，缺项如实标缺失。
- selected / omitted / transformed、程序给出的原因、使用预算与内容引用；不能靠输出关键词推断模型读懂了材料。
- 准备输入发生在实际调用处、窗口和提示构建之后；不能拿初始state或最终输出倒推模型当时看见什么。

必须先有这些可追溯观察，不要求先实现自动偏好提取、复杂证据图谱或改变上下文选择算法。
C1–C5原记录保持不变，只作为迁移前证据；不事后给旧unknown记录补造不存在的节点span。

## 7. 日志、usage、耗时统一口径

- 一次 model.invoke 负责一份 usage；模型 Callback 统一读取供应商总输入/输出及 cache子项。
- cache_read 已含在 input_tokens 时不得再次相加；失败没有usage则为null/unknown，不能当0。
- 预计费用只有在价格来源/版本、模型与缓存计费规则齐全时提供，并标估算；不伪造账单。
- 每个 span 用单调时钟计自己的 wall duration。并行工具耗时之和不等于用户等待时间。
- 分别报告 accepted→commit、实际执行跨度、各调用跨度；JSON全量回复没有真实逐token数据时不虚构TTFT。
- summary从唯一模型/工具跨度派生；旧 telemetry响应也只能是此摘要的兼容视图。
- 日志通过统一filter/adapter带关联标识，只记录白名单摘要；正文存artifact，不到处复制。

## 8. 本机存储、效率与隐私

### 采集级别

- off：不生成执行trace或内容快照；业务生命周期日志与会话保存继续正常工作。
- metadata（默认）：跨度、状态、usage、耗时、版本、引用与缺失标记，不保存用户/工具全文。
- diagnostic：由本机使用者显式限定某会话/验收批次与期限，采脱敏详细输入输出；不会被模型或外部请求参数自行开启。

首版可通过本机诊断命令创建限时scope，不另建复杂管理后台。DEV_MODE仅保留兼容开发摘要含义，不能偷偷打开全文。
不采HTTP认证头、密钥、客户端对象repr或隐藏推理；属性白名单、已知密钥脱敏与敏感字段过滤都在入队前执行。
未知对象拒绝序列化；全文开关关闭时不先复制全文再丢弃。不能声称任意自由文本中的秘密均可自动识别。

### 统一落盘路径

```text
.local-data/observability/
  traces.sqlite3                 # 根记录、span、event、版本、artifact索引
  artifacts/<session-scope>/...   # 私有详细内容，按引用连接
```

与 sessions.sqlite3 分离，避免诊断写入争抢业务事务。只维护一个writer；文件权限与目录权限分别限制为600/700。
不做跨会话内容去重，避免删除、权限和引用计数被提前复杂化。

元数据on_start/end及关键事件通过有界队列入队；后台批量写入。重要开始事件不能只等span结束才可观察，
否则进程崩溃时会丢掉“卡在哪一步”的现场。使用本地SpanProcessor加即时event记录，按event_id归并。

候选可逆默认：队列最多2048条且不超过16MiB；单artifact至多512KiB，每轮详细内容至多16MiB；
元数据保留30天、详细内容7天、总量256MiB。实现时验证真实C1–C5大小后校准，不能静默截断。
超限返回 disabled/truncated/dropped/unavailable 的引用状态及原因，报告 trace.recording_complete=false。
只清理观测数据，不得按这些限额删除用户会话历史。

候选性能验收目标：常规元数据热路径埋点p95不超过2ms；正常关闭尽力在1秒内排空，超限报告未完整。
这些是待验证目标，不是当前性能事实。用本地固定替身基准衡量，不能靠真实模型的长等待掩盖观测开销。

### 故障、删除和恢复

观测I/O失败、队列满或SDK exporter失败不得改变业务返回、执行顺序、超时、工具次数和提交结果。
业务会话保存失败仍按原生命周期处理；不能为了“观测不影响业务”吞掉真正业务故障。
不可写时，内存健康计数和已有日志明确报告 dropped/incomplete；无法保证故障本身一定落盘，不宣称完整。
进程重启时，未闭合span标记记录中断，不凭trace猜测业务轮次最终状态；查询由应用层结合两者展示。

删除会话同时清理规范观测库中属于该会话的记录与artifact；取消诊断scope，以删除代次/标识拒绝队列迟到写回。
删除与事件写入必须有回归，避免正文在trace里形成第二份永久副本。
用户主动冻结的旧验收包/导出和备份仍属于既有删除范围外；界面与文档准确说明，不暗中额外保留内容。

## 9. 已有代码的迁移去向

| 现有位置/Interface | 迁移决定 |
|---|---|
| agent/devtools.py 的 ContextVar、RequestTelemetry | 由统一观测运行时取代写入权；需要兼容的导入/响应先作为facade，从新记录派生摘要，不保留双写collector |
| LLMCallRecord / ToolCallRecord / NodeTiming | 映射到统一span与usage；旧字段由兼容视图转换，测试改为真实同一Interface的行为测试 |
| TelemetryLLMWrapper / _maybe_wrap_telemetry | 归并为模型工厂安装的 LangChain Callback Adapter；明确stage来自调用seam，不按对象id或prompt文本猜 |
| _telemetry_label | 短期作为兼容属性，节点/Render显式span成为事实来源；分类也纳入同一调用观测 |
| instrument_tools | 收敛到 ToolNode 单一调用 seam，覆盖校验失败；不在外层和原工具各建一个重复业务span |
| main._run_with_telemetry | 调用方迁移后取消“为观测切换图路径”的职责；普通与DEV走同一执行语义，不在observer里累积state |
| application生命周期日志 | 保留必要运维日志，同时关联统一trace；状态变更事件从相同事实发出，不重新推导另一台状态机 |
| trace_fixed_scenarios.py | 历史运行证据与observer-source副本保持不变；新验收只开启正式diagnostic scope与导出，不再monkey-patch SDK或模型工厂 |
| ChatResponse.telemetry 与 DEV_MODE | 保留字段兼容；默认不回传完整诊断内容。持久会话当前剥离telemetry，正式查询另走受控接口 |

已核对锁定环境的 ToolNode 支持 awrap_tool_call；优先在该 seam 观测真实调用及验证结局，保持原 handle_tool_errors 语义。
模型优先用框架 Callback，记录实际调用消息与绑定参数；实现前用固定测试确认绑定/异常/取消/并发均触发，
若锁定版本存在缺口，只在统一调用Adapter补齐，不能再全局patch BaseChatModel/BaseTool。

现有 main JSON/SSE、graph工具构建、节点工厂、Render、测试与脚本均引用旧Interface。
每个迁移增量先核对引用、验证行为与兼容责任，再在同一聚焦变更中处理替换；不先删除旧文件或测试。

## 10. 分增量实施与退出条件

| 增量 | 具体交付 | 退出条件 |
|---|---|---|
| T1 中立合同与本地记录 | identity/span/event/schema、OTel接线、队列/存储、无副作用初始化 | 跨任务隔离、开始/结束/异常/取消、写失败/限额/关闭、版本拒绝均有测试；业务不依赖落盘成功 |
| T2 归并执行观测 | 模型Callback、ToolNode seam、分类/节点/Render明确span、日志关联、旧telemetry兼容摘要 | 同一执行路径；无重复usage/工具计数、无unknown阶段；旧调用方与JSON/SSE回归通过 |
| T3 材料可见性 | 实际消息快照、裁剪/选择manifest、材料身份与引用、版本/隐私策略 | 能对固定C2/C3/C4/C5材料回答“实际看见什么/遗漏什么”；scope关闭不留全文，采集不改变材料 |
| T4 查询、导出与维护收口 | 受控本机查询/导出、保留清理/删除、观测健康、脚本使用正式scope | 给定一轮可导出完整或明确不完整的记录；删除无迟到复活；无需临时patch；清洁安装与性能目标有实测 |

T1/T2先形成一套写入与统计事实；T3/T4完成才算达到本项目最低诊断要求，不能只有耗时面板就宣布完成。
每个增量独立提交，保留原验收材料，离线使用审阅入口及临时存储；不得访问受保护目录或真实PostgreSQL。
现有C1–C5只读冻结材料先验证观测行为；新增真实调用需按授权与费用条件执行，不默认再次采集。
不把本计划改成Prompt优化、Render移除、自动重试、自动偏好提取或新工具开发。

## 11. 必须具备的回归与维护规则

- 观测off/metadata/diagnostic使用同一业务路径；固定替身下模型输入、工具参数、次数、顺序与持久结局一致。
- 两会话并行、并行工具、后台task、?wait=true观察者取消、重连查询不会串trace或重复计数。
- 分类、Pipeline、ReAct、chat→Render、JSON/SSE、绑定工具、校验前失败、模型异常/取消均闭合或明确标不完整。
- timeout/cancel/delete、提交失败与提交成功但确认失败均沿用生命周期事实，不由观测器重新执行。
- sink不可写、队列满、超大文本、flush超时、SDK异常不拖垮业务，并留下可观察的不完整状态。
- 敏感字段/合成秘密/客户端对象repr不进入元数据、artifact、导出或日志；诊断scope与归属检查不能绕过。
- 跨保留期清理、在途删除、重启、未来schema版本、旧导入和telemetry响应兼容有测试。
- 新调用seam必须在同一回归中有明确stage和父子关系；不允许以unknown掩盖漏埋点。

维护时区分“没有发生”“没有采集”“被裁剪”“被脱敏”“采集失败”，不能都写成空数组。
记录字段升级必须版本化；字段缺失用unknown/null，不补造0、费用、工具调用或节点时间。
与故障合同共同维护，不另外开一份重复生命周期台账。

## 12. 参考与项目取舍

以下用于术语和字段依据，不是强制采购或本项目已经符合标准的声明：

- [OpenTelemetry GenAI观测示例](https://opentelemetry.io/blog/2026/genai-observability/)：模型/工具跨度及usage、内容采集的区分。
- [OpenTelemetry GenAI属性入口](https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/)：属性含义；部分约定已迁往独立仓库，实施时固定映射版本。
- [LangSmith观测概念](https://docs.langchain.com/langsmith/observability-concepts)：一轮trace、多轮thread、调用span及轨迹的不同用途。

本项目选择本地默认、按需详细快照、标准跨度基础与单一事实来源；不要求云平台、Collector、Grafana、
Prometheus、跨服务分布式追踪、隐藏推理采集或自动LLM质量评委。

## 13. 当前维护入口（2026-10-30）

统一链路现在由 `core/observability/runtime.py` 的显式 OTel provider / LocalProcessor、
`agent/observability.py` 的 SDK Callback 与 ToolNode adapter、应用轮次和本地 writer 组成。
原 devtools 保留导入及响应 facade；模型工厂不再按 DEV_MODE 安装第二个 collector，
`_run_with_telemetry` 兼容入口也调用普通 `ainvoke`，不再自行合并流式 state。
`NodeTiming` 的旧更新间隔估算不作为节点跨度写入；调用该兼容写入口会标记 omitted 原因。

### 启动、查询与短期诊断

- `TRACE_LEVEL=metadata` 为默认；`off` 不创建新执行轨迹。`DEV_MODE` 仅控制旧响应摘要。
- `TRACE_PATH` 默认 `.local-data/observability`，与会话库分开。显式 lifespan 启停 writer；导入不打开存储。
- 同源、已认证查询：`GET /api/observability/sessions/{sid}?request_id={rid}`。
  默认只有元数据；追加 `content=true` 才返回已采集的脱敏 artifact 内容，不会开启采集。
  查询附带会话库中的 status/reason；无记录时明确返回 disabled 或 not_recorded，并附采集级别，不重跑请求。
- `GET /api/observability/health` 返回采集级别、writer 状态、队列和丢失计数，不返回身份或正文。

应用启动后，使用本机命令为下一次实际执行开启 scope（owner 使用当前认证用户 ID；匿名为 `anonymous`）：

```bash
python scripts/agent_trace.py scope --owner anonymous --session SESSION_ID --seconds 300
python scripts/agent_trace.py export --owner anonymous --session SESSION_ID --request-id REQUEST_ID --content --output /private/tmp/trace-export.json
```

scope 必须在提交轮次前授予，最长一小时；每次 capture 再检查到期时间。
它不能由模型、工具参数或 HTTP 请求授予。删除会话立即撤销 scope。
CLI 直接修改本地控制记录，不启动第二个 provider writer、不对活进程执行恢复。
导出文件拒绝覆盖已有路径，权限为 600；身份元数据用别名替换，正文仍可能包含使用者主动提供的身份信息。
脱敏不保证识别任意自由文本中的全部秘密；只使用合成秘密做回归，不声称完整匿名化。

### 如何沿一轮查看材料

以 `trace` 找执行尝试，以 `id` / `parent` 看跨度树，以 `request` 对照持久轮次。
`model.invoke` 上的 stage、schema_hash、model_parameters 和 model_input 是实际 SDK 调用边界的证据；
不是由最终回复倒推。usage 只在这些唯一跨度上出现，cache_read 不再加到 input_tokens。
供应商内部尝试数仍为 unknown，没有真实流式数据时不提供 TTFT，也不计算费用。

`context.assemble` 保存装配候选与历史窗口；`context.prepare` 包住原记忆处理，
子级 `context.selection` 记录历史工具压缩、单条限额与窗口选择前后快照和 manifest。
opaque message_id 连接同一消息，transformed 项通过 derived_from 连接原项；
无法唯一对齐时保持空引用，不凭相似文字猜测来源。
ToolMessage 的 tool_call_id 将材料连接到唯一 `tool.execute`；原材料已有的评论 ID、作者、定位和截断标记位于诊断 artifact。
`response.render` 保存图输出、来源裁剪、实际 Render 输入和来源处理后的最终输出；
不将“在工具返回中存在”写成“模型已经理解”。

根跨度保留 accepted_at、committed_at、accepted_to_commit_ms 和物理执行耗时。
提交失败后的再次查询只产生新的 session.commit 跨度，不重跑模型。
执行器未退出时根跨度保持开放，commit 与 execution.quiesced 是不同事实。
重启把未闭合跨度标为 interrupted；业务最终状态始终查会话库。

正式采集链路的可复现合成示例见 [诊断示例](../Test/agent-trace-synthetic-example.json) 和
[验证说明](../Test/agent-trace-verification-2026-10-30.md)。它验证采集与材料交接，不验证模型质量。

### 保留、删除与故障处理

writer 使用有界队列和字节限额；热路径不等待文件写入。默认单 artifact 512KiB、单轮 16MiB，
元数据 30 天、内容 7 天、规范存储总量 256MiB。默认限额保留：旧 C1–C5 冻结快照的文件大小核对见验证说明。
保留清理在启动和空闲维护时运行；手动运行 `python scripts/agent_trace.py prune` 必须先停止应用。
应用持有本地 writer 文件锁，手动清理不与活 writer 同时运行。

删除会话先形成持久删除屏障，再清理索引、artifact 和 scope；屏障拒绝队列及重启后的迟到写入。
物理清理失败会增加健康计数，删除标记立即阻止查询正文，下一次启动继续清理。
应用启动还核对持久 Trace 的会话归属，清理业务删除已提交、观测删除尚未成功时留下的记录。
最小删除屏障不保存正文；使用者冻结的旧验收包、导出及备份不在会话删除范围内。

captured / disabled / truncated / unavailable / expired 与 redacted 标记分别表达状态；
未知对象不使用 repr 序列化。队列满、超限、I/O 或 SDK 故障记录健康缺口，不能冒充完整。
`recording_complete=false` 在缺失、开放跨度、截断、重启或故障时出现；无法落盘的故障只能由内存健康与日志报告。
丢失计数是进程级保守信号，同进程其他轮次也可能因此显示不完整。
诊断文件缺失会在查询时再次检查，避免仅凭 captured 索引声称正文仍在。

### 经代码验证的适配例外

锁定的 langchain-core 1.7.1 `BaseChatModel.agenerate` 在等待内部 `asyncio.gather` 时被取消，
会跳过后续 `on_llm_error`；正式固定替身回归复现了模型跨度遗留 running。
因此增加中立的 `invoke(call, ...)` 作用域，框架 adapter 注册待闭合 Callback，
取消或异常时仅补齐仍未闭合的原跨度。该入口仍只 await 一次原方法、原样传播原异常，
off / metadata / diagnostic 共享它；不包装或全局 patch BaseChatModel，不重试或屏蔽取消。
已收到 SDK terminal Callback 的跨度按 run_id 移除，不再次统计。

新真实验收脚本已移除旧观察 monkey-patch，使用同一 scope 与导出入口；
`run` 要求显式 `--allow-paid`。历史 observer-source 与冻结证据没有修改，本次没有执行真实模型/API 场景。
