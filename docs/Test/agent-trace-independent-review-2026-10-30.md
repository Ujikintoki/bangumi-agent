# Agent Trace 独立静态核对与动态复验

日期：2026-10-30。受测提交：`34e718d`；开始时工作区干净。
对照[设计与维护合同](../Architecture/agent-trace-design.md)、[原验证报告](agent-trace-verification-2026-10-30.md)
和[合成示例](agent-trace-synthetic-example.json)。本轮仅测试与记录，没有修复生产实现。

## 结论

统一观测已实际接入应用、模型 SDK、工具和持久化，具备继续诊断固定场景的基础。
本次复跑与原报告的主要结果一致；另确认两项缺陷，因此不判定为无缺口完成：

1. 异常日志的堆栈可以绕过已知凭据脱敏，应先修复再扩大详细诊断使用。
2. 单轮查询和导出的完整性沿用整个会话的结果，会把其他轮次的缺口归到当前轮次。

Prompt 模板版本尚未实际填充，属于设计合同的部分实现；不能由 schema 中存在允许字段推断为已采集。

## 静态核对

模块依赖符合本次主线：中立运行时不导入 AgentState、LangChain 或 HTTP；SDK adapter 在 Agent 层，
业务会话库与诊断库分离，devtools 从统一记录派生摘要，普通执行不再为 telemetry 切换图路径。
本轮没有发现第二套独立 token collector。该结论限于上述 trace 实现及调用点，不是全仓标准审查。

已交付与重新生成的合成示例均有21个跨度、4次模型调用、1次工具调用；
父引用全部存在，无遗留 running，recording_complete=true。示例内容明确为合成夹具。
它验证采集和关联，不证明生产节点选择了正确材料或真实模型保持了用户立场。

## 动态复跑结果

所有测试不读取开发者 .env；使用临时会话/观测存储，没有接触受保护目录、真实 PostgreSQL、
Bangumi API 或付费供应商。测试不停止或清空用户当前服务。

| 验证 | 本轮实测 |
|---|---|
| 审阅离线入口 | 608 passed，两个已知警告；无外部 I/O 尝试 |
| 网页连接测试 | 6 passed |
| 原合成导出脚本 | 新导出成功，21个跨度、4次模型、1次工具，完整 |
| 基础环境真实 HTTP/进程探针 | 断连、结果查询、幂等重放、取消、强杀重启、归属查询、限时内容采集和删除通过 |
| 元数据基准 | 300次，p50约0.113ms、p95约0.150ms，排空约670ms，dropped=0、完整 |
| 正常应用 chat 路径 | 实际 lifespan、会话接口、分类节点和 Render；8个跨度、2次真实 SDK 调用，完整 |
| 正常应用 ReAct 路径 | 实际分类、推理、ToolNode、Render、保存；19个跨度、4次 SDK 调用、1次工具，完整 |

本轮复用了已有锁定临时环境，没有重新做干净安装。
基准是同类小规模替身工作负载，不外推到长期存储、高负载和所有文件系统。
文档引用检查另报告3处既有维护入口链接为 BROKEN；目标文件和章节实际存在，
检查器未分离 Markdown 的 #fragment，将其当作文件名。这属于检查工具误报，本轮新增链接没有引入断链。

额外应用探针使用本地随机端口的 OpenAI 兼容接口替身，模型工厂、ChatOpenAI 和生产节点保持实际调用。
ReAct 仅把工具注册/取数换成合成 get_subject_opinions，避免真实 Bangumi 请求；没有改生产文件。
socket guard 拒绝非回环连接及5432，应用 HTTP 路由通过 ASGI 执行；真实 TCP/独立进程边界由上一项探针覆盖。

chat 两次调用的 trace 消息正文逐项等于接口实际收到的正文。
合成 usage 为 input=100、output=10、cache_read=80，记录仍为100输入，没有相加为180。
ReAct 阶段依次为 classify、reasoning#1、reasoning#2、render；唯一工具返回中的合成原评确实出现在
reasoning#2 的 model_input，证明正式链路能够观察“工具返回是否进入下一次模型输入”。
这里的输出和 usage 都是替身数据，不是模型质量或供应商计费结论。

## 发现一：异常堆栈绕过日志脱敏

优先级：P1（敏感信息边界）。位置：core/observability/runtime.py 的 CorrelationFilter.filter，
main.py 的日志 handler 使用普通 logging.Formatter。

