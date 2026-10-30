# Agent Tools 设计一手资料与项目取舍

> 核对日期：2026-10-30。聚焦结构化 Bangumi 取数及材料流转；不是新产品基线、实施承诺或完整行业标准。
> 只查官方文档与静态代码，没有调用模型、Bangumi API或数据库。整体实施顺序仍由 ROADMAP 维护。

## 1. 面向 Agent 的工具接口，不等于逐个 HTTP endpoint 映射

Anthropic 将工具视为确定性程序与非确定性 Agent 之间的合同，建议按真实任务选择工具、
返回高信号信息，并用真实任务评测。它没有规定唯一的 JSON/XML/文本格式，也提醒评测不要限定唯一正确调用路径。
来源：[Writing effective tools for agents](https://www.anthropic.com/research/writing-tools-for-agents)。

它同时讨论可读名称与技术ID：减少无关标识，但下游调用需要的ID仍应可取得；支持按需详略、
过滤与分页。其建议不是“把所有ID去掉”，也不是将完整API对象始终交给模型。
来源：[Returning meaningful context / token efficiency](https://www.anthropic.com/research/writing-tools-for-agents)。

本项目推论：

- 领域工具可以聚合互相关联的只读请求，例如同一条目的有限社区样本；没有必要把每个endpoint都暴露为一个工具。
- 聚合结果必须保留各部分的覆盖与失败，不能因一部分成功就把整份结果说成完整成功。
- 名称帮助模型理解对象，类型与ID帮助程序准确执行；二者不能互相替代。
- “确认条目”“取属性”“读样本”是清楚的能力职责；不应把它们组合成无法观察内部选择的万能回答工具。
- 工具是否易用要通过C1–C5及邻近反例验证，不能只看名称、工具数量或Token减少量。

## 2. 框架明确区分返回材料与状态更新

LangChain 文档说明，字符串或dict返回会成为模型可读取的工具输出；dict不会直接更新graph状态。
需要更新状态时可返回Command；匹配tool_call_id的ToolMessage仍然必要，并行状态更新需要reducer。
来源：[Tools — return values / Command](https://docs.langchain.com/oss/python/langchain/tools#tool-return-values)。

同一文档的ToolRuntime将运行时身份、配置、状态等与模型生成参数区分；这提供注入能力，
并不意味着凭据、owner或会话ID应该由模型填写。
来源：[Tools — access context](https://docs.langchain.com/oss/python/langchain/tools#access-context)。

本项目推论：

- “工具返回有ID”不等于“下一步或下一轮工作上下文已经保存了ID”；中间需要明确的结果接纳与状态装配职责。
- 不应让底层BangumiClient或领域取数模块导入AgentState。状态更新由编排侧Adapter处理，维持仓库依赖方向。
- Command是可用机制，不是必须采用的架构；也不自动解决跨轮持久化、用户纠正、对象确认或删除竞争。
- 即使使用普通ToolNode，也可以由编排侧从规范返回提取材料及身份，再显式装配下一次模型输入。
- 模型参数校验能阻止0等越界ID，不能证明一个正整数对应正确对象；消歧仍需名称、类型、来源和选择依据。

## 3. 模型可见内容与程序持有材料可以分开

LangChain ToolMessage 的content进入模型上下文，artifact用于程序读取且不发送给模型；
tool_call_id将结果关联到原调用。官方示例把检索正文放content，把文档定位等放artifact。
来源：[Messages — Tool message / artifact](https://docs.langchain.com/oss/python/langchain/messages#tool-message)。

本项目推论：

- 可以保留规范结构化材料，再按本轮任务生成模型视图；不是把完整JSON先变成自然语言后丢弃原材料。
- 只存在artifact或数据库中的字段，模型不会自动知道。执行后续调用需要的对象身份，应在适当模型视图中显式提供或由受控Adapter解析。
- 可裁剪正文，但不能静默丢掉作者、定位、范围、取得时间、转载声明等影响含义的限定。
- artifact是载体，不是长期知识库，也不自动定义过期、容量、权限和会话删除规则。
- Trace保留观察证据；业务工作上下文不能依赖读取调试Trace来恢复已确认对象或材料。

## 4. MCP可借鉴返回合同，不必引入传输协议

核对时MCP的latest入口重定向至2026-07-28。本记录固定该版本，不与2025-06-18的字段约束混写。
该版本允许structuredContent为JSON值；声明outputSchema后，服务端结果须符合schema，客户端应验证。
结构化工具返回与“让模型按schema生成答案”是不同机制。
来源：[MCP 2026-07-28 — structured content / output schema](https://modelcontextprotocol.io/specification/2026-07-28/server/tools#structured-content)。

该版本区分协议错误与可供模型纠正的工具执行错误；API失败、输入校验和业务错误可通过isError结果反馈。
模型看到错误不等于一定纠正成功。
来源：[MCP 2026-07-28 — error handling](https://modelcontextprotocol.io/specification/2026-07-28/server/tools#error-handling)。

本项目推论：

- 借鉴“输入合同＋输出合同＋明确错误”的组织方式；保持Pydantic v2及结构化错误，不要求改成MCP Server。
- 传输失败、参数错误、对象未找到、正常空结果、部分资料缺失应分别表达；HTTP 200和工具执行返回均不等于任务通过。
- 返回schema只校验结构，不证明外部事实、搜索候选或评论归属正确。
- 是否继续、澄清或结束由任务执行合同决定；不能单凭isError、span颜色或模型暂未调工具推断任务完成。

## 5. Bangumi endpoint本身的职责和稳定性

Bangumi官方开发文档区分公开API与为新网站前端提供的私有API；后者没有兼容性保证。
来源：[官方dev-docs说明](https://github.com/bangumi/dev-docs#api)。

官方p1 schema分别提供subjects的comments、reviews、topics以及搜索等路径。
本项目客户端也使用p1；例如搜索通过POST请求发送条件，因此业务只读不能被写成“只能使用GET”。
来源：[官方p1 OpenAPI](https://github.com/bangumi/dev-docs/blob/master/api.yaml)，
本地对应：[clients/client.py](../../clients/client.py)、[clients/base.py](../../clients/base.py)。

本项目推论：

- 供应商路径、字段与认证差异留在Client Adapter内；Agent面向稳定领域能力，避免把p1字段变化传播至所有Prompt。
- comments/reviews/topics不能互称；未接入或未取得的维度明确标缺口，不扩大抓取范围来掩盖语义差异。
- 工具的read-only属性按操作副作用判断；具体HTTP方法仍由官方endpoint合同决定。
- 静态文档只能证明接口约定，不能证明现场可达性、权限或当前数据真值；这些需要隔离夹具或获授权的真实验收。

## 6. 适用范围与不直接搬用的部分

三类资料提供互补视角：Anthropic关注模型是否用得好，LangChain提供材料/消息/状态机制，MCP提供协议合同。
没有一份资料规定本项目的完整业务状态模型；以下仍需本项目自己定义：

- 何时把候选对象提升为当前对象、何时切换或清除；不能自动确认首个搜索结果。
- 事实的时效、引用材料的保留范围，以及生成观点与用户认领观点的区别。
- 本轮任务完成、部分完成、等待澄清与执行失败的判据。
- 会话隔离、原子持久化、迟到执行、删除、版本升级与材料容量。

不因此引入云服务、Collector、MCP部署、长期自动画像、复杂证据图谱或新多Agent运行架构。
也不采模型隐藏推理。机制是否适配锁定依赖版本，应在实现时用最小固定探针验证，不直接复制在线最新示例。
这些取舍服务已有连续共读链路，不能把当前C2解释为整个Tools模块必须推倒重来。
