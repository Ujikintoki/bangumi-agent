# Khoj / Open WebUI：网页与后台生命周期核对

> 2026-10-31只读研究快照。仅讨论适用机制，不是采用决定、路线图或新的开发授权。
> 没有安装或运行上游项目，没有修改BGM生产代码；本次源码观察不等同于上游动态故障验收。

## 核对范围与版本

- Open WebUI：`f7ac7ee10962ddec0017f5c29b6740d9b9c5faca`。
- Khoj：`ae229ca894c0b80ad84664afcfdde523b5e87057`。
- 范围限定为任务提交、后台所有权、连接断开、取消、消息保存、活动响应恢复及依赖。
  不用工具循环、记忆功能数量或项目规模推断生命周期更可靠。
- 下述行号按固定版本原始文件核对。网页提取器的展示行号可能不同，引用以GitHub源码定位为准。

## Open WebUI：可化用的机制与实际耦合

### 任务身份与运行所有权

`tasks.py`的活动执行保存在进程内`asyncio.Task`映射；单独维护会话到任务的索引。
`create_task`登记、回调清理；配置Redis后附加任务索引、TTL、心跳及停止命令广播。
Redis保存的是任务标记和索引，不是可恢复的协程栈。
由此只能确认跨实例发现/停止机制，不能把它叫作具备重启续跑保证的持久工作流引擎。
[tasks.py L18–183](https://github.com/open-webui/open-webui/blob/f7ac7ee10962ddec0017f5c29b6740d9b9c5faca/backend/open_webui/tasks.py#L18)

有`session_id`和`chat_id`的交互路径将处理工作交给任务管理器，按模型分配不同的消息和任务身份，
然后返回任务ID；legacy/direct路径仍等待处理结果。不能将交互路径的后台语义推广到全部入口。
这段处理同时依赖模型配置、用户、消息存储、事件发射与中间件。
[main.py L1820–1930](https://github.com/open-webui/open-webui/blob/f7ac7ee10962ddec0017f5c29b6740d9b9c5faca/backend/open_webui/main.py#L1820)

### 活动视图与最终保存分开

活动响应保存快照；Redis路径允许附加增量。读取会话时，路由把活动响应覆盖到数据库会话视图，
因此重新打开页面可以取得正在生成的输出，而不必重新提交模型请求。
它不是把每个token都写入最终消息表，也不证明进程重启后会恢复原来的执行。
[tasks.py L204–309](https://github.com/open-webui/open-webui/blob/f7ac7ee10962ddec0017f5c29b6740d9b9c5faca/backend/open_webui/tasks.py#L204)、
[chats.py L1380–1406](https://github.com/open-webui/open-webui/blob/f7ac7ee10962ddec0017f5c29b6740d9b9c5faca/backend/open_webui/routers/chats.py#L1380)

最终正常完成路径先等待保存最终输出，再清除活动快照、发完成事件。
取消路径关闭上游响应迭代器，将未完成的输出/工具项标为incomplete，并通过shield尽力保存取消时的内容。
这一小组机制可以共同化用：生成中视图、保存结果、资源关闭和取消后的可见状态必须协调。
不能只抄一个`task.cancel()`。
[middleware.py L6332–6434](https://github.com/open-webui/open-webui/blob/f7ac7ee10962ddec0017f5c29b6740d9b9c5faca/backend/open_webui/utils/middleware.py#L6332)

### 事件并非统一事务日志

事件发射器将身份绑定到chat/message，再按事件类型处理状态、来源、文件和消息。
该函数中socket发射发生在其后部分数据库更新之前。因此“有事件系统”不等于所有事件都已持久提交，
也不等于断线客户端有可按序补读的持久事件日志。
[socket/main.py L1596–1764](https://github.com/open-webui/open-webui/blob/f7ac7ee10962ddec0017f5c29b6740d9b9c5faca/backend/open_webui/socket/main.py#L1596)

删除路由先做权限检查，再停止该会话任务及相关子会话任务，最后删除数据。
这与BGM“先删除事实来源，再拒绝迟到提交”的次序不同；是否采用必须重新证明竞争下的不复活保证。
[chats.py L1677–1717](https://github.com/open-webui/open-webui/blob/f7ac7ee10962ddec0017f5c29b6740d9b9c5faca/backend/open_webui/routers/chats.py#L1677)

## Khoj：必须保留的语义差异

聊天生成器直接拥有HTTP/WebSocket对象。HTTP断开监测会设置取消事件，并对中断材料保存使用shield；
WebSocket通过中断队列处理停止、新指示与断开。它的连接与取消合同不等于BGM当前的“连接断开但轮次继续”。
生成器在取消检查点退出；普通流输出结束后通过后台任务保存会话，不等待这项保存结束才发送结束事件。
两项行为都不能在不改产品合同的情况下照搬。
[api_chat.py L750–841](https://github.com/khoj-ai/khoj/blob/ae229ca894c0b80ad84664afcfdde523b5e87057/src/khoj/routers/api_chat.py#L750)、
[api_chat.py L1381–1486](https://github.com/khoj-ai/khoj/blob/ae229ca894c0b80ad84664afcfdde523b5e87057/src/khoj/routers/api_chat.py#L1381)、
[api_chat.py L1522–1589](https://github.com/khoj-ai/khoj/blob/ae229ca894c0b80ad84664afcfdde523b5e87057/src/khoj/routers/api_chat.py#L1522)

保存函数把轮次ID、引用、在线/代码/操作/研究材料和其他元数据组织进对话记录，通过数据库Adapter写入；
之后还可能更新长期记忆和Prompt trace。可借鉴“材料与轮次一起关联”的数据组织，
但该函数直接依赖Django模型、账户、Adapter及其他后处理，不能作为轻量保存SDK引入。
[conversation/utils.py L541–624](https://github.com/khoj-ai/khoj/blob/ae229ca894c0b80ad84664afcfdde523b5e87057/src/khoj/processor/conversation/utils.py#L541)

## 与BGM当前合同的适用关系

| 机制 | 可采用的方式 | BGM需保持或先决定的合同 |
|---|---|---|
| 会话/消息/执行身份分开 | 可采用设计约定；已有session_id/request_id可映射，不必照搬表结构 | 每类状态有明确的权威拥有者；外层网页任务与内层Agent任务若分层，必须关联身份并传递取消，不能形成两套冲突的事实来源 |
| 后台任务独立于交互连接 | Open WebUI交互路径适合作为源码参照 | 保留BGM断开连接继续运行；Khoj的断开即停止不是本项目当前选择 |
| 生成中快照＋最终保存 | 在需要流输出/进度时成组移植机制，重写适配现有Store | 明确快照是临时视图；最终成功以SQLite提交为准，不能把临时内容喂回成功历史 |
| 上游流关闭、取消状态保存 | 可用作BGM取消路径核对清单和故障测试素材 | 有界取消、迟到结果抑制、供应商调用无法物理撤销仍须保留 |
| 聊天级批量停止 | 单进程任务索引的方式可化用 | 当前BGM一会话一轮，不应为照搬而引入多模型/子Agent任务组 |
| Redis跨实例控制 | 当前不需要；若以后多进程再重新评估 | 任务发现/停止不是执行持久化，不能据此承诺重启续跑 |
| Django/SQLAlchemy、账户与全部配置 | 不直接移植 | BGM基础模式SQLite与自备模型API的安装合同保持独立 |

## 对“直接使用”的判断

1. **直接使用完整平台**：让上游拥有网页、用户、会话和运行任务，BGM成为领域扩展。
   这属于采用平台，必须明确谁拥有模型/工具循环与会话事实来源，不能与现有运行时重复执行。
2. **源码移植**：固定版本，按功能连同其依赖和失败处理抽取，保留上游来源与适配测试。
   这是维护一个本地移植版本，后续需跟踪上游修复；不是装一个包就获得上游整个生命周期保证。
3. **机制化用**：保留BGM技术栈、状态机与持久化Interface，以可观察行为和故障测试对齐成熟实现。
   与平台承载、源码移植并列评估；需要源码复用时再核对具体函数集合和许可证，不因现有代码已存在就排除平台方案。

本研究未发现可从上述两个项目直接拔出的、同时覆盖BGM现有幂等/持久终态/断线继续/删除屏障合同的轻量生命周期SDK。
这是所核对路径的结论，不是对所有成熟Agent运行框架的否定，也不证明BGM现状已优于上游。
本轮没有针对具体源码分发方案作许可判断。

## 补充：真正的Agent服务运行时是另一类采用方式

现有LangGraph图执行库和独立Agent服务运行时应分别评估。官方Agent Server提供threads/runs、
持久化与任务队列，属于能接管应用执行职责的候选类别；其文档说明核心运行数据存于PostgreSQL，
Redis承担信号、取消和流事件分发。它与向当前应用安装一个模型/工具循环SDK的采用成本不同。
[Agent Server：运行架构与生命周期](https://docs.langchain.com/langsmith/agent-server)

若未来选择这类运行时，需要明确它替换现有Conversations/轮次存储的哪些职责、现有网页怎样接入，
以及部署与许可条件；不是在现有任务管理器外再包一层就得到可靠性保证。
当前仍是候选类别说明，没有完成具体选型、部署评估或故障实测。
