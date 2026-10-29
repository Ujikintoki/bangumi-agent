# 文档地图

> 整理日期：2026-10-29。本文负责阅读顺序和维护状态；产品、工作顺序和实现细节各有唯一归属。
> 未列为持续维护的资料，不自动产生当前任务。旧资料保留原路径，供已有链接和证据使用。

## 平时只读这些

| 文档 | 唯一职责 | 什么时候读 |
|---|---|---|
| [PRODUCT](../PRODUCT.md) | 产品共识、范围和待确认的选择 | 讨论要做成什么时 |
| [ROADMAP](../ROADMAP.md) | 当前阶段、下一步和推进条件 | 回到项目、决定接下来做什么时 |
| [本机网页开发设计](product/local-web-v1-development.md) | 第一版技术契约、改动落点与验收 | 实现完整使用链路时 |
| [README](../README.md) | 配置、启动和调用 | 实际运行时 |
| [架构蓝图](architecture-blueprint.md) | 系统职责、依赖方向和请求路径 | 定义系统职责或准备跨层修改时 |
| [固定验收场景](product/fixed-agent-scenarios.md) | 主连续场景、意图/工具问题组与判定标准 | 实施领域可靠性或准备真实验收时 |
| [轮次生命周期](Architecture/turn-lifecycle.md) | 请求状态、取消、恢复及故障矩阵 | 维护执行、连接或持久化行为时 |
| [AGENTS](../AGENTS.md) | 开发规则与数据安全边界 | 执行修改前 |
| 本文 | 阅读入口、文档职责和维护状态 | 不知道该读哪份时 |

当前主线：开发第一版本机网页，在完整使用链路中补齐必要的 Agent 可靠性。
产品讨论先读 PRODUCT → ROADMAP；不需要从旧探索稿或历史 Phase 开始补课。

两轮盘点已交付；[盘点委托](codebase-inventory-brief.md)及其报告停止滚动维护。
不再以新一轮全仓盘点或产品讨论作为开始实现的前置条件。

## 按需维护的技术手册

对应模块改变时更新，不承担项目总计划。本文未逐项复验其中的运行指令；
执行仍须遵守 AGENTS，数据库手册不构成连接真实数据库的授权。

| 文档 | 责任范围 |
|---|---|
| [记忆入口](memory/README.md)、[配置](memory/configuration.md)、[调试](memory/debugging.md)、[测试](memory/testing.md) | 记忆模块说明；旧配置和测试路径使用前核对代码 |
| [RAG 概览](Rag/rag_overview.md) | 检索模块说明 |
| [数据库管理](database-admin.md) | 数据库运维参考 |
| [评测运行](../eval/README.md)、[数据可信度](../eval/DATA_PROVENANCE.md) | 评测方法和数字来源 |

## 定时点证据，停止滚动维护

保留特定时间、特定范围的观察，不随代码改写成最新状态。
新的产品决定进入 PRODUCT，当前缺口和工作顺序进入 ROADMAP。

| 资料 | 用途与限制 |
|---|---|
| [系统盘点](codebase-inventory-report.md)、[Agent 设计盘点](agent-design-inventory-report.md) | 静态追踪与隔离固定探针；不代表真实模型质量或当前测试通过 |
| [Bangumi 发言抽样](product/bangumi-usage-research.md) | 理解消息类型；便利样本不代表需求频率 |
| [OtakuNeko 研究](product/otakuneko-capability-study.md) | 产品组织参考；不直接转为开发清单 |
| [Bangumi AI 小组件研究](product/bangumi-ai-gadget-study.md) | 站内入口与服务分工参考；源码观察范围有限 |
| [后端编排修复卡](product/orchestration-repair.md) | 修复范围及验收入口；不再是当前任务卡 |
| [10 月 22 日验收](Test/acceptance-2026-10-22.md) | 修复前实测与恢复记录的限制 |
| [10 月 25 日复测](Test/acceptance-followup-2026-10-25.md) | 修复后证据、部分通过及未验证范围 |
| [C1–C5 真实链路记录](Test/c1-c5-real-trace-2026-10-29-run1.md) | 空库单次连续执行、各阶段输入和工具快照；等待分析，不作质量结论 |

## 已冻结的产品探索

- [旧产品探索锚点](product/current-discovery-anchor.md)：保留此前共识和候选；其中“下一步选择三个场景”的流程已结束，不再更新。
- [评论到草稿候选卡](product/content-to-draft.md)：未入选的候选；草稿是可选出口，不是产品固定终点。

稳定结论由 PRODUCT 接管，不再并行维护另一份总纲。

## 历史参考，不是待办

- `docs/design/` 全目录：早期设计、发布计划、架构决策和复盘。包括[七月愿景](design/claude-on-bangumi-vision.md)、[Phase 路线](design/evolution-roadmap-phase7-9.md)、[宏观决策](design/macro-architecture-decisions.md)。愿景保留方向启发；“待实施”“当前生产参数”等文字属于原记录时点。
- `docs/eval/` 全目录：历史评测设计、计划和标注报告。运行方法以根目录下的 `eval/README.md` 为入口，旧复核项不自动进入排期。
- [AUDIT](AUDIT.md)、[REVIEW](REVIEW.md)：对应提交上的审计证据；未复测前不当作当前缺陷清单。
- [CLAUDE](../CLAUDE.md)、[Copilot 兼容入口](../.github/copilot-instructions.md)：仅保留工具兼容跳转，不维护独立规则。

## 维护方式

- 产品决定写 PRODUCT；工作顺序写 ROADMAP；系统边界写蓝图；运行方法写 README 或对应手册。
- 调研和验收是定时点证据，不升级成第二份产品基线或长期 backlog。
- 新任务先引用对应问题和完成条件，必要时再写聚焦任务卡。
- 不新增 Phase 台账、重复总览或持续增长的已完成清单。
- 本次不移动或删除旧资料；直接打开旧文件时，以文首状态判断用途。

## 引用检查与本机资料

`scripts/check_doc_refs.py` 检查 Git 跟踪及未跟踪 Markdown 的路径和大小写。
严格检查范围包含部分冻结的产品资料，以保护引用；严格检查不等于仍在维护。
通过只代表引用有效，不代表内容或运行指令已经复验；未跟踪、本机忽略资料会单独提示。

Git 中目录拼写为 `docs/design`、`docs/eval`、`docs/memory`、`docs/Rag`，链接按 Git 大小写书写。
`docs/tmp/`、`docs/DEPRECATED/` 等忽略资料不保证 clone 后可得，不作为当前入口。
受保护的数据和输出目录不参与本次整理。
