# OtakuNeko 非文本能力研究

> 维护状态：定时点证据，不滚动维护；结论限于下文记录的时间和验证范围。当前范围及工作顺序见 [PRODUCT](../../PRODUCT.md) 与 [ROADMAP](../../ROADMAP.md)。

> 核对时间：2026-10-15  
> 上游版本：[`ShowayLiao/OtakuNeko@b595f03`](https://github.com/ShowayLiao/OtakuNeko/tree/b595f03239f380aa2f534eab18022eecb78302e6)  
> 研究问题：除了生成回复，哪些状态、动作、界面和外部集成值得 BGM Agent 借鉴？

## 结论

OtakuNeko 的“完整产品感”主要不是来自 Agent 会生成更多文字，而是来自四类可见能力：把当前对象带入对话、保存用户状态、让用户直接操作收藏与排班、把结果交给日历或下载器。其 Agent 本身仍只绑定七个读/分析工具；收藏写入、排班和 RSS 都由普通 API 与专用界面完成，并非自然语言 Agent 自动执行。

对本项目最合适的近期开口是：**页面/条目上下文附着、可见的来源与工具过程、用户可控的会话状态**。它们能让现有只读 Agent 变成可使用的产品，又不要求立即建设收藏数据库或外部写操作。收藏同步、排班和 RSS 是有效产品能力，但会把产品从“Bangumi 共读伙伴”扩展为“个人媒体管理器”，应由单独场景卡和真实需求决定。

## 上游代码实际具备的能力

下表只记代码中可追踪的行为；README 宣传但代码未闭环的内容另行标注。

| 能力 | 源码证据与实际行为 | 相对本项目的耦合/重量 |
|---|---|---|
| **把作品对象附着到消息** | 聊天输入可带搜索结果卡片；前端把所选实体序列化为 `<Entity id source sourceId>` 后再发给 Agent。用户不必在自然语言里重新说明当前作品。[聊天页 95–133 行](https://github.com/ShowayLiao/OtakuNeko/blob/b595f03239f380aa2f534eab18022eecb78302e6/frontend/src/components/chat/index.tsx#L95-L133) | **低到中**。现有工具已经认识 subject/person/character ID；近期只需定义上下文附件契约，Web 壳出现后再做选择器。 |
| **显示 Agent 正在调用什么** | 后端把 `tool_start`、`tool_end` 和文本片段作为 SSE 事件发出；前端维护工具调用的 running/success 状态并随消息显示。[graph.py 68–93 行](https://github.com/ShowayLiao/OtakuNeko/blob/b595f03239f380aa2f534eab18022eecb78302e6/backend/app/agents/graph.py#L68-L93) [聊天页 154–203 行](https://github.com/ShowayLiao/OtakuNeko/blob/b595f03239f380aa2f534eab18022eecb78302e6/frontend/src/components/chat/index.tsx#L154-L203) | **中**。本项目已有工具调用和 SSE 入口，但当前只交付最终事件；需要设计安全的公开事件和前端状态。与“来源可解释”直接契合。 |
| **持久会话工作区** | 前端支持新建、切换、删除、重命名会话，并用 Zustand 持久化；后端还提供指定 thread 的历史读取。[聊天页 228–255 行](https://github.com/ShowayLiao/OtakuNeko/blob/b595f03239f380aa2f534eab18022eecb78302e6/frontend/src/components/chat/index.tsx#L228-L255) [useChatStore.ts 248–300 行](https://github.com/ShowayLiao/OtakuNeko/blob/b595f03239f380aa2f534eab18022eecb78302e6/frontend/src/stores/useChatStore.ts#L248-L300) [agent.py 98–109 行](https://github.com/ShowayLiao/OtakuNeko/blob/b595f03239f380aa2f534eab18022eecb78302e6/backend/app/api/v1/agent.py#L98-L109) | **低到中**。本项目已有 session ID、L1 与互斥，缺的是用户可见的会话列表和生命周期。先做本地 Web 状态即可，不必扩展 L2。 |
| **收藏同步与可操作资料库** | API 支持筛选/排序收藏，以及从 Bangumi 同步和豆瓣上传；收藏页按类型、状态、关键词、排序展示。[collections.py 31–89 行](https://github.com/ShowayLiao/OtakuNeko/blob/b595f03239f380aa2f534eab18022eecb78302e6/backend/app/api/v1/collections.py#L31-L89) [collections.py 348–449 行](https://github.com/ShowayLiao/OtakuNeko/blob/b595f03239f380aa2f534eab18022eecb78302e6/backend/app/api/v1/collections.py#L348-L449) [收藏页 9–81 行](https://github.com/ShowayLiao/OtakuNeko/blob/b595f03239f380aa2f534eab18022eecb78302e6/frontend/src/app/collections/page.tsx#L9-L81) | **中（只读导入）/高（完整管理）**。本项目已有 `get_user_profile` 只读能力；可先让用户主动选择收藏作为推荐或讨论上下文。完整同步、编辑、数据迁移会引入账户与持久业务模型。 |
| **可拖拽、可保存的观看排班** | 时间表把收藏拖入星期/时间槽，修改观看类型、日期和时间；用户主动保存时批量 upsert 到后端。[Timetable 296–411 行](https://github.com/ShowayLiao/OtakuNeko/blob/b595f03239f380aa2f534eab18022eecb78302e6/frontend/src/app/Timetable/page.tsx#L296-L411) [TimetableHeader 47–77 行](https://github.com/ShowayLiao/OtakuNeko/blob/b595f03239f380aa2f534eab18022eecb78302e6/frontend/src/components/header/TimetableHeader.tsx#L47-L77) [schedules.py 203–287 行](https://github.com/ShowayLiao/OtakuNeko/blob/b595f03239f380aa2f534eab18022eecb78302e6/backend/app/api/v1/endpoints/schedules.py#L203-L287) | **高**。需要 Schedule 领域对象、数据库接口和复杂交互；与当前“理解、讨论、表达”核心链路距离较远。若用户明确要追番管理，应作为独立产品闭环。 |
| **把状态导出到外部工具** | 排班可生成 CSV，并提供粘贴给语音助手的提醒文本；另有 TickTick 导出入口。[TimetableHeader 102–145 行](https://github.com/ShowayLiao/OtakuNeko/blob/b595f03239f380aa2f534eab18022eecb78302e6/frontend/src/components/header/TimetableHeader.tsx#L102-L145) [ExportCalendarModal 25–75 行](https://github.com/ShowayLiao/OtakuNeko/blob/b595f03239f380aa2f534eab18022eecb78302e6/frontend/src/components/Modal/ExportCalendarModal.tsx#L25-L75) | **中到高**。导出本身简单，但前提是本项目先拥有可导出的排班/收藏状态。它不是当前 Agent 核心的自然延伸。 |
| **RSS 与 qBittorrent 自动下载规则** | 后端可添加/替换 RSS feed、设置与删除 qBittorrent 规则；收藏卡片按作品名构造 RSS 地址并打开配置表单。[rss.py 18–107 行](https://github.com/ShowayLiao/OtakuNeko/blob/b595f03239f380aa2f534eab18022eecb78302e6/backend/app/api/v1/rss.py#L18-L107) [CollectionContent 51–89 行](https://github.com/ShowayLiao/OtakuNeko/blob/b595f03239f380aa2f534eab18022eecb78302e6/frontend/src/components/collection/CollectionContent.tsx#L51-L89) [SmartSubscriptionModal 146–233 行](https://github.com/ShowayLiao/OtakuNeko/blob/b595f03239f380aa2f534eab18022eecb78302e6/frontend/src/components/Modal/SmartSubscriptionModal.tsx#L146-L233) | **高**。增加下载器、RSS 来源、凭据、路径与高影响写操作，产品定位转向媒体自动化。当前不建议纳入 BGM Agent。 |
| **用户选择模型、密钥与人格配置** | 前端本地保存 provider 配置并检查连接；用户可选择模型、选择或导入/导出自定义角色。[ApiKeyModal 107–159 行](https://github.com/ShowayLiao/OtakuNeko/blob/b595f03239f380aa2f534eab18022eecb78302e6/frontend/src/components/Modal/ApiKeyModal.tsx#L107-L159) [useRoleStore 14–53 行](https://github.com/ShowayLiao/OtakuNeko/blob/b595f03239f380aa2f534eab18022eecb78302e6/frontend/src/store/useRoleStore.ts#L14-L53) | **低到中（设置 UI）**，但产品收益有限。本项目已有模型配置与三个人格；先给自部署用户清楚的配置/状态反馈，比开放任意 Persona 更符合当前范围。 |

### README 与实现不能混为一谈

- OtakuNeko 的 Agent 工具表只有条目详情、评价、staff、cast、搜索、时间和用户画像七项，均为读取或计算；没有收藏、排班或 RSS 写工具。[graph.py 1–30 行](https://github.com/ShowayLiao/OtakuNeko/blob/b595f03239f380aa2f534eab18022eecb78302e6/backend/app/agents/graph.py#L1-L30)
- RSS 表单中的“AI 识别”没有调用模型或后端：代码等待 1.2 秒后填入固定的《迷宫饭》样例。因此只能视为界面原型，不能证明 Agent 已能生成安全可靠的下载规则。[SmartSubscriptionModal 114–143 行](https://github.com/ShowayLiao/OtakuNeko/blob/b595f03239f380aa2f534eab18022eecb78302e6/frontend/src/components/Modal/SmartSubscriptionModal.tsx#L114-L143)
- README 明确标注 V2 为早期 Beta、Bug 较多；研究结论只说明代码存在相应路径，不等于每项都经过生产验证。[README](https://github.com/ShowayLiao/OtakuNeko/blob/b595f03239f380aa2f534eab18022eecb78302e6/README.md)

## 对 BGM Agent 的建议

### 值得进入近期场景探索

1. **对象附件，而非只有文本框。** 用户在作品、角色、章节或评论旁发一句话时，系统自动带上对象 ID、页面类型和来源；用户仍可移除或更换。它使“这人是谁”“我不同意这段”成为完整输入，也复用现有只读工具。
2. **可检查的依据面板。** 回复之外显示“使用了哪些 Bangumi 条目事实、用户原文和社区样本”，工具过程只展示对用户有意义的状态。它直接落实产品基线中的四类内容边界。
3. **会话成为可回到的工作区。** 用户可以命名、继续和删除一段作品讨论；当前条目、剧透范围和用户明确表达的立场作为可见会话状态，而不是隐含在长对话里。
4. **收藏作为用户主动选择的上下文。** 第一版只读取用户选择的收藏或观看状态，用于“从我想看里挑一部”“别剧透我没看完的部分”。暂不建设完整收藏镜像和写回。

这些能力并非都要由 LLM 决定。对象选择、会话管理、来源展示和确认按钮更适合确定性的 UI/Application 层；Agent 只负责解释、判断和提出候选动作。

### 暂不进入近期范围

- 完整收藏 CRUD、数据库镜像与跨平台导入；
- 可视化拖拽排班、日历/TickTick 导出；
- qBittorrent/RSS 下载自动化；
- Agent 自动代表用户收藏、评分、发帖或下载。

它们都有真实价值，但会建立新的业务对象、外部凭据和写操作边界。若未来选择其中之一，应单独写“问题—选择—验收”卡，不能作为聊天能力的顺手扩展。

## 与当前仓库的关系

当前 BGM Agent 已有只读 Bangumi 工具、L1/L2 会话能力、人格渲染和 `/chat`/`/chat/stream`，但没有产品 UI、用户可见会话管理或 Bangumi 写操作；工具层明确把能力限制为读取（搜索请求在 HTTP 层可使用 POST，但没有改变外部状态，见 [`tools/bgm_tools.py`](../../tools/bgm_tools.py)）。因此，最小产品化顺序可以是：

```text
当前页面/对象附件
  → 现有 Agent 查询与讨论
  → 来源与工具状态可见
  → 会话可继续、可删除
  → 有真实需求后再评估收藏上下文
```

这条顺序主要“暴露和组织”已有能力，修改面小于复制 OtakuNeko 的收藏、排班和下载系统。

## 授权与复用边界

上游 README 声称项目采用 MIT License，并链接到仓库根目录的 `LICENSE`。[README License 段](https://github.com/ShowayLiao/OtakuNeko/blob/b595f03239f380aa2f534eab18022eecb78302e6/README.md#L594-L599) 但在本次核对的提交 `b595f03` 中，Git 跟踪文件里没有 `LICENSE`。因此：

- 产品概念、交互模式和独立实现可作为研究参考；
- 在许可证文件和版权声明得到核实前，不应直接复制其源码或素材；
- 即使后来确认 MIT，也应保留要求的许可证与版权声明，并优先按本仓库架构重新实现。

本报告没有复制 OtakuNeko 代码，只记录源码可验证的行为与对本项目的取舍建议。
