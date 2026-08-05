# BGM Agent 架构 & 代码审查 — 合并报告

> 来源：Claude Code deep review (2026-08-05) + 三线审计 agent
> 状态：待修复，按优先级排序

---

## 优先级定义

| 级别 | 含义 | 标准 |
|------|------|------|
| 🔴 P0 | 立即修 | 静默数据丢失 / 安全漏洞 / 功能不可用 |
| 🟠 P1 | 本周修 | 行为错误 / 人格断裂 / 关键矛盾 |
| 🟡 P2 | 本迭代修 | 潜在风险 / 维护负担 / 准确性问题 |
| 🟢 P3 |  backlog | 优化 / 文档 / 锦上添花 |

---

## 一、后端 — main.py + FastAPI

### 🔴 P0

| ID | 问题 | 位置 | 影响 | 修复方向 |
|----|------|------|------|---------|
| **B-2** | **session_id 塌缩 — L2 记忆全部丢失** | `main.py:363` | `_remember_session(result, request, depth)` 中 `request.session_id` 为用户传的空字符串。自动生成的 UUID 只写入了 `initial_state["session_id"]`，从未传给 L2 写入函数。所有匿名 session 记忆塌缩为一行，互相覆盖 | 传 `session_id` 变量而非 `request.session_id` |
| **B-3** | **stream 客户端断开 → L1+L2 静默丢失** | `main.py:507-519` | `asyncio.CancelledError` 是 `BaseException` 子类，不被 `except Exception` 捕获。用户关标签页后 cache store + L2 写入全部跳过 | `except (Exception, asyncio.CancelledError)` 并在 CancelledError 分支做 fire-and-forget 写入 |
| **B-4** | **stream 降级回复永远发不出去** | `main.py:496-504` | render 失败时 `cleaned` 写入 `final_state["messages"]`，但 SSE 事件发的是原始 `rendered_reply`（None）。客户端收到 `"reply": null`，降级文本从未到达 | 发送前判断：若 `rendered_reply` 为 None 则用 `cleaned` |
| **B-1** | **DEV_MODE=true 使测试 mock 全部失效** | `main.py:275-284` + `.env` | `_run_with_telemetry` 走 `astream()`，`@patch("main.agent_app.ainvoke")` 不触发 | DEV_MODE 默认为 False；或 `_run_with_telemetry` 也支持 ainvoke 路径 |
| **ERROR_LEAK** | **错误消息泄露内部信息给用户** | `main.py:299` | `f"啧，出错了：{e}"` 将 Exception 的 `__str__` 直接返回。数据库连接串、文件路径、API key 片段可能泄露 | 返回固定兜底文案，异常详情只写日志 |
| **CORS** | **`allow_origins=["*"]` + `allow_credentials=True` 安全反模式** | `main.py:157-163` | 浏览器规范禁止 credentials 与 wildcard 同用，且若生效则任意网站可带 cookie 调 API | 改为显式 origin 列表，或去掉 `allow_credentials` |

### 🟠 P1

| ID | 问题 | 位置 | 影响 | 修复方向 |
|----|------|------|------|---------|
| **C-1** | **`/chat` 与 `/chat/stream` 渲染管线完全重复** | `main.py:310-370` vs `main.py:455-514` | ~150 行重复逻辑。修一处必忘另一处 | 提取 `_render_final_reply()` 共享函数 |
| **A-5** | **无全局异常处理器** | `main.py` | 错误响应格式不统一：Pydantic validation error 返回 422 HTML，LLM 异常返回 JSON，Graph 超限返回另一格式 | 注册 `@app.exception_handler` |
| **A-1** | **Middleware 几乎为零** | `main.py` | 无请求 ID 追踪、无 TrustedHost、无 GZip、无速率限制。多用户场景下无任何保护 | 逐步添加：request ID → TrustedHost → GZip |
| **HEALTH** | **健康检查不检查实际依赖** | `main.py:224-230` | `/health` 返回静态 `{"status": "ok"}`。DB 挂了、LLM API 不可达时仍返回 200。K8s liveness probe 形同虚设 | 增加 DB ping + 可选 LLM API 探活 |
| **TIMEOUT** | **无请求级超时保护** | `main.py:279-284` | graph 卡死（LLM 重试 3×60s）时 HTTP 连接挂 3+ 分钟 | `asyncio.wait_for()` 包裹，或 graph config 加 timeout |

### 🟡 P2

