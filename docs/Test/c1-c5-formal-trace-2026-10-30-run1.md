# C1–C5 正式 Trace 真实连续执行 · 2026-10-30 · Run 1

状态：已执行并冻结，留待后续定位与策略分析。本轮不改 Prompt、上下文选择、工具能力或 Render。
题目来自[固定场景](../product/fixed-agent-scenarios.md)，五题原样在同一新会话连续发送；每题一次，失败不补问、不重跑。

## 修复复验与执行条件

- 受测提交：`086c8bd21a75210d55ac3eda8096b7a8719aa4b9`，执行时 dirty=false。
- 日志异常/堆栈脱敏修复 `5c43591`、按请求判定完整性修复 `f9bac10` 已静态核对并用原最小复现反向验证通过。
- `086c8bd` 已补公开 Prompt 源码版本与缺失原因；本轮18次模型调用均为 prompt_version_status=available。
- 审阅入口 `scripts/test_offline.py`：622 passed、2个已知警告；包含异常链/缓存堆栈、HTTP/导出请求范围和 Prompt 版本回归。
- 用户明确授权修复核对后执行真实调用。使用正式 `scripts/trace_fixed_scenarios.py`，未安装临时 monkey-patch。
- 独立8011端口、空会话库和观测库；不清空已有服务或旧证据。执行结束已停止本轮服务并排空 writer。
- fast / bangumi，DEV_MODE=false，RAG/L2关闭，匿名本机单进程；会话背景始终为空。
- 请求模型 deepseek-v4-flash，供应商响应 model_name=deepseek-flash；名称差异如实保留，不推测供应商内部映射。
- 正常应用读取已有模型配置；没有读取展示 .env 或凭据。真实模型及 Bangumi 只读工具均未替换为夹具。
- 诊断 scope 在首轮提交前授予600秒；执行约在上海时间13:07–13:08，各事件UTC时间在原文件中。

## 程序结果与采集结果

| 场景 | 持久终态 | 图意图 | 程序资料结局 | 模型 / 工具数 | 等待 ms | 跨度 / 内容快照 | Trace完整 |
|---|---|---|---|---|---|---|---|
| C1 | completed | explore | success | 4 / 2 | 6785 | 20 / 41 | true |
| C2 | failed，reason=execution | discuss | failed | 3 / 1 | 5156 | 18 / 31 | true |
| C3 | completed | discuss | partial，缺board | 5 / 2 | 14861 | 25 / 50 | true |
| C4 | completed | explore | partial，缺board | 4 / 1 | 7175 | 19 / 37 | true |
| C5 | completed | chat | success | 2 / 0 | 2058 | 8 / 15 | true |

总计90个跨度、18次模型调用、6次工具调用、174份captured快照；dropped=0，没有孤立父引用。
Trace完整不表示产品通过：C2完整记录了一次失败，C5也可能在程序成功时违背用户立场约束。
所有已调用模型均记录实际stage、Prompt版本、准备输入与可见输出；没有补造未执行的Render模型调用。

供应商返回的usage合计：input=85,327，output=1,657；其中cache_read=70,400，已包含在input中，不再相加。
未计算费用，不把usage当作供应商账单；未核验供应商内部尝试数或计费行为。

## 每轮观察与可追溯入口

### C1：评分、热度、前作比较

执行 search_bangumi_subject 与 get_trending_subjects；阶段为 classify、reasoning#1、reasoning#2、render。
第二次推理实际输入含2条工具消息，最终回复给出续作5.52/#9392、前作8.28/#98、趋势第三，
同时加入导演变化与口碑判断。数字和解释的正确性须分别分析，未独立核验外部页面真值。

证据：[轮次](../../.local-data/acceptance/c1-c5-20261030-formal-run1/C1/turn.json)、
[单轮正式Trace](../../.local-data/acceptance/c1-c5-20261030-formal-run1/C1/trace.json)。

### C2：口碑讨论失败，但失败现场完整

