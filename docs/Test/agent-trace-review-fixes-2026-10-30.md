# 独立核对缺陷修复验证

日期：2026-10-30。依据[独立审查](agent-trace-independent-review-2026-10-30.md)及随后三次只读复现。
原审查与冻结示例保持不变；本记录说明修复后的验证，不改写历史证据。

| 项目 | 修复与回归 |
|---|---|
| 异常日志脱敏 | 标准异常、异常链、已有 exc_text、stack_info 四组回归先失败；统一清洗正文与附加通道，保留类别/定位，清除 LogRecord 中可被 Formatter 再次使用的原始异常 tuple。使用合成已知凭据、Bearer、sk 模式，未接触真实密钥 |
| 单轮完整性 | 其他轮次 incomplete/running/内容到期三组 HTTP/导出回归先失败；筛选下沉到 Runtime.query，在 artifact 检查及完整性聚合前执行。当前完整轮返回 true，无记录及当前缺口返回 false；进程 dropped 后仍保守返回 false |
| Prompt 版本 | 四个正式工厂阶段的版本回归与自定义调用 unavailable 回归先失败；工厂写入公开 source bundle 版本，不对私有输入生成裸 hash。同阶段不同输入版本相同，公开源码变化在重新加载后改变版本；来源缺失时显式 unavailable，模型仍只执行原调用并正常返回 |

最终通过 `scripts/test_offline.py` 审阅入口：622 passed，两个既有警告。
全部使用固定 SDK 替身、ASGI 请求和隔离临时存储；禁外部网络、禁 PostgreSQL、不加载开发者 .env。
没有新增 Prompt 文案、改变模型输入、工具调用或生命周期策略，没有执行付费供应商或真实数据库请求。

重新运行最初只读复现脚本三次：六种日志情形的 leaks 均为 false；仅包含完整 good 轮的 HTTP/导出均为 true，dropped=0。
未知模板来源明确保留原因，不凭模型消息或阶段名称猜测模板；源码不可用会标记采集缺口，不能拖垮业务。
这些验证不代表已完成真实供应商、所有第三方日志 handler 或高负载验收；已知凭据与有限模式脱敏也不保证任意自由文本完全匿名。

当前维护语义见[Trace 维护合同](../Architecture/agent-trace-design.md#日志异常通道查询范围与模板版本)。