| ID | 问题 | 位置 | 修复方向 |
|----|------|------|---------|
| **STREAM_NO_RECURSION** | `/chat/stream` 缺少 `GraphRecursionError` 捕获 | `main.py:516` | 加 `except GraphRecursionError` 分支，发结构化错误 SSE |
| **NO_MAX_LENGTH** | 输入 `message` 无 max_length | `main.py:34` | `Field(..., min_length=1, max_length=2000)` |
| **BG_TASKS** | `asyncio.create_task` 应改为 `BackgroundTasks` | `main.py:363,514` | FastAPI `BackgroundTasks` 在响应返回后执行且随 lifepan 等待 |
| **DEAD_CRITIC** | 死代码 critic_node ~300 行 | `nodes.py:400-622` | 确认不需要后清理；如需保留，加 `# pragma: no cover` |
| **A-10** | `astream` mode 未显式声明 | `main.py:281,436` | LangGraph `astream` 默认 mode 可能随版本变化。显式声明 `stream_mode="updates"` |
| **PIPELINE_PARALLEL** | Pipeline 节点可并行但未利用 | `graph.py` | fetch/realtime/profile 的 search 步骤可并行（均无依赖）。当前串行浪费 LLM 延迟 |

### 🟢 P3

| ID | 问题 | 修复方向 |
|----|------|---------|
| **NO_ROUTER** | 全部端点挂在 `app` 上 → 686 行 main.py | 拆分为 `routers/chat.py`, `routers/health.py` |
| **NO_DI** | `get_session()` 定义了但从未被 FastAPI Depends 使用 | 端点注入 `session: Session = Depends(get_session)` |
| **CACHE_PURE_MEM** | Session cache 纯内存，重启丢失 | 可选 Redis 后端 |
| **NO_OPENAPI_TAGS** | 端点无 tags，API 文档无分组 | 加 `tags=["chat"]` |

---

## 二、Prompt & Docstring & LangGraph

### 🔴 P0

| ID | 问题 | 位置 | 影响 | 修复方向 |
|----|------|------|------|---------|
| **A1** | **tool-depth 四处矛盾** | 四个 prompt 源 | `_TERMINATION_RULES` 说"必须调 detail" / `TOOL_GUIDANCE` 说"search 已够用" / `_SEARCH_DEPTH_INSTRUCTIONS` 按档位不同 / `_FEW_SHOT_EXAMPLES` 说"不调 detail 就是编造"。同时注入 → LLM 收到完全矛盾的指令 | 统一为：TOOL_GUIDANCE 只给通用原则 + 搜索深度指令按档位覆盖。去掉 _TERMINATION_RULES 中与深度指令冲突的绝对指令 |

### 🟠 P1

| ID | 问题 | 位置 | 影响 | 修复方向 |
|----|------|------|------|---------|
| **B1** | **Few-shot 虚构字段名** | `prompt_builder.py:121-170` | 示例引用 `results[0].name`, `.score`, `.rank`, `.summary`——需验证这些字段在 `search_bangumi_subject` 实际返回中存在。若实际是 `name_cn` / `rating.score` / `rating.rank`，few-shot 系统性误导 LLM | 对照实际 API 返回逐字段核对，修正为真实字段名 |
| **B2** | **Few-shot 示例 5 参数错误** | `prompt_builder.py:163` | `search_bangumi_subject(keyword="花泽香菜")` 默认 `type=subject`，搜作品名而非人物。教 LLM 用错误参数搜声优 | 改为正确的参数（可能需指定 type 或直接用 person 搜索） |
| **A3** | **"今天星期几" 被归为 realtime 但无工具能答** | `classifier.py:44` | 分类器 prompt 示例说 `"今天星期几"` → realtime，但 realtime 工具集 `[calendar, trending, hot_topics]` 全都不能回答。应归 chat | 从 realtime 示例中移除，改归 chat 或常识 |
| **A4** | **`route_by_classification` 注释与代码不符** | `classifier.py:232-234` | 注释写 "profile 降级到 fetch"，代码 `return "fallback"` | 统一为 fallback（更安全），修正注释 |
| **A5** | **Fetch pipeline 有工具但不引导 LLM** | `TOOLS_BY_INTENT["fetch"]` + `graph.py` | `TOOLS_BY_INTENT["fetch"]` 含 `person_detail`/`character_detail`，但 fetch pipeline 节点只绑了 `search` + `detail`，person/character 工具从未被 pipeline 使用 | 要么从 fetch 工具集移除，要么 pipeline 增加 person/character 步骤 |
| **DOUBLE_INJECT** | **deep 模式 Character Card 可能双重注入** | `deep_strategies.py:126` → `prompt_builder.py:400` (deprecated) | `deep_strategies.build_system_prompt()` 调用了旧版 `build_system_prompt()`（含 Character Card + `_render_tone`）。若此路径激活，Character Card 会同时出现在 Aggregator 和 Render | 将 `deep_strategies.build_system_prompt()` 改为调 `build_aggregator_prompt()` |

