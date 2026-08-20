# BGM Agent 项目审计报告

> **审计日期**：2026-08-16（基于 commit `e36197c`）
> **审计方式**：静态代码审计（全量通读核心模块）+ 5 个子系统并行深审 + 实测验证（临时 venv 安装依赖、运行 pytest 收集与部分执行、最小复现脚本）。审计过程未改动任何项目代码，实测验证均已对照源码逐条确认。
> **项目规模**：206 文件 / 约 11.2 万行（约半数为评测数据 JSON 夹具），纯 Python 代码约 3.5 万行；136 个提交；开发周期约 2.5 个月（2026-05-29 ~ 2026-08-16）。

---

## 目录

- [1. 执行摘要](#1-执行摘要)
- [2. 做得好的地方](#2-做得好的地方)
- [3. P0 — 阻断级问题（7 项）](#3-p0--阻断级问题)
- [4. P1 — 功能缺陷 / 契约违背 / 隐私问题](#4-p1--功能缺陷--契约违背--隐私问题)
- [5. P2 — 技术债 / 文档漂移 / 性能](#5-p2--技术债--文档漂移--性能)
- [6. 测试与评测体系专项评估](#6-测试与评测体系专项评估)
- [7. 宣称 vs 现实对照表](#7-宣称-vs-现实对照表)
- [8. 修复路线图](#8-修复路线图)
- [9. 审计局限性与方法说明](#9-审计局限性与方法说明)

---

## 1. 执行摘要

**总评：设计品味与工程文化 8.5 分，交付可靠性 4.5 分，综合 ≈ 6.5/10。**

这是一个"架构直觉好、文档文化罕见、产品设计有品味，但快速迭代中『宣称与现状』严重脱节"的个人项目。它像一个装修到 70% 的房子：设计图很漂亮，但你现在不能照着 README 的 4 步启动住进去。

| 维度 | 得分 | 依据 |
|---|---|---|
| 架构与分层设计 | 8.5 | 四层单向依赖、异质拓扑、职责切分干净 |
| 产品思维与人格设计 | 8.5 | Character Card 质量、HATEOAS 提示、persona-not-script |
| 记忆系统实现 | 7.5 | 细节用心，但有串号级隐私缺陷 |
| 客户端/数据层健壮性 | 5.5 | 重试意识好，但异常逃逸 + 契约错位是硬伤 |
| 测试与验证体系 | 4.5 | 规模大但跑不起来，且有多处假阳性 |
| 文档质量（内容） | 8.0 | postmortem 是少见的高质量 |
| 文档一致性（宣称=现实） | 3.5 | 版本号四处矛盾、CLAUDE.md 多处失真 |
| 部署可运行性 | 4.0 | 空库迁移必败、依赖清单缺包 |

**核心判断**：项目的"自我叙事"（badge、4 步启动、手册式 CLAUDE.md）领先于代码现实约一个版本。修复 P0 清单（约 1-2 天工作量）后，它才真正配得上自己的文档。

---

## 2. 做得好的地方

以下优点均有代码证据，非客套：

### 2.1 架构分层是真干净

- 编排层 → 人格层 → 记忆层 → 数据层的单向依赖在代码里确实成立（数据层零 `AgentState` 引用），不是纸面宣称。
- `messages: Annotated[list[BaseMessage], operator.add]`（`agent/state.py:41`）使用正确，节点返回追加而非覆盖。
- 异质拓扑（Pipeline + ReAct + 隐式终止）符合 LangGraph 最佳实践：START→classify_node 按 7 intent 分发（`agent/graph.py:191-204`），subgraph 条件边映射与路由函数返回值逐键对齐，无死路、无不可达节点。
- 路由熔断层设计合理：硬熔断（iterations ≥ max → END）、连续空搜索早停、重复调用检测（`agent/routing/routes.py:74-140`）。

### 2.2 产品设计有真实的品味

- **Character Card 质量上乘**（`agent/persona/profiles.py:179-193`）——"人格描述而非行为指令"（persona-not-script）的 prompt 哲学比绝大多数 agent 项目高级。
- **HATEOAS 提示**：`search_bangumi_subject` 返回里注入 `_next` 引导 LLM 走 detail（`tools/bgm_tools.py:207-212`）——细节见功力。
- **few-shot 基于真实数据**：示例基于"166 条真实 trace 的行为缺口"提炼（`agent/prompts/aggregator.py:155`），是数据驱动的 prompt 工程而非拍脑袋。
- 人格参数分流设计真实落地：snark/initiative → Render，depth_taste → Aggregator，两处共用 `_pick_level`（`agent/persona/profiles.py:167-172`）。

### 2.3 记忆系统的工程细节扎实

- tiktoken `cl100k_base` 精确计数 + 编码失败降级路径（`agent/memory/short_term.py:70-83`）。
- SystemMessage 免疫真实：截断跳过（`short_term.py:194-197`）、滑动窗口保留（`short_term.py:296-297`）。
- **孤儿 ToolMessage 清理**（`short_term.py:213-274`）——防止 DeepSeek 报 "Messages with role 'tool' must be a response to a preceding message with 'tool_calls'" 400 错误，真实存在的贴心细节。
- 时间衰减公式数学正确：`combined = (1 - cosine_distance) × 0.5^(days_ago / half_life)`（`long_term.py:865-878`），边界情况（未来时间戳、半衰期≤0）有 clamp。

### 2.4 工程文化罕见成熟

- `docs/design/v0.1.3-postmortem.md` 是个人项目里少见的高质量复盘：数据驱动（两次压测 run ID）、根因分层（✅有数据/⚠️推断/❓推测）、诚实标注"需要但缺少的数据"、修复标注 commit。
- Conventional commits 全仓库贯彻（136 个提交均规范）；代码中几乎零 TODO/FIXME 残留。
- DEV_MODE 遥测用 ContextVar 实现协程安全（`agent/devtools.py:83-96`）；SSE 对客户端断开做 `asyncio.shield` 紧急保存（`main.py:492-515`）。
- `eval/metrics.py` 纯函数实现 recall@k/precision@k/mrr/ndcg，公式与边界处理正确且注明出处。

### 2.5 安全基础意识好

- access_token 永不进 LLM schema、只在 `BangumiClient(access_token=token)` 构造时注入（`bgm_tools.py:806/863/915`）。
- 日志只记 path+status 不打 body/token（`clients/base.py:67`）；`format_tool_error` 剥离堆栈（`agent/guardrails.py:151-163`）。
- sanitizers 纯函数 + 白名单 + 兜底默认值；User-Agent 合规（`clients/base.py:24`）。
- `.env` 已 gitignore，`.env.example` 无真实密钥。

---

## 3. P0 — 阻断级问题

> P0 定义：无法部署 / 无法运行 / 核心承诺失效。全部经实测或逐行验证。

### P0-1 全新数据库迁移链必然断裂

`database/migrations/versions/20260803_001_initial_schema.py:29` 用 `SQLModel.metadata.create_all()` 按**当前** ORM 模型建表，然后 `:56-64` 对 `chunk_text` 列建 trigram 索引——但当前 `RagEntity` 已没有这一列（重构中移除，仅在过期注释/docstring 中出现）。即便修掉，`002` 迁移（`20260804_7a94e5c746c4_add_popularity_column.py:29-32`）的 `add_column("popularity")` 也会因列已由 create_all 建出而报重复列。

**后果**：任何全新环境 `init_db()` 必失败；现有开发库能跑只是因为表结构是重构前创建的。这是"v0/v1 共存债务"的直接恶果。

**修复**：迁移重写为显式 DDL（`op.create_table` 固定列集），或删除 `chunk_text` 索引 + `002` 加列前判空。

### P0-2 宣称的 539 个测试根本跑不起来

`test/test_rag.py:14-22` 导入 `_build_character_chunk_text` / `_build_person_chunk_text` / `_build_subject_chunk_text`，这些函数已在 RAG 重构中改名为 `_build_*_rag_dict`。实测 `pytest test/`：

```
ERROR collecting test/test_rag.py
ImportError: cannot import name '_build_character_chunk_text' from 'rag.ingestion'
539 tests collected, 1 error — Interrupted: 1 error during collection
```

**后果**：**0 个测试执行**。README badge 的 "tests-539" 是断的——项目的头号质量信号失效。

**修复**：更新 `test_rag.py` 的导入名（或恢复旧函数），让套件至少能收集完。

### P0-3 "不抛异常"铁律不成立（两处）

**3a. 客户端异常逃逸**：`clients/base.py:66-93` 只捕获 `httpx.TimeoutException` 和 `httpx.HTTPStatusError`。`ConnectError`/`ReadError`/`RemoteProtocolError`（连接拒绝、DNS 失败）、`response.json()` 的 `JSONDecodeError`（200 + HTML 错误页）全部逃逸；而 16 个工具函数零 try/except 兜底——异常穿透 LangGraph 运行时。

**3b. render 后处理裸奔**：`main.py:279-308` 的 try/except 只包住 graph 调用；render 后处理（`main.py:337-350`）、`session_cache.store`（`354-358`）均在 try 外。而 `agent/persona/render.py:156` 的 `create_llm()` 调用在 try 之外（render.py 的 try 从 157 行才开始）——**实测无 `LLM_API_KEY` 时 `/chat` 返回未捕获 500**（`test_endpoint.py` 5 个用例因此失败）。

**修复**：`_request` 最外层兜底 `except Exception → {"_error": ...}`；`/chat` 后处理整体纳入 try；`create_llm` 移入 render 的 try 内。

### P0-4 requirements.txt 缺 `langchain-openai`

`agent/llm.py:18`、`agent/nodes/classify.py:15`、`test/conftest.py:14` 均 `from langchain_openai import ...`，但 `requirements.txt` 没有此项。全新 `pip install -r requirements.txt` 后启动即 `ModuleNotFoundError`——README 的"4 步启动"断在第 2 步。

**修复**：补 `langchain-openai` 并锁定版本（见 P2-5）。

### P0-5 评测体系必错（两处，一处虚高一处恒零）

**5a. RAG 评测是循环验证，Recall 虚高**：`rag/eval/evaluate.py:172-195` 的 auto GT 由 SQL 条件查库生成，keyword 通道又用**同一段 SQL 提取的相同 WHERE 条件**重跑（`:92-161`）——18 条 tag/year/score 查询测的是"过滤器实现自洽性"而非检索质量。auto 组（GT 可达上百行）与 human 组（1-3 行）混算全局指标，系统性高估能力。且 `rag/eval/artifacts/` 目录不存在——该评测管线从未产出过基线数字。

**5b. 分类器评测 accuracy 恒为 0**：`eval/eval_classifier.py:92-98` 的 `_classify_llm` 返回 `classify_intent_llm` 的 `tuple[intent, confidence]` 却标注 `-> str`；`eval_classifier.py:136` 的 `true_label == pred_label` 恒为 False——LLM baseline 报告数字完全失真，任何基于此评测的调参结论都不可信。

**修复**：keyword 通道直调生产 `retriever.keyword_search`，auto 组降级为过滤器回归测试；`eval_classifier.py` 解包 `intent, _ = await classify_intent_llm(...)`。

### P0-6 DEV_MODE telemetry 路径在 langgraph ≥1.x 下必然崩溃

`main.py:529-562` 的 `_run_with_telemetry` 假定 `astream` 每个事件是 `{node_name: node_output}`（注释 `:533` 明写 `stream_mode="updates"`），但调用处 `:541` **没传 stream_mode**。实测 langgraph 1.2.11 默认 `stream_mode="values"`——每个事件是完整状态，`event.items()` 抛 `AttributeError: 'list' object has no attribute 'items'`（已构造等价 FakeApp 复现）。异常被 `main.py:296` 的 except 吞掉，DEV_MODE=true 时所有 `/chat` 静默返回"抱歉，请稍后重试"。

根因：`requirements.txt:15` 只写 `langgraph>=0.2.0` 未锁上界——0.2.x 默认 updates 能跑，1.x 默认 values 就崩。`conftest.py:24-38` 特意禁用 DEV_MODE 也侧面证明这条路径从未被测试。

**修复**：`astream` 显式传 `stream_mode="updates"`（或改用 ainvoke + 回调）；`langgraph` 锁上界（如 `<0.4`）。

### P0-7 无 CI

`.github/` 只有 `copilot-instructions.md`——539 个测试没有任何自动执行保障。P0-2、P0-6、以及那个"从来不可能通过"的测试（P1-1）之所以长期存在，根因都在这里。

**修复**：最小 GitHub Actions（pytest + ruff），测试挂一次修一次。

---

## 4. P1 — 功能缺陷 / 契约违背 / 隐私问题

| # | 问题 | 证据 |
|---|---|---|
| 1 | **Session 缓存无 user_id 隔离（隐私级）**：缓存键只有客户端可传的 `session_id`，任何持有 ID 者都能读到前序对话全文，并把 A 的上下文写进 B 的 L2 记忆——违反项目自述的"隐私第一" | `agent/memory/cache.py:60,65-158`、`main.py:249` |
| 2 | **created_at 冻结破坏时间衰减**：UPSERT 更新摘要/embedding 但注释明写"created_at 不更新"——多轮 session 的记忆被当作陈旧记忆衰减 | `agent/memory/long_term.py:368-378` |
| 3 | **字数控制形同虚设**（ROADMAP 自认 P0）：软限制靠 LLM 自觉；硬截断 280/480 **字符**远超宣称的 200/350 **字**（中文 1 字 ≈ 1 字符），且只截不重试、断句无收尾 | `agent/persona/render.py:29-40,172-186` |
| 4 | **search 的 `limit` 放进了 JSON body，契约要求 query param** → limit 实际不生效，可能返回 20 条而工具宣称 ≤8 | `clients/client.py:60-65` vs openapi yaml `:8364-8372` |
| 5 | **`get_user_profile` 收藏分支字段名与 API 契约不符**：期望 `c.subject.name` 但契约返回扁平 `name/nameCN` → 姓名/职业恒为空串（静默数据丢失） | `clients/client.py:374-397` vs yaml `:2246-2276` |
| 6 | **重复调用/连续空搜索直接 END 且无最终文本**：guardrail 语义本是"注入反馈换策略再给一轮"，路由却直接终止；main.py 会向上找到旧回复重渲染或落"（无数据）"兜底 | `agent/routing/routes.py:126-137` vs `agent/guardrails.py:108-111` |
| 7 | **人格两层管线宣称失真**：CLAUDE.md 说"Character Card 决定 WHAT to think"，代码里 Card 只进 Render（HOW to say），Aggregator prompt 是人格中性的"数据专家"——与 aggregator.py 自身文档一致，与 CLAUDE.md 矛盾 | `agent/prompts/aggregator.py:1-9,90-96,272-276` |
| 8 | **`extra_body={"thinking": {"type": "disabled"}}` 硬编码**：DeepSeek 专用参数，OpenAI/Azure 后端会被拒——与"多 provider 兼容"宣称冲突 | `agent/nodes/reasoning.py:116`、`agent/nodes/pipeline.py:82` |
| 9 | **deep 模式下 pipeline 路径用错记忆参数**：无视 depth 一律用非 deep 的 300 tok / 0.35 阈值，拿不到宣称的 500 tok / 0.5 | `agent/nodes/pipeline.py:59-67` vs `reasoning.py:71-81` |
| 10 | **`name_search` 的 `GREATEST` 遇 NULL 行排最前**：`name_cn` 为 NULL 的行相似度恒 NULL，DESC 默认 NULLS FIRST → 无关条目霸榜 | `rag/retriever.py:382-409` |
| 11 | **别名子串误触发**：`"ll"` 条目使 "all/will/hello/llm" 全展开成 Love Live!；`"eva"` 命中 "evaluate" | `rag/_aliases.py:39,59,100-104` |
| 12 | **embedding 零重试 + 维度零校验**：智谱 SDK 不走 BaseClient 重试，瞬时故障中止整批灌入；改 `EMBEDDING_MODEL` 忘改维度会静默 mismatch | `rag/ingestion.py:394-402`、`database/rag_tables.py:36` |
| 13 | **分类器双重置信度阈值 + `has_entities` 死代码**：0.5/0.8（classify.py）与 0.7（routes.py）两套门槛叠加无文档；`classify_node` 从不传 `has_entities`（恒 False），"chat→fetch"降级分支在生产永不生效却有测试 | `agent/nodes/classify.py:211-224,253`、`routes.py:45` |
| 14 | **第 2 轮起 Aggregator 的 SystemMessage 消失**：首轮后才 `system_content=None`，身份/终止规则/真实性约束/few-shot/场景提示全丢，只剩陈旧 seed——幻觉风险随轮次上升 | `agent/nodes/reasoning.py:86-98`、`main.py:260` |
| 15 | **`get_episode_discussion` 部分失败语义颠倒**：评论成功但 episode 失败时，顶层 `_error` 导致成功数据被整体丢弃，与 docstring 承诺相反 | `clients/client.py:188-198` + `tools/bgm_tools.py:533-534` |
| 16 | **`Retry-After` 无类型校验、无上限**：非数字头直接 ValueError；`Retry-After: 3600` → 外部可控 sleep 数小时（DoS 向量）；契约 429 根本没此头（死代码） | `clients/base.py:69-76` vs yaml `:7349-7367` |
| 17 | **`nsfw` 参数被静默丢弃**：工具暴露但 body 里根本没传——R18 过滤/包含都不生效 | `clients/client.py:61` vs `tools/bgm_tools.py:151` |
| 18 | **chat 路径 render 无对话历史**：单条 SystemMessage 调用，多轮闲聊必然失忆——与产品定位"记得你聊过什么"直接冲突 | `agent/persona/render.py:158`、`main.py:316-322` |
| 19 | **"今天星期几"误分类根因**：硬规则 `"今天/本周/当前在播" → realtime`（classify.py:40）把"今天星期几"带进 realtime；chat 类示例无日期 few-shot；conf∈[0.7,0.8) 直接进 realtime_pipeline 答非所问 | `agent/nodes/classify.py:28,38,40` |
| 20 | **非消化态 XML 泄漏无防护**：`guard_xml_leak` 仅消化态检查；只剥离完整 `<function_calls>` 块，残骸检测只在已死的 critic.py 中；pipeline 节点完全不调用 | `agent/helpers.py:174`、`agent/guardrails.py:69-79` |
| 21 | **deep 0 工具调用机制缺陷**：`tool_choice="required"` 无 provider 兜底；首轮无 tool_calls 直接 END 无重试；profile 工具未注册时 `reasoning_node` 缺 `if intent_tools` 防护（pipeline 有） | `agent/prompts/tool_config.py:81-82`、`reasoning.py:118` vs `pipeline.py:85` |
| 22 | **trending 默认 type=2(anime) 与文档"留空则不限制类型"矛盾**：用户问"最近什么最火"实际只返回动画，且 schema 语义上永远无法表达"全部" | `clients/client.py:145-147`、`tools/bgm_tools.py:417-419` |
| 23 | **测试断言自相矛盾（从未通过）**：`test_dialogue.py:262-269` 先断言 `max_iter == 3` 再断言 `get_max_iterations("fast") == 5`（实际 5）——证明"全绿"从未被验证 | `test/test_dialogue.py:262-269` |
| 24 | **假阳性/空测试/恒真断言**：try/except pass（注释自认）、空函数测试、`assert len(x) >= 0` | `test/test_tools.py:230-249`、`test_sanitizers.py:160-162`、`test_integration.py:284` |
| 25 | **"mock"测试实际依赖真实 API key**：test_endpoint 只 mock `ainvoke` 未 mock render 的 `create_llm`；test_graph 漏 mock `create_classifier_llm`——无 key 时实测 11 个用例全挂 | `test/test_endpoint.py:36`、`test_graph.py:37` |
| 26 | **GraphRecursionError 降级为裸 Exception**：符号缺失时 `except GraphRecursionError` 变成 `except Exception`，把所有异常伪装成"超时"话术，掩盖真实故障 | `main.py:31-34` |
| 27 | **LLM key 解析优先级两处矛盾**：config.py AliasChoices 顺序 AZURE→OPENAI，llm.py `_resolve_api_key` 顺序 OPENAI→AZURE | `core/config.py:62-67` vs `agent/llm.py:157-190` |
| 28 | **评测基线标签体系错位**：关键词基线用旧 8-way 标签（chitchat/debate/emotional），与 7-intent 新体系混淆矩阵维度对不上 | `eval/eval_classifier.py:37-47` |

---

## 5. P2 — 技术债 / 文档漂移 / 性能

### 5.1 性能与资源

- **连接/客户端零复用**：16 个工具每次调用 `async with BangumiClient()` 新建 httpx 连接池（每次 TLS 握手，`tools/bgm_tools.py` 全文件）；`create_llm()` 每节点每轮新建 ChatOpenAI（`agent/llm.py:29-132` 无任何缓存，ROADMAP P2#9 自认）；`search_local_bangumi` 每调用重建检索器（`bgm_tools.py:1156-1169`）。
- `create_task` 泄漏：`clients/client.py:177-183, 290-291` 第一个 await 抛异常时第二个 task 永不被 await。
- 无客户端节流：`get_user_profile` 单次并发 5 请求，`X-RateLimit-*` 头完全未消费。

### 5.2 死代码与 v0 遗产

- **v0/v1 共存远比文档承认的重**：`BangumiChunk` 模型/表/CLI 导出/300 行注释旧检索器全在（`database/rag_tables.py:308-359`、`database/cli.py:59`、`rag/retriever.py:599-926`）——它直接弄断了迁移链（P0-1）。
- **死代码成体系**：`classify_intent_step`（`helpers.py:36`）零调用；`is_terminal_response` 在 v5 图中两分支都 END（无行为影响）；`critic.py` 整模块 DEPRECATED 但 `test_critic.py` 269 行测试还在维护；`_ROLE_MAP`/`_TYPE_ICONS`/`text_processor.py`/`sanitize_discussion_topics` 全死。
- 死配置：`MEMORY_MIN_SESSIONS_FOR_PROFILE`（自认 L3 deprecated）、`CRITIC_MODE`（Critic 已移除）、`MEMORY_DIALOGUE_*` 命名过时。

### 5.3 文档一致性（重灾区）

- **版本号 4 处矛盾**：`core/config.py:34` 0.1.1、README badge 0.1.1、ROADMAP v0.2.0-beta、git tag v0.1.3(beta)。
- **文档大面积过期**：`docs/Rag/rag_overview.md` 声称的 chunk_text 拼接、3000 字符截断、语料规模 250/120/80（实际 800/100/300/100）全部失真；`get_agent_tools` docstring 写"11 个无条件工具"实际 13 个；CLAUDE.md 说 `search_local_bangumi` 返回 str 实际返回 dict；ROADMAP 引用已不存在的 `orchestrate/` 路径、人格数量写 4（实际 3）。
- ROADMAP P2#6"`_memory_context` 空串 bug"已修复（`helpers.py:85-87` 用 `is not None`），条目过时且定位错误。
- 测试 dump 入库：`docs/Test/test_output/test_output_v4_*.md` 约 14 万字节。
- eval 报告硬编码日期 "2026-08-01"（`eval_classifier.py:212`、`eval_rag.py:311`）。

### 5.4 入口与配置卫生

- `/chat` 与 `/chat/stream` 约 150 行重复代码（`main.py:248-380` vs `396-519`），改一处忘另一处风险极高。
- 请求校验不完整：`message` 无 `max_length`，`session_id`/`user_id` 无长度与字符约束（`main.py:84,95-99`）。
- `asyncio.create_task` fire-and-forget 不持有引用（`main.py:361,486`）；`_remember_session` 的 except 分支引用 try 内才定义的 `effective_session_id`（潜在 NameError，`main.py:638-691`）。
- CORS 全开 + 无鉴权：`user_id` 可任意指定，L2 记忆可被跨用户污染（`main.py:158-163`）。
- `print()` 与 logger 混用（`main.py:144,147`）；导入私有函数 `_extract_user_query`（`main.py:26`）。
- requirements.txt 全部 `>=` 无锁定 + 冗余依赖（`python-dotenv`、`requests` 零引用）；pytest.ini 仅 3 行（无 asyncio_mode/addopts/testpaths）。

### 5.5 数据层细节

- `datetime.fromtimestamp` 无类型/量级校验（13 位毫秒时间戳产出 5 万年日期）。
- `subject_type` 允许不存在的类型 5（契约 `Literal[1,2,3,4,6]`）。
- username 未 URL 编码拼路径（低危注入）；RAG `_error` 把异常原文回传 LLM（可能泄露路径/连接串，`bgm_tools.py:1162,1172,1192`）。
- 三通道合并不做全局重排：popularity / trigram 相似度 / 余弦距离三种分数体系直接拼接，`final_score` 语义跨通道不统一（`bgm_tools.py:1038-1092`）。
- 映射表三处重复定义（SubjectType/收藏状态/角色类型各两份）。
- `estimate_tokens` 只计 content 不计数 `tool_calls` 的 args JSON；cl100k_base 并非 DeepSeek tokenizer——"精确计数"名不副实（`short_term.py:98-107`）。
- `similarity = 1 - distance` 假设距离域 [0,1]，pgvector 实为 [0,2]；全项目无向量归一化——0.35/0.5 阈值语义存疑（`long_term.py:865` vs `retriever.py:56`）。
- `trim_messages` 预算耗尽时整条丢弃最新 HumanMessage（与 ToolMessage 截断保底不对称，`short_term.py:306-323`）。
- 反向孤儿不清理（有 tool_calls 但 ToolMessage 已丢的 AI 消息）；清理只在 `trim_messages` 内执行，预算未超时跳过（`short_term.py:213-274,515-517`）。
- `embed_single` 硬编码 embedding-2，无视 `EMBEDDING_MODEL` 配置（`clients/zhipu_client.py:101`）。
- `classify_node` 无条件返回 `_memory_context: None`——当前拓扑恰好多亏此才触发召回，属潜伏地雷（`classify.py:260-264`）。

---

## 6. 测试与评测体系专项评估

### 6.1 数量真实性（实测数据）

| 口径 | 数字 | 依据 |
|---|---|---|
| README badge / CLAUDE.md | 539 tests / 20 文件 | README.md:6,168,212 |
| ROADMAP | 534 函数 / 22 文件 | ROADMAP.md:15 |
| 静态统计 | 494 个 `def test_` + 9 处 parametrize | 全文件正则统计 |
| parametrize 展开 | 约 571 个潜在用例 | test_tools 56 项 + test_sanitizers 30 项 |
| **pytest 实测收集** | **539 collected, 1 error**（收集中断） | 临时 venv 实跑 |
| **排除 rag+integration 实跑** | **474 passed / 21 failed / 2 errors / 23 skipped** | 临时 venv 实跑 |
| 目录实际文件数 | 22 个 .py（20 test_* + conftest + \_\_init\_\_） | 实际目录 |

**结论**：
- "539"恰好是 pytest 在 test_rag.py 崩溃点之前收集到的半途数字，纯属巧合对上了 README，不代表可运行的测试数。完整 `pytest test/` 因 ImportError 在收集阶段中断，**0 个测试执行**。
- parametrize 确实放大了数量（不算造假，但"函数数 494/534"与"用例数 539/571"是两个概念，文档混用）。
- ROADMAP "534/22" 与 README "539/20" 互相矛盾，均非自动统计。"539 全绿"在当前 HEAD 上为假。

### 6.2 质量分布

- **强断言（真实行为，约 200+ 用例，质量合格）**：`test_schemas.py`（边界/枚举/ValidationError）、`test_tools.py`（schema 绑定）、`test_classifier.py`（置信度路由表）、`test_state.py`、`test_tool_node.py`（ToolMessage 结构）、`test_prompts.py`。
- **弱断言（只测不炸/非空）**：test_graph 大部分、test_dialogue 部分、test_integration 部分。
- **假阳性（约 6-8 个不测任何东西）**：`test_tools.py:230-249` try/except pass、`test_sanitizers.py:160-162` 空测试、`test_integration.py:284` 恒真、`test_graph.py:47` 恒真、`test_phase5_l1.py:228-256` TestDialogueBudget（fixture 仅 3400 tokens < 预算 10000，截断从未发生，断言恒真）、`test_memory.py:47` 循环论证（自己和自己比）。
- **零覆盖区**：SessionCache 全模块、`_remove_orphaned_tool_messages`、`recall_for_prompt` 编排、`_memory_context` 缓存语义（已知 bug 无回归测试）、guardrails（XML 泄漏/重复调用检测，无 test_guardrails.py）、render 降级路径、DEV_MODE telemetry（实测已损坏）、`name_search`/`keyword_search`（两通道零测试）。
- **为已删除功能维护测试**：`test_critic.py` 269 行；`test_reasoning.py:128-139` 标注 DEPRECATED 且无断言。
- **无覆盖率工具链**：无 pytest-cov、无 coverage 配置。整体行覆盖率预计中低（<50%）。

---

## 7. 宣称 vs 现实对照表

| 宣称（README/CLAUDE.md/ROADMAP） | 现实 |
|---|---|
| "4 步启动" | 依赖清单缺包 + 空库迁移必败，走不通 |
| "539 个测试"（badge） | 收集阶段 ImportError，0 个能跑 |
| "Character Card 决定 WHAT to think" | Card 只进 Render 层，Aggregator 人格中性 |
| "13 无条件 + 3 条件 = 16 工具" | 数量对，但 docstring 写 11 |
| "深档记忆 500 tok / 0.5" | 仅 ReAct 路径生效，pipeline 路径深档也用 300/0.35 |
| "名称精确检索" | 实际是 pg_trgm 模糊匹配，且有 NULL 排序缺陷 |
| "关键字硬过滤" | 实际是两阶段（硬过滤+软补齐），README 描述过时 |
| "RAG v0/v1 共存（P2 技术债）" | 是 P0——它直接弄断了迁移链 |
| ROADMAP P2#6"`_memory_context` 空串 bug" | 已修复（helpers.py:85-87），条目过时 |
| 版本 v0.2.0-beta（ROADMAP） | 代码 0.1.1，tag v0.1.3(beta) |
| `search_local_bangumi` 返回 str（CLAUDE.md 规则 #7） | 实际返回 dict，例外不存在，文档过时 |
| "embedding-2 1024d 已规避 HNSW 上限" | 规避成立，但维度一致性零代码防护 |

---

## 8. 修复路线图

### 第一阶段（1-2 天）：让项目"能装、能迁移、能跑测试、能开 dev 模式"

1. 修 `test/test_rag.py:14-22` 导入名（`_build_*_chunk_text` → `_build_*_rag_dict`），让 `pytest test/` 至少能收集完。
2. `requirements.txt`：补 `langchain-openai`；`langgraph` 锁上界（如 `<0.4` 或精确 pin）；删 `python-dotenv`/`requests` 冗余项。
3. `main.py:541` `astream` 显式传 `stream_mode="updates"`（或改 ainvoke+回调）；render/session_cache.store/响应提取整体纳入 try/except；GraphRecursionError 的 fallback 不要用裸 `Exception`。
4. `clients/base.py` `_request` 最外层 `except Exception → {"_error": ...}`。
5. 重写迁移为显式 DDL，保证空库可初始化（`init_db` 全流程验证）。
6. `eval/eval_classifier.py:98` 解包 tuple。

### 第二阶段（1 周）：产品正确性与隐私

7. 缓存键加 `user_id` 维度；`created_at` 语义修正（更新时刷新或引入独立 `first_seen_at`）。
8. 字数控制改"超限重渲染"（带超限反馈），而非事后截断。
9. 契约回归测试：limit 位置、`get_user_profile` 字段名（拿 openapi yaml 做对照）、X-RateLimit-* 头。
10. 重复调用/空搜索改为注入反馈再给一轮，杜绝无最终文本；`extra_body` 按 provider 条件注入。
11. 第 2 轮起保留精简版终止/真实性 SystemMessage；chat 路径 render 携带最近 N 条历史。
12. 分类器：收窄 realtime 关键词 + 补日期 few-shot；统一置信度阈值单点来源。

### 第三阶段（随后）：工程质量

13. 最小 CI（pytest + ruff，README 建议的 DB 测试可用 docker service）。
14. 版本号单点来源（git tag 驱动 badge）；README/ROADMAP/CLAUDE.md 对账。
15. 删除 v0 遗产（BangumiChunk 模型/表/CLI、300 行旧检索器注释、critic.py + 其测试、死配置）。
16. 进程级共享 httpx 连接池 + `create_llm` 缓存 + 检索器单例。
17. 评测按 category 分组报告；keyword 通道直调生产 retriever；补 name/keyword 通道、guardrails、SessionCache 的测试；修假阳性测试。

---

## 9. 审计局限性与方法说明

- **审计方法**：① 主审计员全量通读核心模块（agent/ 编排、路由、节点、人格、记忆 L1、入口 main.py、配置、客户端）；② 5 个并行子系统深审（记忆系统、RAG/数据层、工具/客户端层、编排/人格层、测试/评测/工程化），全部要求 file:line 级证据；③ 实测验证：临时 venv 安装依赖（解析到 langgraph 1.2.11 / pytest 9.1.1）运行 `pytest --collect-only` 与部分执行，构造等价 FakeApp 复现 DEV_MODE 崩溃。关键 P0 结论均经主审计员逐行交叉验证。
- **环境限制**：本地 Python 3.10（项目要求 3.11+）；无 PostgreSQL、无 DeepSeek/Bangumi API key——`test_integration.py`、`test_rag.py`、`test_memory*` 的真实服务路径未做端到端验证（已通过源码分析与失败行为佐证）。
- **未改动项目代码**；审计产生的临时文件（venv、收集输出等）已全部清理，`git status` 干净。

---

*报告完。修复 P0 清单后建议重跑本审计以复核结论。*
