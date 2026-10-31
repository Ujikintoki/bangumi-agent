# Agent harness 设计研究

研究日期：2026-10-31。检查官方源码与文档；未运行外部项目、未测其任务质量。本记录是设计参考，不构成实施排期。最初研究读取默认分支；后续采用研究与后端编排研究固定版本，具体以各节为准。较早建议由后续范围校准修订，不作为保留自建底座的既定决定。

## 选择依据

选择 Khoj 与 Open WebUI，原因是它们都把连续对话、用户主动材料、外部检索、模型配置和可选记忆放在同一使用链路。Khoj 更适合观察材料与个性化如何交给回答生成；Open WebUI 更适合观察工具循环、上下文压缩与来源展示。相似性是职责组合，不是主题、用户规模或质量结论。

## 官方原则：可采纳的设计约束与适用条件

以下依据官方工程文章与文档，是经验原则和机制说明，不是统一认证标准，也没有跨模型、跨任务成立的最优拓扑。2024/2025文章仍有价值，但本次同时读取2026评估文章和当日框架文档；未宣称穷尽截至研究日的最新发布。

### 1. 复杂度需要任务收益证明

Anthropic 区分程序预定义路径的 workflow 与模型决定路径的 agent；固定步骤适合可枚举任务，自主循环适合步骤难预知的任务，额外调用要权衡质量、延迟与成本。原文也允许组合模式，因此“简单优先”不等于必须删除图或统一所有路径。[Building effective agents，2024-12-19](https://www.anthropic.com/engineering/building-effective-agents)

对 BGM 的推论：分类、固定查询流程、独立 Render 都是可比较的设计变量。先定义整轮输入输出与任务判据，再比较它们是否减少遗漏或引入交接漂移；不能从官方简单循环示意图直接推出改图收益。

### 2. 上下文工程发生在每次模型调用前

上下文包含系统规则、工具定义、外部材料和历史；需要持续筛选高信号信息。官方建议使用轻量标识按需读取资料，并在需要时混合预检索与自主搜索；压缩应先重召回、再减少冗余，避免关键细节丢失。[Effective context engineering，2025-09-29](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)

对 BGM 的推论：完整会话、活动对象与纠正、当前工具轨迹、来源记录、某次模型视图应有各自语义。实体ID/材料定位是重新取得原文的入口；摘要不能覆盖原始身份。压缩需要验收否定、范围限定与用户认领关系，而不只是检查token变少。

### 3. 工具面向任务，返回足以继续行动的上下文

官方建议减少重叠工具、按高价值任务设计能力；输入输出明确，返回高信号内容，支持过滤/分页/有说明的截断，错误提供可行动的信息。评估应检查最终任务、工具错误、冗余调用、耗时和token；允许存在多个正确工具路径。[Writing effective tools，2025-09-11](https://www.anthropic.com/engineering/writing-tools-for-agents)

对 BGM 的推论：先明确搜索、详情、趋势、短评、长评摘要、讨论区各自能证明什么。工具输出需保留实体身份、取得时间、作者/定位、样本与截断；错误区分非法参数、无匹配、上游失败和能力未支持。是否合并某些工具需看真实选用表现，不追求工具数少这一指标。

### 4. 硬执行合同与语义判断采用不同机制

LangChain 的 guardrails 文档区分确定性规则与模型语义检查，并展示在模型/工具调用前后插入检查的位置；两者有成本与覆盖差异。文档示例只是机制演示，不是可直接照搬的业务策略。[Guardrails](https://docs.langchain.com/oss/python/langchain/guardrails)

对 BGM 的推论：工具允许集、正整数ID、调用数、超时、预算与结构化错误由执行程序落实；“用户是否认领此观点”“草稿是否加强立场”不能靠schema或关键词黑名单保证。程序应保留归属和明确限制，模型服从它们，语义验收判断最终文本。外部材料是数据，不能拥有修改系统规则、授权或用户偏好的权力。人格应受上述任务合同约束；本次资料没有证明人格必须拆成独立生成阶段。

### 5. 会话状态、模型窗口、长期记忆分开

LangChain 将短期记忆置于线程状态中，并讨论裁剪、删除与摘要等模型视图管理方法；工具调用与结果须维持合法消息协议。[Short-term memory](https://docs.langchain.com/oss/python/langchain/short-term-memory)

对 BGM 的推论：现有SQLite继续承担会话事实来源；Agent接收快照并提出受校验的上下文变更。RAG索引描述外部知识，历史记录对话，长期偏好描述经允许保留的用户信息。是否使用同类数据库不改变这三种责任。本项目无需为了采用框架示例而另加一套checkpoint持久化。

### 6. 执行终止、任务完成、回答质量分别验收

Anthropic 的2026评估文章区分任务、尝试、轨迹、实际结果与评分器；建议检查完整轨迹，使用代码、模型和人工各自适合的判据，同时覆盖行为应该发生和不应该发生的正反例，控制试验环境并考察多次尝试稳定性。[Demystifying evals，2026-01-09](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)

对 BGM 的推论：替身验证协议与交接，真实模型验证语义，真实页面验证交付。新增案例应覆盖“已有资料不用再查/确实缺资料要查”“用户引用/用户认领”“纠正讨论范围/断言作品质量”“预算耗尽但已有部分资料”。C1–C5是锚点，不能成为全部产品样本；无需先建设大型评测平台。

### 7. 长任务harness经验需要按任务尺度裁剪

Anthropic 长任务文章用初始化环境、功能列表、增量工作、进度记录和端到端验证处理跨窗口软件开发，并明确示例针对全栈开发、推广到其他领域仍待研究。[Effective harnesses for long-running agents，2025-11-26](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)

对 BGM 的推论：可借鉴显式状态交接和实际完成证据；不把初始化Agent、Git进度、数百功能列表或断点续跑引入普通对话。跨轮继续讨论与连续运行数小时是两类要求。

框架参照：[Deep Agents](https://docs.langchain.com/oss/python/deepagents/overview) 将执行环境、上下文管理、委派和人工干预组合成harness。它说明可用机制，但文件系统、代码执行与子Agent并非BGM首版需求；不由此形成采用该SDK的结论。

## Khoj：分类材料进入统一回答上下文

- `ResearchIteration` 分开保存文档、在线、代码、操作材料、摘要、警告与模型原始响应；`construct_iteration_history` 把并行调用归为一个 assistant 调用批次及相应结果批次。`truncate_messages` 抽出 system，按预算删旧消息，遇到工具调用同时删除紧随结果。[utils.py，约195、221、798行](https://github.com/khoj-ai/khoj/blob/master/src/khoj/processor/conversation/utils.py)
- `build_conversation_context` 接收 references、online_results、code_results、chat_history、relevant_memories 等独立参数；`send_message_to_model_wrapper` 承担多供应商调用；`ai_update_memories` 在开关允许后通过模型提取 create/delete，并直接保存。后者没有在该函数中逐项取得用户确认。[helpers.py，约914、1511、1664行](https://github.com/khoj-ai/khoj/blob/master/src/khoj/routers/helpers.py)
- research 路径构造历史与本轮工具轨迹，允许并行执行部分工具；重复名称与参数组合产生警告；程序有迭代上限及终止结果字段。该机制属于 research 路径，不能推广成所有普通对话均走同一循环。[research.py](https://github.com/khoj-ai/khoj/blob/master/src/khoj/routers/research.py)

对 BGM 的设计启发（推论）：保留材料类别和原始身份，再形成模型视图；摘要不能成为唯一证据。人格、用户记忆与外部材料可以在同一回答调用中装配，但这不证明一次生成优于独立 Render。自动记忆写入不符合本项目更严格的显式偏好边界，应借鉴数据分离而非照搬写入策略。

## Open WebUI：工具循环、来源与压缩各有专门处理

- native 工具路径有迭代计数/上限；每批工具结果更新调用状态，来源发给前端并累积。后续调用前从基础提示重建 citation context，避免逐轮重复追加。文件中同时包含大量路由、检索、权限与输出处理，不是一个可原样移植的小循环。[middleware.py，约5177、5427、5487行](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/utils/middleware.py)
- `compact_messages_for_request` 将工作窗口压缩单独处理；选择切点时保留调用/结果批次，生成摘要时显式加入工具名和参数，默认摘要要求保留决定、限制、材料和未完成任务。这是压缩机制与提示约束，不证明语义无损。[context_compaction.py，约22、334、351行](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/utils/context_compaction.py)
- memory 独立进行召回与完成轮后的后台复核，包含能力、功能配置、间隔和服务端权限检查；复核生成结构化 add/replace/move/remove。其 user/context 长期记忆范围比 BGM 当前产品更广。[memory.py，约268、376、484行](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/utils/memory.py)

对 BGM 的设计启发（推论）：资料来源不只存在于最终文字，应该有程序维护的记录；压缩要维护工具协议完整性；长期记忆不承担会话恢复。无需引入它的通用插件平台、后台记忆或多用户治理。来源元数据和引用入口仍不能证明引文忠实或用户立场正确。

## 对本次方案选择的意义

两项目提供了可借鉴的机制，却没有证明某个完整拓扑适合 BGM。优先对比的设计变量是：材料身份是否独立保留、模型工作窗口与历史是否分离、工具循环是否受程序约束、长期记忆是否有独立写入合同。实体连续性、用户认领观点、范围限定与最终整理忠实性，仍需用本项目的配对场景验收。

未覆盖：完整请求持久化与恢复、供应商兼容矩阵、引用正确率、人格行为质量、提示注入动态实验及长期记忆删除竞争。没有依据宣称任一项目已经解决 BGM 的 C2/C5。

## 扩展采用范围：从机制借鉴到基于上游适配

用户进一步明确：允许更多参考、模仿、复用实现，甚至基于项目框架做自适应。前文对个别通用能力的保守判断不构成排除上游框架的决定。以下是进一步源码调查与采用候选，尚未实施。

本节固定版本：Open WebUI `f7ac7ee10962ddec0017f5c29b6740d9b9c5faca`；Khoj `ae229ca894c0b80ad84664afcfdde523b5e87057`。前文默认分支观察不自动视为逐项在固定版本上重验。

### 可整套模仿的职责组织

- Khoj：选资料来源和输出形式、普通资料取得与research路径、分类材料记录、统一上下文装配、带人格的最终回答。这些可作为整条候选链路，替换BGM现有职责组织，而不限于修补某个工具。[research.py](https://github.com/khoj-ai/khoj/blob/ae229ca894c0b80ad84664afcfdde523b5e87057/src/khoj/routers/research.py)、[helpers.py](https://github.com/khoj-ai/khoj/blob/ae229ca894c0b80ad84664afcfdde523b5e87057/src/khoj/routers/helpers.py)。这条方案仍有路由和阶段交接，不能把它叫作纯单循环。
- Open WebUI：模型配置、原生工具调用、来源事件、模型视图压缩与可扩展处理点可以构成一套交互式工具Agent组织。固定版本的工具循环在middleware约5614行起，维护迭代上限、调用状态、来源累积，后续请求重建引用上下文并按需压缩。[middleware.py](https://github.com/open-webui/open-webui/blob/f7ac7ee10962ddec0017f5c29b6740d9b9c5faca/backend/open_webui/utils/middleware.py)。该文件约6553行，抽取循环必须同时识别模型调用、事件、配置和保存依赖。

### Open WebUI 的现成扩展接口

- Tools：`get_tools`从配置装载并做访问检查；存在OpenAPI工具规格转换与外部调用路径。可把现有Bangumi工具经专用接口接入其原生循环；应只暴露领域只读操作，不能自动暴露BGM整个会话管理接口。[tools.py](https://github.com/open-webui/open-webui/blob/f7ac7ee10962ddec0017f5c29b6740d9b9c5faca/backend/open_webui/utils/tools.py)
- Filters：payload处理有inlet/request处理点，响应有stream/outlet处理点。可承载BGM背景装配、领域约束和输出检查；不能靠叠加Filter假设语义忠实已解决。[middleware.py](https://github.com/open-webui/open-webui/blob/f7ac7ee10962ddec0017f5c29b6740d9b9c5faca/backend/open_webui/utils/middleware.py)
- Pipes：活跃pipe注册成可选模型，`generate_function_chat_completion`调用其`pipe`，接收正文、用户和事件等参数并适配同步/异步与流输出。可将BGM Agent作为专用Pipe承载；此时必须明确BGM还是平台拥有模型/工具循环，避免双重执行。[functions.py](https://github.com/open-webui/open-webui/blob/f7ac7ee10962ddec0017f5c29b6740d9b9c5faca/backend/open_webui/functions.py)

以上源码证明扩展位置存在，未证明BGM字段、来源协议、取消或删除合同无需适配即可通过。

### Khoj 的完整采用入口

固定版本聊天入口先加载会话、Agent配置和相关记忆，再以`aget_data_sources_and_output_format`判断资料来源与输出形式；普通取数或research后进入统一最终生成。[api_chat.py](https://github.com/khoj-ai/khoj/blob/ae229ca894c0b80ad84664afcfdde523b5e87057/src/khoj/routers/api_chat.py#L942)。Agent配置同时包含人格、输入工具、输出模式和聊天模型；可较完整借鉴能力与行为配置，而不将人格仅放在最后改写。[配置模型](https://github.com/khoj-ai/khoj/blob/ae229ca894c0b80ad84664afcfdde523b5e87057/src/khoj/database/models/__init__.py#L220)。research存在MCP工具发现，可考虑在上游内以Bangumi工具扩展；保留BGM运行底座时不必因此新增MCP。

research与helpers直接涉及Django ORM、Adapters、账户与配置，不是可独立安装的轻量harness库。完整项目还声明PostgreSQL/pgvector与多种模型/文档处理依赖。[依赖声明](https://github.com/khoj-ai/khoj/blob/ae229ca894c0b80ad84664afcfdde523b5e87057/pyproject.toml)。移植要识别这些依赖，整项目采用则要接受或改造它们。另需注意`summarizedResult`并非总是独立模型摘要，部分直接来自工具结果序列化；不能仅凭字段名推断有损压缩。最终research交接方式仍需按实际材料检查。

### 三种应公平比较的采用方式

| 方式 | 主要复用 | BGM仍负责 | 关键成本 |
|---|---|---|---|
| 模仿完整架构，保留自己的实现 | 上游任务组织、状态分类、上下文与生成流程 | 本机运行底座、领域工具、全部实现与升级 | 自己维护通用逻辑，但可最贴合现有接口 |
| 抽取较完整的核心实现并适配 | 消息处理、上下文压缩、部分循环/来源或模型调用 | 替换平台配置/存储依赖，补领域合同 | 移植、许可证、上游更新和自有测试责任 |
| 在上游应用内做BGM专用版本/扩展 | 网页、会话、模型配置、工具循环、资料与扩展体系 | Bangumi工具、领域规则、对象/观点状态和任务验收 | 接受上游运行与数据模型，迁移现有底座合同 |

第三种方式应列正式候选。已完成的运行底座是可比较资产，不能成为必须保留所有现实现的理由；若采用上游平台，需要用具体收益说明哪些底座职责被接替，并复验取消、幂等、删除、恢复和配置反馈。用户此前规定基础模式不依赖PostgreSQL，若上游采用方式改变这一产品约束，需要在方案中显式列出取舍。

### 复制实现与许可证

Khoj固定版本采用AGPL-3.0-or-later；Open WebUI固定版本采用带品牌保留条件与例外的自定义许可证，并另有历史材料许可证说明。可修改不等于无条件按任意许可证重新分发。[Khoj许可证](https://github.com/khoj-ai/khoj/blob/ae229ca894c0b80ad84664afcfdde523b5e87057/LICENSE)、[Open WebUI许可证](https://github.com/open-webui/open-webui/blob/f7ac7ee10962ddec0017f5c29b6740d9b9c5faca/LICENSE)、[历史许可证](https://github.com/open-webui/open-webui/blob/f7ac7ee10962ddec0017f5c29b6740d9b9c5faca/LICENSE_HISTORY)。本次读取许可原文用于筛选采用方式，未完成针对具体分发方案的法律判断。

初步推荐（设计判断）：以Khoj作为整条Agent职责链路的主要参照，以Open WebUI作为完整承载平台与扩展机制的候选；将“抽核心接现有底座”与“上游平台内BGM扩展”进行同任务设计对比。未确定选型，未安装、移植或运行外部项目。

## 后端编排全过程与运行底座取舍：进一步研究

### 对“自建后端未经使用检验”的重新判断

用户指出：零用户的自建local-web后端缺少实际使用磨合，Web请求、压力、配置等通用运行工作应优先考虑成熟应用的积累。该观点改变采用方案的比较权重：本次需要研究和复用的范围包含完整Web后端，不仅是模型内部循环。

本项目并非没有任何验证：[集成验收](../Test/local-web-integrated-acceptance-2026-10-31.md)已有真实页面、少量真实模型请求与受控故障证据。但这些证明的是选定条件下的接线和行为，不能证明长期使用、多配置、多供应商、资源压力或版本升级的可靠性。“基础harness工程收口”只能理解为当轮限定工作完成，不能用作自建底座更值得保留的理由。此前“现有SQLite继续承担”“不重新建设底座”等建议不再是本次设计前提。

我的判断：成熟上游应获得优先采用和深入验证的地位，尤其是通用Web后端；自己实现需要给出额外价值理由。成熟性的价值包括实际问题进入修复、回归、配置与部署机制，以及后续持续维护，不只代码行数。另一方面，“每个具体合同一定比BGM更强”仍不能从项目成熟性推出：下文查到的两个上游有不同的断连、保存和恢复语义。应优先比较是否接受这些语义，不能预先要求上游逐字实现BGM自己制定的全部合同。

本节Open WebUI源码固定`f7ac7ee10962ddec0017f5c29b6740d9b9c5faca`，Khoj固定`ae229ca894c0b80ad84664afcfdde523b5e87057`。这是围绕普通网页对话和相关生命周期的调用追踪，包含工具与资料支路，不是两个平台所有音视频、自动化、共享和管理功能的完整审计。

### Open WebUI：网页后台执行与事件交付

```text
启动：配置/迁移/资源初始化 → 数据库、共享HTTP池、可选Redis、Socket、后台清理
网页提交 /api/chat/completions
  → 身份、模型可用性和访问权、全局/模型/本轮参数合并
  → 创建或更新会话、用户消息、助手占位、附件关系
  → 为每个目标模型登记后台task，返回task_ids与chat_id
后台process_chat
  → 从数据库重建消息链与结构化工具轨迹
  → 上下文压缩、Filter、记忆、资料/工具能力装配
  → 模型分派：Pipe / Ollama / OpenAI兼容等
  → 供应商流解析 → 工具执行↔后续模型调用（有迭代上限）
  → Socket事件 + 进行中输出快照
  → 最终output/usage/done落库 → 完成事件 → Filter与后台附加任务
刷新/重连：数据库会话 + 当前task的输出快照
停止：归属检查 → task取消 → 关闭上游流 → 保存部分输出并清理快照
删除：归属检查 → 停止在途任务/内部子会话 → 删除记录
```

**启动和配置。** 容器入口处理启动密钥等条件后启动uvicorn，可配置worker数；lifespan设置线程执行资源、导入旧配置、填充配置默认值、初始化运行状态、共享Redis/Socket和清理任务，结束时关闭共享HTTP会话等资源。就绪检查检查startup标记、数据库及配置的Redis连通；这不等于模型凭据和每个工具就绪。[start.sh](https://github.com/open-webui/open-webui/blob/f7ac7ee10962ddec0017f5c29b6740d9b9c5faca/backend/start.sh)、[main.py#L366](https://github.com/open-webui/open-webui/blob/f7ac7ee10962ddec0017f5c29b6740d9b9c5faca/backend/open_webui/main.py#L366)、[就绪#L3093](https://github.com/open-webui/open-webui/blob/f7ac7ee10962ddec0017f5c29b6740d9b9c5faca/backend/open_webui/main.py#L3093)。数据库迁移入口位于config导入路径；这与BGM当前强调的无导入副作用规范不同，若整应用采用应理解其启动方式。[config.py#L64](https://github.com/open-webui/open-webui/blob/f7ac7ee10962ddec0017f5c29b6740d9b9c5faca/backend/open_webui/config.py#L64)

**接收和登记。** `chat_completion`校验身份、模型权限，合并全局默认、模型配置、本轮参数并处理模型fallback；新聊天先保存用户消息与助手占位。网页路径为每个模型建立后台task并返回ID。已有会话有访问与写入校验，模型/附件/工具权限分别处理。[main.py#L1146](https://github.com/open-webui/open-webui/blob/f7ac7ee10962ddec0017f5c29b6740d9b9c5faca/backend/open_webui/main.py#L1146)、[登记消息#L1427](https://github.com/open-webui/open-webui/blob/f7ac7ee10962ddec0017f5c29b6740d9b9c5faca/backend/open_webui/main.py#L1427)、[后台task#L1884](https://github.com/open-webui/open-webui/blob/f7ac7ee10962ddec0017f5c29b6740d9b9c5faca/backend/open_webui/main.py#L1884)。这不是“每会话仅一轮”的BGM合同；平台允许多模型等多任务组织。

**上下文到执行。** 已保存聊天从数据库按消息链重建，保留前端不完整携带的结构化output。payload阶段负责压缩、Filter、相关材料与能力；模型分派再选择Pipe、Ollama或OpenAI兼容路径。native工具循环在流响应处理中解析调用，执行工具、汇总来源、重建后续模型消息，再继续生成。[middleware.py#L1894](https://github.com/open-webui/open-webui/blob/f7ac7ee10962ddec0017f5c29b6740d9b9c5faca/backend/open_webui/utils/middleware.py#L1894)、[payload#L2052](https://github.com/open-webui/open-webui/blob/f7ac7ee10962ddec0017f5c29b6740d9b9c5faca/backend/open_webui/utils/middleware.py#L2052)、[模型分派](https://github.com/open-webui/open-webui/blob/f7ac7ee10962ddec0017f5c29b6740d9b9c5faca/backend/open_webui/utils/chat.py#L148)、[循环#L5614](https://github.com/open-webui/open-webui/blob/f7ac7ee10962ddec0017f5c29b6740d9b9c5faca/backend/open_webui/utils/middleware.py#L5614)。

**流、保存与刷新。** task保存在本进程asyncio任务表；可选Redis保存任务索引、心跳和流快照，并通过发布订阅转发停止命令。Socket事件交付与部分状态写库配合；流增量使用快照，完成后最终output写入数据库。读取会话时把活动输出快照覆盖到持久会话上，支持回来时看到仍在生成的回复。[tasks.py#L162](https://github.com/open-webui/open-webui/blob/f7ac7ee10962ddec0017f5c29b6740d9b9c5faca/backend/open_webui/tasks.py#L162)、[快照#L204](https://github.com/open-webui/open-webui/blob/f7ac7ee10962ddec0017f5c29b6740d9b9c5faca/backend/open_webui/tasks.py#L204)、[事件](https://github.com/open-webui/open-webui/blob/f7ac7ee10962ddec0017f5c29b6740d9b9c5faca/backend/open_webui/socket/main.py#L1596)、[完成保存#L6344](https://github.com/open-webui/open-webui/blob/f7ac7ee10962ddec0017f5c29b6740d9b9c5faca/backend/open_webui/utils/middleware.py#L6344)、[会话读回](https://github.com/open-webui/open-webui/blob/f7ac7ee10962ddec0017f5c29b6740d9b9c5faca/backend/open_webui/routers/chats.py#L1380)。保存调用位于完成事件之前，但`upsert`会捕获异常返回None；不能仅凭调用顺序认定保存失败一定不会显示完成。[模型写入#L1391](https://github.com/open-webui/open-webui/blob/f7ac7ee10962ddec0017f5c29b6740d9b9c5faca/backend/open_webui/models/chats.py#L1391)

**停止、删除和重启。** 停止入口校验聊天写权限与任务归属；取消路径shield关闭上游body iterator、把进行中output标记incomplete、保存部分输出并清理快照；删除先授权再取消任务并删除会话。数据库消息恢复、刷新时连接仍在运行的task、进程崩溃后继续同一个执行是三种能力；这里的Redis任务记录不等于持久执行队列，asyncio协程不会因重启自动重建。[停止入口](https://github.com/open-webui/open-webui/blob/f7ac7ee10962ddec0017f5c29b6740d9b9c5faca/backend/open_webui/main.py#L2215)、[取消处理#L6378](https://github.com/open-webui/open-webui/blob/f7ac7ee10962ddec0017f5c29b6740d9b9c5faca/backend/open_webui/utils/middleware.py#L6378)、[删除#L1677](https://github.com/open-webui/open-webui/blob/f7ac7ee10962ddec0017f5c29b6740d9b9c5faca/backend/open_webui/routers/chats.py#L1677)。本次未证明存在等同BGM request_id重放去重的整轮合同。

**压力和多实例。** HTTP客户端有共享TCP连接池、总连接/单主机限制、DNS缓存、总超时和流空闲读取超时，finally清理响应；数据库有连接池，消息事务使用SQLite BEGIN IMMEDIATE或数据库行锁，可选Redis协调跨进程事件与取消。[session_pool.py](https://github.com/open-webui/open-webui/blob/f7ac7ee10962ddec0017f5c29b6740d9b9c5faca/backend/open_webui/utils/session_pool.py)、[数据库池](https://github.com/open-webui/open-webui/blob/f7ac7ee10962ddec0017f5c29b6740d9b9c5faca/backend/open_webui/internal/db.py#L450)、[消息事务#L447](https://github.com/open-webui/open-webui/blob/f7ac7ee10962ddec0017f5c29b6740d9b9c5faca/backend/open_webui/models/chats.py#L447)。这些是资源与并发机制，不能推出吞吐数字或全局任务准入/公平排队；多worker还要配套共享状态，单纯调worker数不构成可靠部署。

**入口差异。** 网页有chat/session元数据时走后台task与Socket事件；legacy/direct调用还可能同步返回StreamingResponse，消费者断连语义不同。接BGM扩展时应指定采用哪条路径，不能用API冒烟替代网页路径验证。[分支#L1884–1930](https://github.com/open-webui/open-webui/blob/f7ac7ee10962ddec0017f5c29b6740d9b9c5faca/backend/open_webui/main.py#L1884)

### Open WebUI 的使用反馈与回归资产

主仓后端CI包括格式和逻辑检查；另有工作流在满足版本PR条件时调用官方`open-webui/tests`仓库，不能把任意PR检查绿色解释为完整回归。[backend.yaml](https://github.com/open-webui/open-webui/blob/f7ac7ee10962ddec0017f5c29b6740d9b9c5faca/.github/workflows/backend.yaml)、[regression.yaml](https://github.com/open-webui/open-webui/blob/f7ac7ee10962ddec0017f5c29b6740d9b9c5faca/.github/workflows/regression.yaml)。

测试仓本次固定树`294a0491d12da14480011fb2d734775f07f3ea53`，与应用固定版本是独立快照，不宣称该组合实际通过。已读测试覆盖切走/关闭页签后回来、第二页签跟随/停止、晚返回的任务查询覆盖新任务、供应商中途断流及重生成，并注明一些已复现问题和修复判别条件。这比单纯README声称稳定提供了更具体的维护证据。[离开和返回](https://github.com/open-webui/tests/blob/294a0491d12da14480011fb2d734775f07f3ea53/e2e/chat/test_reply_while_away.py)、[任务状态竞争](https://github.com/open-webui/tests/blob/294a0491d12da14480011fb2d734775f07f3ea53/e2e/chat/test_chat_taskids_race.py)、[供应商断流](https://github.com/open-webui/tests/blob/294a0491d12da14480011fb2d734775f07f3ea53/e2e/chat/test_provider_drop_mid_reply.py)。

官方测试合同规定HTTP集成、真实浏览器与源码检查分工；测试harness启动隔离实例，使用可脚本控制的模型供应商。工作流存在集成分片和浏览器准备，PostgreSQL/Redis集成模式的job设置`continue-on-error`，不能当作所有模式同等强度的发布阻断。本次未运行测试、未查询其实际CI通过结果，也未取得生产压力报告。[测试合同](https://github.com/open-webui/tests/blob/294a0491d12da14480011fb2d734775f07f3ea53/docs/regression-test-contract.md)、[执行工作流](https://github.com/open-webui/tests/blob/294a0491d12da14480011fb2d734775f07f3ea53/.github/workflows/regression.yml)

### Khoj：统一事件生成器与中断材料续接

```text
启动：CLI/env、Django数据库/配置 → 默认模型/Agent初始化 → FastAPI与网页挂载
HTTP中间件：Session/认证、数据库连接清理、错误与断连处理
POST聊天 或 WebSocket聊天
  → 身份、用户/client归属、能力就绪、限流/连接条件检查
  → event_generator创建turn_id、取消事件、分类材料与轨迹
  → 读取会话、Agent、模型、历史、相关记忆、上次中断材料
  → 选择资料来源与输出形式
  → 普通取资料 或 research工具循环
  → 统一上下文 → 最终模型流 → 事件交付
  → 创建后台保存任务（正文、资料、轨迹等），随后发结束事件
断连/停止：设置取消 → 尝试保存空正文及已取得材料
后续请求：提取末尾中断记录中的材料，重新组织当前任务
```

**启动与运行。** CLI/env、Django设置与数据库、默认Agent/模型初始化后挂载FastAPI路由、Django管理与网页。中间件负责Session/Bearer等身份、DB错误与连接清理；`AsyncCloseConnectionsMiddleware`处理FastAPI线程池和Django混合同步/异步带来的旧连接。调度器有数据库jobstore与leader process lock，解决的是调度去重，不等于聊天会话互斥。[main.py#L119](https://github.com/khoj-ai/khoj/blob/ae229ca894c0b80ad84664afcfdde523b5e87057/src/khoj/main.py#L119)、[configure.py#L69](https://github.com/khoj-ai/khoj/blob/ae229ca894c0b80ad84664afcfdde523b5e87057/src/khoj/configure.py#L69)、[认证#L98](https://github.com/khoj-ai/khoj/blob/ae229ca894c0b80ad84664afcfdde523b5e87057/src/khoj/configure.py#L98)、[调度#L150](https://github.com/khoj-ai/khoj/blob/ae229ca894c0b80ad84664afcfdde523b5e87057/src/khoj/main.py#L150)。

**配置和接收。** `is_ready_to_chat`按用户/默认模型与供应商配置检查可用性并给配置提示；HTTP聊天和WS聊天最终进入共同的`event_generator`，WS另处理Origin、连接和请求频率。模型包装器按上下文大小、tokenizer、vision能力组织消息，并区分可重试错误与配置fallback。[配置检查](https://github.com/khoj-ai/khoj/blob/ae229ca894c0b80ad84664afcfdde523b5e87057/src/khoj/routers/helpers.py#L169)、[HTTP入口#L1733](https://github.com/khoj-ai/khoj/blob/ae229ca894c0b80ad84664afcfdde523b5e87057/src/khoj/routers/api_chat.py#L1733)、[WS入口#L1491](https://github.com/khoj-ai/khoj/blob/ae229ca894c0b80ad84664afcfdde523b5e87057/src/khoj/routers/api_chat.py#L1491)、[模型包装#L1635](https://github.com/khoj-ai/khoj/blob/ae229ca894c0b80ad84664afcfdde523b5e87057/src/khoj/routers/helpers.py#L1635)。

**会话到回答。** `event_generator`创建本轮turn_id/cancellation_event，按用户/client查会话、加载Agent配置、历史、记忆和先前中断的online/code/research/operator材料。资料来源与输出形式选择后走普通取数或research，统一最终回答。research具备工具批次、动态选择、迭代限制和中途指示等机制，具体任务状态仍由该应用流程管理。[生成器#L676](https://github.com/khoj-ai/khoj/blob/ae229ca894c0b80ad84664afcfdde523b5e87057/src/khoj/routers/api_chat.py#L676)、[上下文加载#L944](https://github.com/khoj-ai/khoj/blob/ae229ca894c0b80ad84664afcfdde523b5e87057/src/khoj/routers/api_chat.py#L944)、[用户/client过滤](https://github.com/khoj-ai/khoj/blob/ae229ca894c0b80ad84664afcfdde523b5e87057/src/khoj/database/adapters/__init__.py#L1114)、[research#L476](https://github.com/khoj-ai/khoj/blob/ae229ca894c0b80ad84664afcfdde523b5e87057/src/khoj/routers/research.py#L476)。

**流与断连。** HTTP监听`http.disconnect`，设置取消事件，shield保存空正文和已取得材料；WS有当前连接任务和interrupt队列，可停止/加入新指示，同连接新普通请求会取消旧任务，断开WS也触发中断。因此它与BGM现有“关闭页面仍独立执行完”的选择不同；这首先是交互语义取舍。取消信号存在不等于所有外部供应商立即停止，本次未动态验证。[HTTP断连#L753](https://github.com/khoj-ai/khoj/blob/ae229ca894c0b80ad84664afcfdde523b5e87057/src/khoj/routers/api_chat.py#L753)、[WS取消#L1523](https://github.com/khoj-ai/khoj/blob/ae229ca894c0b80ad84664afcfdde523b5e87057/src/khoj/routers/api_chat.py#L1523)、[中断保存#L788](https://github.com/khoj-ai/khoj/blob/ae229ca894c0b80ad84664afcfdde523b5e87057/src/khoj/routers/api_chat.py#L788)。

**保存、恢复、删除。** 正常生成后`asyncio.create_task(save_to_conversation_log(...))`，随后发送结束/usage事件，并不await保存提交。保存包含turn_id、正文、引用、online/code等资料、轨迹并处理记忆。会话保存路径读messages后追加整JSON并asave；在本次已读路径中未找到turn_id重放去重、同会话全局锁或删除版本保护，会话不存在时存在创建分支。不能据静态观察断言必现覆盖或删除复活故障，但也不能声称已继承BGM的幂等与删除竞争合同。末尾空正文中断记录可被取出复用材料，属于后续任务续接，不是进程重启后持久协程续跑。[异步保存#L1449](https://github.com/khoj-ai/khoj/blob/ae229ca894c0b80ad84664afcfdde523b5e87057/src/khoj/routers/api_chat.py#L1449)、[保存内容#L541](https://github.com/khoj-ai/khoj/blob/ae229ca894c0b80ad84664afcfdde523b5e87057/src/khoj/processor/conversation/utils.py#L541)、[DB写入#L1567](https://github.com/khoj-ai/khoj/blob/ae229ca894c0b80ad84664afcfdde523b5e87057/src/khoj/database/adapters/__init__.py#L1567)、[中断记录#L703](https://github.com/khoj-ai/khoj/blob/ae229ca894c0b80ad84664afcfdde523b5e87057/src/khoj/database/models/__init__.py#L703)。删除入口及所有调用方未做完整竞争审计，以上只描述已读保存路径。

**压力、部署和验证。** 存在用户请求频率、命令、图像大小/数量、WS连接管理与陈旧连接清理；但billing关闭时会跳过部分限流，匿名/debug模式也有连接限制放行，不能假定个人实例天然启用所有保护。计数后create不代表原子配额。默认Uvicorn的WS ping与keep-alive超时、外部Gunicorn部署支持提供可配置运行方式，不构成容量承诺。[请求限制#L2104](https://github.com/khoj-ai/khoj/blob/ae229ca894c0b80ad84664afcfdde523b5e87057/src/khoj/routers/helpers.py#L2104)、[连接管理#L2296](https://github.com/khoj-ai/khoj/blob/ae229ca894c0b80ad84664afcfdde523b5e87057/src/khoj/routers/helpers.py#L2296)、[运行参数#L218](https://github.com/khoj-ai/khoj/blob/ae229ca894c0b80ad84664afcfdde523b5e87057/src/khoj/main.py#L218)。CI配置在多Python版本和临时pgvector PostgreSQL环境运行pytest，测试资产包含认证、隔离、配置、会话工具等；未运行、未核验该版本CI结果，未取得聊天压力基准或完整故障矩阵。[CI](https://github.com/khoj-ai/khoj/blob/ae229ca894c0b80ad84664afcfdde523b5e87057/.github/workflows/test.yml)、[tests](https://github.com/khoj-ai/khoj/tree/ae229ca894c0b80ad84664afcfdde523b5e87057/tests)

### 采用判断与扩展思路

| 要解决的事情 | Open WebUI | Khoj | 对BGM的判断 |
|---|---|---|---|
| 完整Web后端与模型配置 | 网页后台任务、事件、刷新快照、供应商连接层 | HTTP/WS统一事件生成、模型/Agent配置、连接清理 | 优先整体复用这些已有协作机制 |
| 资料和研究型Agent | 原生工具循环、来源、资料/压缩与扩展点 | 资料需求/输出选择、research、统一上下文与人格 | 两者都可承载，Khoj作为完整行为蓝本尤其有价值 |
| 离开页面后继续执行 | 网页主路径有后台task | 已读HTTP/WS路径断连触发中断与材料保存 | 决定产品希望哪种行为；不先按BGM旧合同给上游打不合格 |
| 保存和恢复 | 最终数据库+活动快照；不等于进程断点续跑 | 正常保存异步；中断材料可供后续请求复用 | 明确界面完成和保存的关系，只补真正需要的保证 |
| 长期维护可靠性 | 可见使用问题、对应回归和独立测试体系 | 配置/连接/限流/模型与数据库测试资产 | 保持上游升级路径，才更可能持续享受积累 |
| 压力承诺 | 有连接池/数据库/Redis机制，无本次容量结果 | 有条件限流/连接机制，无本次容量结果 | 单用户也需测快速重复发送、多标签页、慢工具和资源限制；不先造多人压力平台 |

我的采用优先级进一步调整为：**先认真设计“在上游完整应用路径内承载BGM”的方案，再判断哪些独有要求值得自建或移植。** Open WebUI当前更适合作为Web运行承载的首要验证候选，Khoj是另一条完整应用候选兼Agent组织蓝本；这是基于本次查到机制与适配距离的设计判断，不是运行测量排名。自建底座保留为可回退实现与行为参考，不能因已投入工程而默认胜出，也不在本轮删除。

具体扩展方向：

1. **平台原生工具路线。** 上游拥有会话、执行、模型调用、流交付与恢复；BGM提供只读领域工具、人格与材料/观点规则、少量可见的对象/进度状态。这样复用后台编排最多。先检查工具结果怎样带作者、定位、范围和认领信息进入模型/界面。
2. **专用Agent扩展路线。** 若原生Agent能力不足，可用Open WebUI Pipe等承载专用决策逻辑；清楚指定循环、模型、取消与保存各由谁负责。仅把旧BGM HTTP包在新UI后面，不能宣称后端问题已转交上游；某些能力仍需桥接。
3. **Khoj专用应用路线。** 保留其配置、会话、事件生成和资料管线，通过领域工具/Agent能力配置适配Bangumi；接受其运行依赖与交互语义，确有产品价值的地方才改核心。
4. **维护路径也是采用条件。** 优先配置与扩展接口，较少改核心；固定应用与扩展版本、保留升级回归。抽取几个函数后长期自行维护，并不能完整继承用户反馈与上游修复。

下一轮若授权进入验证，应先在可丢弃实例上走小而完整的实际使用链：首次配置→连续查询/讨论→工具慢或失败→离开/返回页面→停止→重复提交/多标签页→重启/删除→配置变更。检查真实模型输入、页面与保存读回，同时记录哪些旧合同主动放弃。供应商/工具压力、真实模型费用、依赖安装与服务启动需按当时授权执行；本轮仅研究和文档补充。已有集成报告保持原样，不把未运行上游写成通过。
