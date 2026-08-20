# BGM Agent 复核档案

> **复核日期**：2026-08-17
> **复核对象**：[`docs/AUDIT.md`](AUDIT.md)（同学审计报告，基于 commit `e36197c`）
> **复核 HEAD**：`892b85d`（审计基线之后另有 4 个提交：`e7c086e` 后端三件套 / `7f15466` 文档清理 / `ee29bd8` 删 Critic / `892b85d` 修 test_rag 收集）
> **复核方式**：4 路并行代码核验（P0 / P1 前半 / P1 后半 / P2+文档+遗漏扫描）+ 本机实测 pytest。所有审计行号已按当前 HEAD 重新定位。复核未修改任何项目文件。

---

## 目录

- [1. 判词](#1-判词)
- [2. 审计质量：该信什么，要打折什么](#2-审计质量该信什么要打折什么)
- [3. P0 七项核验](#3-p0-七项核验)
- [4. P1 二十八项核验](#4-p1-二十八项核验)
- [5. 审计盲区：它没看见的问题](#5-审计盲区它没看见的问题)
- [6. 实测记录](#6-实测记录)
- [7. 演进策略](#7-演进策略)
- [8. 局限与说明](#8-局限与说明)

---

## 1. 判词

**审计值得认真对待，但不必全盘照收。** 37 项硬指控中 **33 项成立**（含部分成立），大方向全中：

- "宣称领先现实约一个版本"的判断准确命中项目现状：文档大面积漂移（版本号 4 处矛盾、CLAUDE.md 两处失真、README 测试数已过时）、空库迁移链必断、测试存在假阳性与"从未通过"的断言。
- 但它有 **2 处误报、2 处失实、2 处夸大**，且有一个它自己没意识到的盲区——审计基线之后的 `e7c086e` 新增的 `middleware.py`（IP 限流）完全在视野外，那里藏着 6 个审计不知道的新问题。这反过来说明项目迭代速度确实快，文档跟不上不是偶然。

**对项目的评判**：审计给 6.5/10（设计品味 8.5、交付可靠性 4.5）。在审计当时这个分数大体成立；当前 HEAD 上，4 个后续提交已修复约 15 个测试用例与收集阻塞，实测从"0 个测试能执行"回到"466 过 / 6 挂"——**交付可靠性可上调至 5.5 上下，综合约 6.8~7/10**。架构与品味的好评经抽查全部属实（Character Card、SystemMessage 免疫、时间衰减公式、HATEOAS 提示）。真实短板不是品味，是三个互为因果的事：**文档没有随重构同步、测试没有自动执行保障、迁移没有空库验证**——根子在于没有 CI 这道闸。

## 2. 审计质量：该信什么，要打折什么

### 该信——大方向全部命中

- **宣称 ≠ 现实**：版本 4 处矛盾（`core/config.py:34` 0.1.1 / README badge 0.1.1 / ROADMAP v0.2.0-beta / git tag v0.1.3(beta)）、CLAUDE.md 失真、ROADMAP 引用已删除路径（`orchestrate/`、人格数 4）。
- **P0-1 迁移链必断**：双重断裂（001 对已删列 chunk_text 建索引 + 002 重复加 popularity 列），`main.py:145` 启动即触发。
- **测试假阳性与"从未通过"的断言**（`test/test_dialogue.py:267` 先断言 3 再断言 5）——本机实测精确复现。
- **隐私与契约问题**：缓存无 user_id 隔离、limit/字段名/nsfw 契约错位、created_at 冻结。
- **正面结论（审计第 2 章）抽查 6 项全部属实**——不是为黑而黑。

### 要打折——三类失准

| 类型 | 条目 | 核验结论 |
|---|---|---|
| 误报 | **P0-6** DEV_MODE telemetry 必崩 | 实测 langgraph 1.2.10 默认 astream 即 updates（`main.py:621` 不传 stream_mode 不崩）；只剩 `requirements.txt:15` 未锁上界的版本漂移风险 |
| 误报 | **P1#10** name_search 的 GREATEST 遇 NULL 排最前 | WHERE 子句（`retriever.py:396-404`）已用相同 similarity 表达式过滤 NULL 行，且 PostgreSQL 的 GREATEST **忽略 NULL**——"NULLS FIRST 霸榜"不可能发生（审计疑似混淆 MySQL GREATEST 语义） |
| 失实 | P0-5a 称 `rag/eval/artifacts/` 目录不存在 | 实际存在且产物齐全（ground_truth_auto.json 25 条 / merged 34 条 / pooled 10 条，8-11/8-12 生成，早于审计日）；循环验证批评本身成立 |
| 失实 | 语料规模"实际 800/100/300/100" | 仓库无出处；`scripts/data/*.json` 实为 1000/200/400/150 |
| 夸大 | P1#26 GraphRecursionError 降级裸 Exception | 结构真实但**不可触发**（langgraph 完全缺失时 `main.py:22` import 先行失败，fallback 执行不到），属防御性死代码 |
| 夸大 | P1#27 LLM key 解析优先级矛盾 | 声明顺序（config 的 AliasChoices AZURE→OPENAI vs llm.py 的 `_resolve_api_key` OPENAI→AZURE）确实相反，但 Settings 正常加载后 llm.py 的 os.environ 回退是死代码，无实际行为冲突 |
| 批评不成立 | 余弦距离"假设距离域 [0,1]"（P2 5.5） | `1 - cosine_distance`（`long_term.py:865`）恰是 cosine similarity，与向量是否归一化无关（余弦是角度度量）；阈值比较全基于原始距离且一致。残留真实问题仅：`long_term.py:853` docstring 写 [0,1] 与 `retriever.py:56` 写 [0,2] 自相矛盾 |

## 3. P0 七项核验

| 编号 | 指控 | 结论 | 证据（当前 HEAD） | 重定级 |
|---|---|---|---|---|
| P0-1 | 空库迁移链必断 | **CONFIRMED** | `migrations/20260803_001_initial_schema.py:70-76` 对已删列建 trigram 索引；`20260804_7a94e5c746c4.py:29-32` 重复加 popularity 列；`main.py:145` lifespan 启动即触发 | **维持 P0** — 新环境起不来 |
| P0-2 | 测试收集 ImportError，0 个执行 | **FIXED**（892b85d） | `test/test_rag.py:14` 改导入 `rag.enricher._build_chunk_text`；实测 543 collected 0 errors | 关闭 — 但 README/CLAUDE.md 的"539"已过时（实际 543） |
| P0-3a | 客户端异常逃逸 | **CONFIRMED** | `clients/base.py:95,105` 仅捕 TimeoutException/HTTPStatusError；`response.json()` 的 JSONDecodeError 逃逸；16 个工具中仅 `search_local_bangumi` 1 个有兜底 | 降 P1 — 真实缺陷，非启动阻断（端点层已有全局兜底） |
| P0-3b | render 后处理裸奔 500 | **PARTIAL** | e7c086e 新增全局 handler（`main.py:172-185`）把裸 500 变 JSON 500 + 单点日志；但 `render.py:156` create_llm、`main.py:385,406` render/store 仍在 try 外 | 降 P1 — 用户仍拿 500，"不抛异常"铁律未兑现 |
| P0-4 | requirements 缺 langchain-openai | **CONFIRMED** | 代码 5 处 `from langchain_openai import`（`llm.py:18`、`classify.py:15`、`conftest.py:14` 等）vs 依赖清单零项；本机能跑恰因 .venv 非纯净安装 | **维持 P0** — "4 步启动"断在第 2 步 |
| P0-5a | RAG 评测循环验证，Recall 虚高 | **PARTIAL** | 循环验证代码在（`evaluate.py:92-161` 用 GT 同源 SQL 条件重跑）；但 artifacts 已有产物、按 category/来源分组报告已实现（`:556-607`），仅全局表仍混算 | 降 P1 — 方法学缺陷 |
| P0-5b | 分类器评测 accuracy 恒 0 | **CONFIRMED** | `eval/eval_classifier.py:92-98` 标注 `-> str` 实际返回 `classify_intent_llm` 的 tuple；`:136` 恒 False | 降 P1 — 评测失真，不影响生产 |
| P0-6 | DEV_MODE telemetry 必崩 | **WRONG**（实测推翻） | langgraph 1.2.10 默认 astream = updates 形态，`main.py:621-623` 不崩；仅剩未锁上界风险 | 降 P2 — 显式传 `stream_mode="updates"` 即可 |
| P0-7 | 无 CI | **CONFIRMED** | `.github/` 仅 `copilot-instructions.md`；6 个失败测试无人发现即为现状证据 | 降 P1 — 单人项目，仍是根因 |

## 4. P1 二十八项核验

28 项中 **25 项成立**，1 项误报（#10），2 项夸大（#26/#27）。按复核结论分三组：

### 成立 · 维持 P1（20 项）：#1 #2 #3 #4 #5 #6 #8 #9 #12 #14 #15 #16 #17 #18 #19 #20 #21 #22 #25 #28

- **隐私级**：#1 缓存无 user_id 隔离（键只有 session_id，`cache.py:60,152`；唯一可升 P0 的 P1，违背"隐私第一"承诺）；#14 第 2 轮起 Aggregator 的 SystemMessage 消失（`reasoning.py:86-98`，身份/终止规则/真实性约束/few-shot 全丢）；#18 chat 路径 render 无对话历史（`render.py:158` 单条 SystemMessage，多轮闲聊失忆）
- **契约级**：#4 search 的 limit 放 JSON body 不生效（`client.py:60-65` vs yaml query param）；#5 get_user_profile 收藏字段名错（期望嵌套 `subject.name`，契约返回扁平 `name/nameCN`，姓名/职业恒空串）；#17 nsfw 静默丢弃（`client.py:61-63` body 无此字段）；#22 trending 默认 type=2 无法表达"全部"（`client.py:145-147`）
- **语义级**：#15 部分失败丢成功数据（episode 失败时顶层 `_error` 使成功评论被整体丢弃，与 docstring 相反）；#16 Retry-After 无类型校验无上限（非数字 ValueError + 3600 可 sleep 数小时）；#21 `tool_choice="required"` 无 provider 兜底 + reasoning 缺 `if intent_tools` 防护（当前路由下不可达）
- **其余**：#2 created_at 冻结破坏时间衰减；#3 字数控制（软限制靠自觉、硬截断 280/480 字符超宣称 200/350 字）；#8 extra_body thinking 硬编码；#9 deep 模式 pipeline 用错记忆参数（恒 300/0.35）；#12 embedding 零重试 + 维度无预检（DB 层晚失败，非静默）；#19 日期问题误分类根因；#20 非消化态 XML 泄漏无防护（`TOOL_CALL_XML_RESIDUE` 零引用）；#23 自相矛盾断言（实测复现）；#25 mock 测试依赖真实 key；#28 评测标签体系错位

### 成立 · 降 P2（5 项）：#7 #11 #13 #24

- #7 人格两层管线（性质为文档失真——代码与自身设计自洽，改 CLAUDE.md 即可）
- #11 别名子串误触发（`_aliases.py:100-105` 纯子串匹配，但别名是追加且返回前过相似度 WHERE，污染被部分吸收）
- #13 分类器双重阈值 + `has_entities` 死代码（`classify.py:211-224` 0.8/0.5 vs `routes.py:45` 0.7 叠加；`has_entities` 生产永不传，影响仅为路由次优）
- #24 假阳性测试（`test_tools.py:237-249` try/except pass、`test_sanitizers.py:160-162` 空测试、`test_integration.py:284` 恒真）

### 夸大 / 误报（3 项）：#26 #27 #10

见[第 2 节表格](#2-审计质量该信什么要打折什么)。

## 5. 审计盲区：它没看见的问题

全部位于审计基线之后的 `e7c086e` 新代码（middleware.py + main.py 约 160 行改动）——审计完全未覆盖。

| 级 | 问题 | 证据 |
|---|---|---|
| P1 | 限流表无界增长：清理只在同一 IP 再次请求时触发，一次性访问的 IP 条目永不回收 | `middleware.py:22,38-44` |
| P1 | 反代/多 worker 下限流失真：只取 `request.client.host`，不认 X-Forwarded-For——nginx 后全员共享一桶，一人耗尽全体 429；多 worker 时限额 ×worker 数 | `middleware.py:34` + `core/config.py:56` |
| P1 | 请求级超时不覆盖 render 与 session_cache.store：45s 已回"超时"后请求仍在后台渲染+写缓存 | `main.py:308-317` vs `:385,:406` |
| P2 | 429 响应不带 CORS 头（Starlette `add_middleware` insert(0)：限流在 CORS 外添加反而最外层），浏览器读不到 429 错误体，与 `main.py:167-168` 注释宣称相反 | `main.py:160-169` + `middleware.py:50-54` |
| P2 | SSE 断开（CancelledError）时把"提问无应答"的 final_state 写入 L2 长期记忆 | `main.py:476-478,570-594,708` |
| P2 | test_graph 残留 Critic 死测试（`test_shallow_mode_skips_critic`，ee29bd8 删了 test_critic.py 307 行但漏了这个） | `test/test_graph.py` |

其余检查过未升级：SQL 注入面无发现（retriever 全部走参数化）；`/chat/stream` 在 `[DONE]` 之后才写缓存，store 失败会在 DONE 后补发 error 事件（P2 附注）；`init_db()` 同步调用阻塞 async lifespan（P2）。

## 6. 实测记录

本机 .venv（Python 3.14.5 / pytest 9.1.1 / langgraph 1.2.10 / langchain-openai 1.4.1）。

| 项 | 审计基线（e36197c） | 当前 HEAD（892b85d） |
|---|---|---|
| pytest 收集 | 539 collected, 1 error — **0 个执行** | **543 collected, 0 errors** |
| 排除 rag+integration 实跑 | 474 passed / 21 failed / 2 errors / 23 skipped | **466 passed / 6 failed / 23 skipped**（errors→0，failed 21→6） |
| test_endpoint | 无 key 时 5 个 500 失败 | **12 / 12 通过** |

剩余 6 个失败（按性质）：

| 失败用例 | 性质 |
|---|---|
| `test_bgm_tools.py::test_no_block_event_loop` | 测试按过时契约写（断言 search_local_bangumi 返回 str，实际返回 dict——7f15466 已改 CLAUDE.md，测试未同步） |
| `test_dialogue.py::test_last_chance_unbinds_tools` | 行为/测试漂移（bind_tools 被调用 1 次，断言未调用） |
| `test_dialogue.py::test_max_iterations_enforced_in_node` | **审计 P1#23 精确复现**（`test_dialogue.py:267` 自相矛盾断言，从未通过） |
| `test_graph.py::test_shallow_mode_skips_critic` | ee29bd8 清理遗漏的死测试 |
| `test_memory.py` ×2（同一性断言） | 断言 `result is messages` 过严/实现漂移 |

另：**ruff 在本地两个 venv 中均未安装**——CLAUDE.md 声明的格式化门禁本机无法执行（P2 工程化证据）。

## 7. 演进策略

按"止血 → 隐私与正确性 → 对账 → 防线"排序；每阶段独立可交付，完成后立刻提交。

### 阶段零（半天）—— 让项目能装、能启动、能自证

对应 P0-1 / P0-4 / P0-7。

1. 迁移 001/002 重写为显式 DDL（`op.create_table` 固定列集），删掉对已删列 chunk_text 的索引；用 docker 起**全新** pgvector 容器跑通 `init_db()` 全流程——别再用旧开发库验证。
2. `requirements.txt`：补 `langchain-openai`；`langgraph` 锁上界（如 `>=0.2,<1.0`）；删 `python-dotenv`/`requests` 零引用项。
3. 最小 GitHub Actions（pytest，DB 相关用 docker service；+ ruff）；CLAUDE.md 的测试数改为 CI 徽章或如实数字。
4. 同一提交修掉 3 个死测试：`test_dialogue.py:267` 自相矛盾断言、`test_graph.py` critic 残留、`test_bgm_tools.py` str 断言。

### 阶段一（1-2 天）—— 隐私与对话正确性

P1 中成本最低、伤害最大者。

1. **缓存键加 user_id 维度**（`cache.py:60,152`）：键改 `(user_id, session_id)` 元组——"隐私第一"是产品承诺，这是唯一一条可升 P0 的 P1。
2. **created_at 语义修正**（`long_term.py:377`）：更新时刷新，或引入独立 `first_seen_at`，避免活跃记忆按 14 天半衰期被当作陈旧记忆衰减。
3. **第 2 轮起保留精简 SystemMessage**（`reasoning.py:86-98`）：身份/终止规则/真实性约束是防幻觉护栏，不应只存在于首轮。
4. **chat 路径 render 携带最近 N 条历史**（`render.py:158`）——多轮闲聊失忆直接违背"记得你聊过什么"。
5. 把 render 与 `session_cache.store` 纳入请求超时域内（盲区 #3）。

### 阶段二（约 1 周）—— 契约回归与工程对账

1. **契约回归测试**：limit 移到 query param、get_user_profile 扁平字段（拿 `docs/Tools/bangumi_openapi_p.yaml` 逐字段对照）、nsfw 传入 filter、trending 支持"全部"——数据丢失级缺陷，值得写测试锁死。
2. **客户端兜底**（`base.py`）：最外层 `except Exception → {"_error": ...}`；Retry-After 类型校验 + 上限（如 ≤60s）。
3. **middleware 三缺陷**：限流表定时清理、429 补 CORS 头、认 X-Forwarded-For（或文档声明仅限直连）。
4. **评测修复**：`eval_classifier.py` 解包 tuple（一行）；keyword 通道直调生产 `retriever.keyword_search`。
5. **文档对账**：版本号单点来源（git tag 驱动 badge）；修 CLAUDE.md 两处失真（`search_local_bangumi` 返回 dict、"Character Card 决定 WHAT to think"改为如实描述）；ROADMAP 清掉 `orchestrate/` 路径与人格数 4。
6. **死代码清除**：BangumiChunk 模型/表/注释检索器（`rag_tables.py:308-359`）、`TOOL_CALL_XML_RESIDUE`（已零引用）、`_ROLE_MAP`/`_TYPE_ICONS`/`text_processor.py`。

### 阶段三（持续）—— 性能与测试防线

1. 进程级共享 httpx 连接池（16 个工具每次新建连接池 = 每次 TLS 握手）+ `create_llm` 缓存 + 检索器单例——agent 场景延迟敏感，这是吞吐大头。
2. 消费 `X-RateLimit-*` 头做节流；补 name_search/keyword_search 两通道测试（当前零覆盖）。
3. 假阳性测试清零（try/except pass 改真断言或删）；上 pytest-cov，补 SessionCache/guardrails/render 降级等零覆盖区。
4. SSE 断开语义修正：先写缓存再发 `[DONE]`，避免"应答已生成但用户已断开"时丢 L2。
5. 每完成一个阶段跑一次 `pytest test/` 全量 + 空库迁移演练；可重跑本审计复核以验证收敛。

## 8. 局限与说明

- 本机无 PostgreSQL 与外部 API key，`test_rag.py`/`test_integration.py`/记忆真实服务路径未做端到端验证（与审计同限）。
- P1#8（OpenAI 拒收 thinking 参数）与 middleware CORS 挂载顺序为静态推断；实测数据来自本机 .venv，非 requirements 纯净安装。
- 复核未修改任何项目文件。
