# 后端交接修复与 HTTP 复测

日期：2026-10-25（Asia/Shanghai）。基线：`94f0f26`；修复提交：`c55c7d4`。
本次记录的是代码边界修复和有限产品验收，不推断总体成功率。

## 可复现入口

离线：`TIKTOKEN_CACHE_DIR=/private/tmp/bgm-agent-tiktoken .venv/bin/python scripts/test_offline.py`。
最终结果：544 passed，16 条既有警告，未报告外部 I/O 尝试。

有限真实 HTTP：`TIKTOKEN_CACHE_DIR=/private/tmp/bgm-agent-tiktoken .venv/bin/python scripts/verify_chat_http.py`。
新会话 S3 对照加 `--independent-only`。脚本会产生正常模型费用，读取 Bangumi 公开内容。
它启动临时 loopback 服务，通过真实 HTTP 调用 `/chat` 和 `/chat/stream`，关闭 lifespan，
禁用长期记忆，并阻断 psycopg2 连接；不运行初始化、迁移或真实数据库验收。
输出不含匿名续聊凭据或配置秘密；保留评分记录与所选原评的脱敏工具对照。

## 已修复并有回归的边界

- Pipeline 与 ReAct 模型异常均以空消息和结构化失败交接；失败路由不重放旧工具调用。
- 最终提取/替换局限于本轮，带 tool_calls 的 AI 消息不会被当作最终回复或被覆盖。
- 重复调用熔断在 HumanMessage 边界重置；用户下一轮可重新读取同一对象。
- Render 独立接收最近三组可见对话，包含对象与用户纠正；历史助手正文不标为本轮事实。
- 执行状态与任务验收分开：成功也只标 task_status=unverified；空结果、生成失败、
  无摘要、未知引用、HTTP/SSE 执行异常有明确缺失状态。
- comments 原文与来源保留，推理/Render 的材料按记录预算缩减，保留有效 JSON。
  补注区分获取记录数、整理保留数和原文不完整；返回记录数不是全站评论总数。
- 引用通过 comment:ID 绑定；未知 ID、缺作者/URL 或不完整引用标记不能静默留下无来源引文。
- get_blog 加入 discuss/explore 白名单；客户端保留全文，超预算上下文标明 excerpt/truncated。
  该路径有固定材料回归，尚无真实授权日志的全文产品验收。

## 真实 HTTP 证据

默认 fast / bangumi / anonymous，三条主问题保持原题；同一匿名 session 连续执行。
共运行三组连续请求，另有独立 S3 对照。以下数字取最后一组，避免把多次结果挑选拼接为一次成功。

| 用例 | 耗时 | 实际工具 | 工程结果 | 产品判定 |
| --- | --- | --- | --- | --- |
| S1 评分、热度、前作比较 | 5624ms | search_bangumi_subject | partial，recent_trend 缺失 | 部分通过：评分比较可核对，趋势未完成 |
| S2 口碑感受 | 6828ms | opinions + detail | partial，board 未覆盖 | 部分通过：取得短评、可定位多条来源；作品归因仍需单独核对 |
| S3 选择有梗原评 | 7072ms | opinions | partial，board 未覆盖 | 部分通过：主要原评可核验；额外比较及纠错表述未全部验证 |
| 纠正后 SSE 续聊 | 本轮未输出独立计时 | opinions | partial，board 未覆盖 | 连续性通过：围绕表达手法继续分析，不再被跨轮熔断 |
| S3 独立会话对照 | 8569ms | search + opinions | partial，board 未覆盖 | 主要原评与作者、定位可核验；回复未出现同会话的季数纠错 |

S1 本轮工具原始记录与最终答案一致：条目 309311 为 8.28 分、排名 98；条目 513878 为
5.53 分、排名 9293。最后一组没有调用 trending，回复明确承认热度未取得。第二组实际调用了
trending 并返回趋势数据，说明链路可用，但选择工具仍有波动，不能标为稳定完成。

S3 最后选中作者“张雪峰老师”、原评 ID 54633674。工具返回完整原文，最终引用的游戏修改器
比喻及“黑超梦”收尾可在原评核对。所获取 15 条中，Render 预算保留 9 条，补注如实披露。
最终答复还加入“这剧只有一季”的纠错说法及对另一条评论的比较；没有逐项证据验证，不能认定
整段忠实性已通过。

公开 HTML 定位核对：`https://bgm.tv/subject/513878/comments` 的目标节点为
`id="likes_grid_54633674"`，已修复此前生成不存在的 `#54633674` 锚点。
这是分页列表锚点，随新评论翻页可能移出当前页，不承诺永久单条 permalink。

第一组纠正续聊曾真实失败：duplicate breaker 将前一用户轮次同参数读取算作循环，返回兜底话术。
固定材料复现先红，修复后同会话 HTTP/SSE 两次续聊均继续分析表达手法。
各组验收均报告 database_attempts=0；临时服务器退出后没有常驻服务。

## 两路审查与仍未通过的部分

Standards 审查发现 Render 绕过预算、未知/缺定位引用静默丢标记及重复分类逻辑；
Spec 审查发现 chat 状态滞后、渲染降级跳过来源解析、实际阅读计数未披露和日志正文静默截短。
以上均修复并补回归；最终两路复核未报告本次改动的阻塞项。

保留余项：模型工具选择与额外事实归因、遗漏来源标记的生成内容、长会话和跨会话记忆、
board 楼层读取、其他工具及历史工具压缩、生产启动与迁移。它们不因离线测试或 HTTP 200 自动通过。
