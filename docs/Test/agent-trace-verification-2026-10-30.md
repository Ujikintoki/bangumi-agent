# Agent Trace 离线与隔离启动验证

日期：2026-10-30。范围为本机单进程采集、材料可见性和生命周期合同；不评价真实模型回答质量。
实现和日常维护以[原设计合同第 13 节](../Architecture/agent-trace-design.md#13-当前维护入口2026-10-30)为准。
本次没有调用付费模型、Bangumi API、embedding 或真实 PostgreSQL，没有修改迁移前冻结证据。

## 验证入口及结果

| 验证 | 本次结果与边界 |
|---|---|
| `TIKTOKEN_CACHE_DIR=/tmp/bgm-trace-tokenizer python scripts/test_offline.py` | 干净临时开发环境运行，608 passed；原有 pytest 插件重复导入与 datetime.utcnow 弃用警告各一类。入口禁止外部网络与 PostgreSQL、不加载开发者 .env；新增测试全部使用临时 Trace/会话存储 |
| `node --test test/web_connection.test.cjs` | 通过；连接、草稿、恢复和轮询语义未改 |
| `python test/lifecycle_http_probe.py` | 通过；真实随机回环端口、临时数据、替身执行，覆盖响应前断连、查询/重放、取消、强杀/重启、Trace 查询、scope 正文与删除 |
| 干净安装 | 用原锁文件离线建立两套临时环境：基础 `uv sync --offline --frozen --no-dev`，以及带 reviewed 单测依赖的 `--all-groups --extra local-data`。开发环境通过上述审阅入口；基础环境不含 sqlalchemy、psycopg2、pytest，仍通过完整 HTTP/进程探针 |
| CLI | 活 writer 上授予限时 scope、导出指定请求均通过；活 writer 上手动 prune 被拒绝，停止后 prune 通过。CLI 不启动第二套 writer，也不把活跨度改为 interrupted |
| `python scripts/benchmark_trace.py` | 本机 300 次“跨度开始 + 元数据事件 + 结束”，p50 0.111ms、p95 0.118ms，关闭排空约 581ms，dropped=0，导出完整。满足本次 2ms / 1s 目标，不外推到高负载或所有文件系统 |

公开 tokenizer 缓存在测试前单独准备；运行审阅入口时始终禁网。
启动探针只放行本机回环网络，执行器是固定无付费替身，不把 HTTP 可用视为模型质量通过。

## 关键边界的证据

| 边界 | 固定验证 |
|---|---|
| 正常、异常、取消、超时 | 实际 SDK Callback + FakeMessagesListChatModel；Event 控制等待，模型失败不计为零 token，取消原样传播 |
| off / metadata / diagnostic | 比较实际模型消息、裁剪材料、模型次数及持久结局；三种模式一致。DEV_MODE 对照证明均调用普通 ainvoke，不走旧 state 合并路径 |
| 绑定、重复、并行 | 绑定工具定义可见；共享模型的两会话不串 Trace，两个并行工具为兄弟跨度；幂等重放、Callback 重复终结、旧工具重复包装不增加调用数；保留调用者 CallbackManager |
| usage | 单一模型跨度提供 usage；100 input 中含 80 cache_read 的夹具仍统计为 100，不变成 180；缺失值保留 null |
| 工具校验与结果 | ToolNode seam 覆盖参数校验失败与实际成功；结构化错误/部分返回沿用旧分类，校验与 actual_execution 分开 |
| 提交重试 | 写入前失败、已写入但读回确认失败均验证只重试提交、不重跑模型；提交跨度保留各次尝试 |
| 物理停止 | 超时已提交失败、执行器吞取消继续等待时根跨度保持 running，业务仍占用；退出后才出现 execution.quiesced，迟到回复不提交 |
| 删除/重启 | 删除屏障拒绝在途和重启后的迟到写入；物理清理失败时先屏蔽正文查询，启动继续清理；第二个 writer 无法误恢复活跨度 |
| 队列、存储、SDK | 有界队列/字节限额、总量不足、不可写 sink、关闭超时与 processor 抛错均不抛入业务；健康或记录明确不完整 |
| 内容、隐私与归属 | 单份/每轮内容限额、scope 到期、禁用时 lazy capture、未知对象拒绝、字段与已知秘密脱敏、隐藏内容块排除、600 文件权限；不同 owner 和跨站 HTTP 请求不能读取正文 |
| 保留与 schema | 7 天正文到期变 expired，30 天元数据清理；未来 SQLite schema 拒绝处理；导入不打开存储 |

实际锁定 SDK 的取消缺口及最小补齐入口记录在原合同；没有全局 patch SDK，没有增加重试器或第二套指标采集器。
正式处理链路使用显式 OTel provider 与 LocalProcessor，开始、即时事件和结束按相同跨度 ID 合并；摘要来自这些记录。

## 合成诊断示例怎么读

[正式链路生成的 JSON](agent-trace-synthetic-example.json) 包含 21 个跨度、4 次固定模型调用和 1 次固定工具调用，recording_complete=true。
生成入口：

```bash
TIKTOKEN_CACHE_DIR=/path/to/prepared/cache python scripts/trace_synthetic.py --output /private/tmp/new-trace-example.json
```

脚本不加载 .env，不访问网络；它直接使用 SDK 固定模型与 ToolNode、正式 Callback / Processor、Conversations 和临时 SQLite。
固定的分类、材料选择和 Render 文本只是夹具，不是业务策略改动，也不证明真实模型会作出相同选择。
usage 是夹具提供的合成值，不是供应商账单。采集时工作区尚未提交，versions 如实带 dirty=true；示例对应本次交付源码。

沿同一 trace 查看：

1. `context.assemble` 的 assembled_messages 包含无关旧历史和 C2/C3 原评选择、C4 演出纠正、C5 引文/用户立场区分的合成输入。
2. `context.prepare` / `context.selection` 显示旧历史如何离开窗口，before/after、omitted 和 opaque message_id 对齐；SystemMessage 保留。
3. `model.invoke` 的 stage=reasoning#1：model_input 是裁剪后的实际消息，tool_definitions 和 model_parameters 是当次绑定材料；返回 opinion-call。
4. `tool.execute` 的 tool_call_id=opinion-call：参数 subject_id=100，返回合成评论 11/12；作者、原文、定位与 truncated 都在 tool_result。合成密钥字段被过滤并带 redacted 标记。
5. stage=reasoning#2：model_input 含同一 tool_call_id 的材料，可查评论实际是否进入模型，而非靠回答关键词猜测。
6. `response.render` 明确夹具选择 comment:11、排除 comment:12；stage=render 的实际消息与 reasoning 输入分别保存；render_final 是最终可见文本。
7. `session.commit` 的 committed_result 和根跨度的 committed_at / accepted_to_commit_ms 证明保存的内容与时点；execution.quiesced 表示执行器已退出，不与保存混同。

opaque ID 不使用用户文本裸 hash；自由文本引用归属不能确定时保持 unknown。
示例只含合成材料；旧 C1–C5 unknown 阶段没有被补造新跨度。

## 冻结证据大小核对

只读取 `.local-data/acceptance/c1-c5-20261029-run1/C*/` 下原 JSON 快照的文件大小，没有读取密钥、改写冻结文件或重跑题目。
不含 turn.json 的最大单份/每轮快照总字节：C1 38,325 / 88,033；C2 40,285 / 127,700；
C3 47,042 / 169,365；C4 39,909 / 116,277；C5 9,290 / 15,948。
据此保留 512KiB 单份、16MiB 每轮的候选默认值；新链路的阶段快照更多，不能将旧总量冒充新真实 Trace 的实测大小。
超限始终以 truncated / recording_complete=false 表达，不承诺任意长对话都完整保存。

## 尚未覆盖与使用限制

- 未复跑真实供应商模型/API、供应商内部重试、流式断网或计费；provider_attempts 为 unknown，不提供费用或虚构 TTFT。
- 未进行真实磁盘耗尽/损坏、系统休眠、崩溃断电、跨平台文件系统与退出信号实验；I/O 故障主要由注入验证。
- 基准为本机小规模固定工作负载，不是持续高并发吞吐承诺。
- 脱敏只识别结构字段、已知凭据和有限模式，不能保证任意自由文本完全匿名；诊断导出需要由本机使用者妥善保管。
- 正式会话删除清理规范观测存储；主动冻结的旧验收包、导出及备份须由使用者另行处理。
- Trace 记录程序选择与实际可见输入，不自动证明材料可靠、模型理解正确或产品场景通过。