reasoning#1提出 get_subject_opinions(subject_id=0)，工具出现structured_tool_error；下一次推理收到工具消息。
reasoning#2的可见输出承认未确认条目ID、无效ID查询失败，并提出重新搜索；本轮并未实际执行新的搜索。
最终持久结果为failed，面向用户的是站内访问失败的通用话术，没有走新的Render模型调用。

这些是可见链路事实，不据此直接认定哪条Prompt或哪个模块是最终根因。
工具span正常返回结构化错误，因此span.status=success；其result_status=partial，应用资料结局为failed，
三种字段不是同一含义，后续分析不要只看跨度颜色。

证据：[轮次](../../.local-data/acceptance/c1-c5-20261030-formal-run1/C2/turn.json)、
[单轮正式Trace](../../.local-data/acceptance/c1-c5-20261030-formal-run1/C2/trace.json)。

### C3：具体原评可定位

先搜索，再调用 get_subject_opinions(subject_id=513878, limit=15)。
reasoning#3实际输入含2条工具消息；最终选择“面包房少女”重写主角故事的评论，并附该条原评定位。
程序记录评论返回15条、整理保留5条、board未覆盖。与前次Run1材料采样不同，不据此宣告策略改进。

证据：[轮次](../../.local-data/acceptance/c1-c5-20261030-formal-run1/C3/turn.json)、
[单轮正式Trace](../../.local-data/acceptance/c1-c5-20261030-formal-run1/C3/trace.json)。

### C4：本次接住了原评指代

调用同条目的opinions工具。回复明确指出上一轮那条是剧情重写、没有讨论演出，
随后区分其他评论对音乐、画面和分镜的看法。这次原评对象保持一致；不外推到其他歧义输入或多次稳定性。

证据：[轮次](../../.local-data/acceptance/c1-c5-20261030-formal-run1/C4/turn.json)、
[单轮正式Trace](../../.local-data/acceptance/c1-c5-20261030-formal-run1/C4/trace.json)。

### C5：用户立场仍被扩写

chat路径只调用分类与Render，没有工具。最终两句为：

> 演出和表达撑不住，剧情本身不是问题所在。
>
> 音乐和音效几乎没起到该起的作用，情感累积被拖垮了——问题出在怎么讲，不是讲了什么。

用户没有明确认领音乐/音效的具体判断；这部分来自前轮其他评论的讨论。
程序success不能作为“没有混入他人观点”的证明，后续可沿Render输入与输出分析首次偏差位置。

证据：[轮次](../../.local-data/acceptance/c1-c5-20261030-formal-run1/C5/turn.json)、
[单轮正式Trace](../../.local-data/acceptance/c1-c5-20261030-formal-run1/C5/trace.json)。

## 冻结材料与限制

私有证据目录：`.local-data/acceptance/c1-c5-20261030-formal-run1/`，不进入Git。

- `manifest.json`：原始题目、采集方式和scope。
- `run-metadata.json`：代码/依赖版本、模式、模型响应名称及授权范围。
- `C1…C5/turn.json`：原题、请求接受、耗时和持久结果；`trace.json`为各轮脱敏导出。
- 根 `trace.json` / `session.json`：整场导出与业务会话；`review-summary.json`保留本次核对摘要。
- `driver-source.py`：实际执行的正式验收脚本副本；`progress.log`、`service.log`、`offline-regressions.log`保留执行证据。
- `observability/`：正式规范存储；`sha256.json`冻结其余196份文件的摘要。

详细Prompt、社区材料和会话保留在本机私有包；使用者应按诊断资料管理，不将脱敏等同于完全匿名。
主动冻结的证据包与导出不在普通会话删除范围内。

原2026-10-29证据保持不变。真实数据、采样和模型输出会变化；两次运行不是只改变单一变量的质量对照。
本轮目的为验证正式Trace真实可用并固定新现场；回复策略优化和根因判断留待后续分析。