### 🟡 P2

| ID | 问题 | 位置 | 修复方向 |
|----|------|------|---------|
| **A2** | Scene hint discuss 措辞问题："社区评论作为弹药" | `strategies.py:28` | 改为中性表述，如"参考社区观点" |
| **A6** | `reasoning_node` docstring 说 deep "无 last_chance" 但两种 depth 都注入 | `nodes.py:230` | 修正 docstring |
| **A7** | `_WORD_LIMITS` 残留 v1 的 "facts" 概念 | `prompt_builder.py:373` | `f"## 输出约束\nfacts 中每条 summary 不超过 200 字。"` 中的 "facts" 是 v1 遗留。当前 Aggregator 不输出 facts 字段 |
| **DOC_DRIFT** | 多处 docstring 与实际状态不一致 | 多文件 | 见下方"文档漂移清单" |
| **PROFILE_TRIGGER** | `_PROFILE_TRIGGER = re.compile(r"@\|用户")` 过宽 | `classifier.py:118` | "用户" 出现在大量非 profile 语境（"用户评分"、"其他用户"）→ 改为 `r"@\w+\|分析.{0,5}(品味\|评分习惯\|看番)"` |
| **FEWSHOT_MISSING** | 缺少 profile/realtime/多候选歧义/错误处理 的 few-shot 示例 | `prompt_builder.py:121-170` | 补充 3-4 个示例 |
| **FEWSHOT_NO_NEGATIVE** | 5 个示例全是正确路径，无"不该做什么"的反例 | 同上 | 加 1 个反例：什么时候不调工具 |

### 🟢 P3

- `classifier.py:24`: `from typing import Optional` 未使用
- `TOOLS_BY_INTENT` 闭合括号缩进异常
- LangGraph 未激活能力评估（Checkpointer、interrupt、Send、subgraphs、retry policies）→ 记录为未来迭代参考

---

## 三、Render + 人格系统

### 🔴 P0

| ID | 问题 | 位置 | 影响 | 修复方向 |
|----|------|------|------|---------|
| **R1** | **depth_taste 人格轴完全失效** | `render.py` + `profiles.py` | Aggregator 端硬编码 `depth_taste=0.70/0.90`。Render 端完全不接收 depth_taste 参数。结果：deep 模式搜了 5 轮数据，但 Render 的语气里无任何分析深度指引。`_DEPTH_LEVELS` 5 档文本全部是死代码 | `build_render_prompt()` 增加 `depth_taste` 参数，注入对应 `_DEPTH_LEVELS` 档位文本 |

### 🟠 P1

| ID | 问题 | 位置 | 影响 | 修复方向 |
|----|------|------|------|---------|
| **R2** | **生成后零安全过滤** | `main.py` 渲染后处理 | 回复直接输出给用户。无剧透检测、无年龄适宜性检查、无仇恨/引战言论过滤。ACGN 社区 AI 角色的底线缺失 | 至少加一层规则过滤器（敏感词 + 模式匹配）。可选 LLM 安全检查 |
| **R3** | **bangumi 默认 snark 落 L4 而非 L3** | `profiles.py:420` | 阈值 `≤0.6=L3, ≤0.8=L4`。`snark=0.65 > 0.6` → L4（"标准很高、该被 diss"）。默认角色过于毒舌 | `0.65 → 0.55`，让默认落在 L3 |
| **R4** | **bangumi_cold "话少精准" 无结构性保障** | `profiles.py` + `render.py` | cold 的 initiative=0.25 (L2) 只靠 prompt 建议"说完就停"。字数限制与 bangumi 相同 (200/350)，无额外约束 | cold 应有独立的更紧字数限制（如 120/250） |
| **R6** | **`_STYLE_BASE` 对所有人格注入"聊天"语气** | `render.py:29-34` | neutral 人格被注入"你不是在写报告，你是在聊天"。neutral 应保持客观信息输出 | 按 `character_key` 选择不同的 style base |

### 🟡 P2