filter 清洗 record.getMessage()，但未处理 exc_info、已缓存的 exc_text 或 stack_info。
标准 Formatter 会在 filter 之后追加异常堆栈。因此 logger.exception 即便正文已脱敏，
异常文本内的同一个已知凭据仍原样输出。应用确有 logger.exception 调用点。

本轮使用合成凭据复现：normal_message_redacted=true，exception_traceback_leaks_known_secret=true。
没有使用真实密钥，也没有把合成异常正文保存到证据日志。

最小复现（仓库 Python 环境中执行）：

```python
import io, logging
from core.observability import Runtime, CorrelationFilter
buffer = io.StringIO()
handler = logging.StreamHandler(buffer)
handler.addFilter(CorrelationFilter())
handler.setFormatter(logging.Formatter('%(message)s'))
logger = logging.getLogger('trace-review-repro')
logger.handlers = [handler]
logger.propagate = False
logger.setLevel(logging.INFO)
credential = 'SYNTHETIC_REVIEW_CREDENTIAL_123'
runtime = Runtime(secrets=[credential])
with runtime.turn('u', 's', 'r'):
    try:
        raise ValueError(credential)
    except ValueError:
        logger.exception('credential %s', credential)
assert 'credential [REDACTED]' in buffer.getvalue()
assert credential in buffer.getvalue()  # 当前缺陷：泄漏来自堆栈
runtime.close()
```

修复验收：保留异常类别和必要定位信息，对最终格式化日志覆盖异常链、缓存异常文本、stack_info，
合成已知凭据与 Bearer/sk 模式均不得出现；保持记录关联字段和业务异常传播不变。

## 发现二：单轮完整性被其他轮次污染

优先级：P2（诊断结果准确性）。位置：api/observability.py 的 inspect，以及
core/observability/maintenance.py 的 export。

两处均先查询全会话并计算完整性，再过滤 request_id，却沿用过滤前的 recording_complete。
在同一会话先创建有明确缺口的 bad 轮，再创建正常 good 轮；无队列丢失：

```json
{
  "only_good_records": true,
  "all_good_records_complete": true,
  "http_complete": false,
  "export_complete": false,
  "dropped": 0
}
```

这与文档已声明的“进程级 dropped 保守传播”不同：本例 dropped=0，缺口只在另一条 trace。
历史诊断内容到期或某轮中断后，后续独立完整轮次仍可能被误判，影响固定场景的逐轮验收。

核心复现：

```python
import tempfile
from core.observability import Runtime
from core.observability.maintenance import export
with tempfile.TemporaryDirectory() as directory:
    runtime = Runtime(directory)
    runtime.start()
    with runtime.turn('u', 's', 'bad') as scope:
        scope.incomplete('fixture_gap')
    with runtime.turn('u', 's', 'good'):
        pass
    runtime.flush()
    result = export(runtime, 'u', 's', request='good', content=False)
    assert all(r['recording_complete'] for r in result['records'])
    assert result['recording_complete'] is False  # 当前缺陷
    runtime.close()
```

修复验收：HTTP和CLI统一在选定请求范围内判定完整性，同时保留确实适用的进程级故障信号；
补“其他轮次 incomplete/running/内容过期、当前轮完整”的组合回归，不简单移除全局故障提示。

## 设计合同的维护缺口

ModelObserver 当前保存工具 schema_hash，根跨度保存代码版本/dirty/依赖版本；
prompt_version 只有属性白名单声明，没有赋值调用点。原示例4个模型跨度均未提供该字段。
后续应为公开模板提供稳定版本或注明明确不可用原因；不要对用户实际输入生成可公开比较的裸 hash。
当前诊断模式仍能看到实际消息，因此这项不否定已有材料诊断能力。

## 证据与后续推进

本机证据包保存在 `.local-data/acceptance/trace-review-20261030/`，不进入 Git；包含本次临时应用探针源码、
结构化结果、运行日志、新合成导出与离线/网页测试输出。这里只含替身材料。
该包属于主动冻结的验收证据，不随测试中的临时会话删除。

建议先修两项已复现缺陷并补回归，再开启 C1–C5 的正式真实调用对照。
本轮没有执行付费供应商、真实 API、真实流式断网、磁盘损坏/耗尽或高负载测试，
不以本机替身运行代替这些边界的验收。