| ID | 问题 | 位置 | 修复方向 |
|----|------|------|---------|
| **R5** | `_VOICE` dict 是死代码 | `render.py:45-61` | `_CHARACTER_CARDS` 覆盖全部 4 个 key，`_VOICE` 作为 fallback 永远不会被触发。可移除 |
| **R7** | `_style_modifiers()` 是死代码 | `render.py:65-86` | 定义但无调用点。v2 用 `_pick_level` 替代了它 |
| **R8** | `get_render_tone_variables()` 是死代码 | `profiles.py:235-253` | 仓库中无调用点 |
| **R9** | hard cutoff `rfind("。")` 对无句号文本边界情况 | `render.py:252-254` | 纯列表输出（`- xxx\n- yyy`）无句号 → `rfind` 返 -1 → 直接字符截断，可能截在列表项中间 |
| **R10** | bangumi_cute 在 fast 200 字限制下无法表达完整人格 | `render.py:92` | cute 需要 ~150 字才能完成"安利 + 感受"，200 字仅够一次输出。考虑 fast 给 250 |

### 🟢 P3

| ID | 问题 | 修复方向 |
|----|------|---------|
| **NEUTRAL_WEAK** | neutral Character Card 无审美体系 | 给 neutral 加基本的数据态度描述，不要只是一个 placeholder |
| **CUTE_NO_DIFF** | cute 在需要取舍时缺乏尖锐度（snark=0.15 什么都说好） | 可接受——这是 cute 的设计意图 |
| **COLD_OVERKILL** | cold: snark=0.95 + initiative=0.25 = 每句话毒性浓度极高 | 观察线上反馈后决定是否调 |

---

## 四、文档漂移清单（Docstring/注释与实际代码不符）

| 位置 | 文档写的是 | 实际是 |
|------|-----------|--------|
| `state.py:54` | `"v4: 4→6"` intent | 实际 7 intent（含 profile） |
| `state.py:78` | `depth: auto \| quick \| deep` | 实际 `fast \| deep` |
| `prompt_builder.py:314` | `depth: "auto"` | 实际传入 `"fast"`/`"deep"` |
| `prompt_builder.py:373` | `"facts 中每条 summary"` | v1 遗留，当前 Aggregator 不输出 facts |
| `deep_strategies.py:1` | `"Research Skill 深度意图策略"` | 已合并为单一 Companion Agent |
| `nodes.py:1` | `"v2 纯 ReAct"` | 当前 v5 异质拓扑 |
| `nodes.py:230` | `"deep: ...无强制终止"` | 两种 depth 都注入 last_chance |
| `classifier.py:233` | 注释 `"profile 降级到 fetch"` | 代码 `return "fallback"` |

---

## 五、LangGraph 未激活能力（记录供未来参考）

| 能力 | 潜在用途 |
|------|---------|
| `Checkpointer` (PostgresSaver) | 跨进程持久化 graph 状态，替代纯内存 |
| `interrupt()` / `Command()` | 多候选歧义时暂停等用户选择 |
| `Send` API | 动态并行 fan-out（同时查 3 个候选的 detail） |
| Subgraphs | 将 pipeline 步骤封装为可复用子图 |
| `stream_mode="custom"` | Token 级流式输出 |
| Node retry policies | LLM 调用失败自动重试 |
| Pregel 并行 | 当前 pipeline 节点本质串行 |

---

## 六、建议修复顺序

```
第 1 批（P0，~2h）：修数据丢失 + 安全
  B-2 session_id   → 1 行改
  B-3 CancelledError → 2 行改
  B-4 stream 降级   → 3 行改
  ERROR_LEAK       → 1 行改
  CORS             → 1 行改
  B-1 DEV_MODE     → 默认值改

第 2 批（P0-P1 prompt，~3h）：解矛盾 + 修虚构
  A1 tool-depth 矛盾 → prompt 重写
  B1 Few-shot 字段名 → 对照 API 验证
  B2 Few-shot 示例 5 → 1 行改
  A3 "今天星期几"    → 1 行删
  A4 注释 vs 代码    → 1 行改
  DOUBLE_INJECT    → deep_strategies 切到 build_aggregator_prompt

第 3 批（P0-P1 render，~2h）：修复人格断裂
  R1 depth_taste   → build_render_prompt 加参数
  R3 snark 默认值   → 0.65→0.55
  R6 neutral 语气   → 按 character_key 分支
  R4 cold 字数      → 独立字数限制

第 4 批（P1-P2 工程，~4h）：
  C-1 去重渲染管线
  A-1 中间件（请求 ID + TrustedHost）
  HEALTH 深度检查
  TIMEOUT 请求超时
  A5 Fetch pipeline 工具
  文档漂移批量修正

第 5 批（P2-P3，持续）：
  R2 安全过滤
  R5/R7/R8 死代码清理
  补充 few-shot 示例
  neutral Character Card 强化
  APIRouter 拆分
```

---

*最后更新: 2026-08-05 | 合并两份 review，去重 12 项，新增遗漏 10 项*
