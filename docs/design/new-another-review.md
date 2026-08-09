# BGM Agent 架构 & 代码审查 — 合并报告 v2

> 来源：Claude Code deep review (2026-08-05) + 三线审计 agent + 产品定位校准 (2026-08-06)
> 状态：Section 1 ✅ 已修复；Section 2 ✅ 已修复；Section 3 死代码已清理 + depth_taste 数据流已修复，人格内容待用户 DIY

---

## 产品定位参考

详见 [`claude-on-bangumi-vision.md`](claude-on-bangumi-vision.md)。修复 Section 2、3 时必须遵循以下原则：

| 原则 | 含义 |
|------|------|
| **reasoning ≠ 人格** | reasoning 层只负责调工具、返回结构化数据，**不输出观点**。观点和表达是 render 层的事 |
| **多工具、浅返回** | 该调的工具都调（search → detail，discuss 加 opinions/comments），但每个工具返回精简准确 |
| **depth_taste ∈ reasoning** | depth_taste 只控制 reasoning 层搜多深（不是调多少工具），不控制 render 表达深度 |
| **neutral = 开发者工具** | 无性格、纯数据罗列。不给它加"客观"或"审美"属性 |
| **cold = 高冷反差萌** | kuudere 二次元人格，不是冷冰冰的数据播报器 |

---

## 优先级定义

| 级别 | 含义 | 标准 |
|------|------|------|
| 🔴 P0 | 立即修 | 静默数据丢失 / 安全漏洞 / 功能不可用 / 关键指令矛盾 |
| 🟠 P1 | 本周修 | 行为错误 / 人格断裂 / 工具链断裂 |
| 🟡 P2 | 本迭代修 | 潜在风险 / 维护负担 / 准确性问题 / 死代码 |
| 🟢 P3 | backlog | 优化 / 文档 / 锦上添花 |

---

## 一、后端 — main.py + FastAPI ✅ 已修复

### 🔴 P0 — 全部已修复 (2026-08-05)

| ID | 问题 | 修复 |
|----|------|------|
| **B-2** | session_id 塌缩 → L2 记忆丢失 | `_remember_session()` 加 `session_id` 参数 |
| **B-3** | CancelledError 不被捕获 → stream 数据静默丢失 | 独立 `except CancelledError` + `asyncio.shield` 紧急保存 |
| **B-4** | stream 降级回复发 null | `reply_to_send` 变量替代 `rendered_reply`(None) |
| **B-1** | DEV_MODE=true 使测试 mock 失效 | conftest autouse fixture → `settings.DEV_MODE=False` |
| **ERROR_LEAK** | 异常信息泄露 | `/chat` + `/chat/stream` 固定兜底文案 |
| **CORS** | `allow_origins=["*"]` + credentials | 移除 `allow_credentials=True` |

### 架构决策 (2026-08-05)

- **reasoning 层全部 `ainvoke`**：`/chat/stream` 从 `astream` 改为 `ainvoke`，移除 per-node SSE 事件
- **`/chat/stream` 新增 `GraphRecursionError` 捕获**
- **DEV_MODE 保留 `astream` 用于 per-node 计时**（仅开发诊断，不影响生产路径）

### 🟠 P1 — 待修复

| ID | 问题 | 位置 | 修复方向 |
|----|------|------|---------|
| **C-1** | `/chat` 与 `/chat/stream` 渲染管线完全重复 | `main.py:310-370` vs `main.py:430-482` | 提取 `_render_final_reply()` 共享函数 |
| **A-5** | 无全局异常处理器 | `main.py` | 注册 `@app.exception_handler` |
| **A-1** | Middleware 几乎为零 | `main.py` | 逐步添加：request ID → TrustedHost → GZip |
| **HEALTH** | 健康检查不检查实际依赖 | `main.py:224-230` | 增加 DB ping + 可选 LLM API 探活 |
| **TIMEOUT** | 无请求级超时保护 | `main.py` | `asyncio.wait_for()` 或 graph config timeout |

### 🟡 P2

| ID | 问题 | 修复方向 |
|----|------|---------|
| **NO_MAX_LENGTH** | 输入 `message` 无 max_length | `Field(..., min_length=1, max_length=2000)` |
| **BG_TASKS** | `asyncio.create_task` → `BackgroundTasks` | 响应返回后执行 + lifepan 等待 |
| **DEAD_CRITIC** | 死代码 critic_node ~300 行 | 确认后清理 |
| **A-10** | `astream` mode 未显式声明 | 仅 `_run_with_telemetry` 仍使用，显式声明 `stream_mode="updates"` |
| **PIPELINE_PARALLEL** | Pipeline 节点可并行但未利用 | fetch/realtime/profile 的 search 步骤可并行 |

### 🟢 P3

| ID | 问题 | 修复方向 |
|----|------|---------|
| **NO_ROUTER** | 全部端点挂在 `app` 上 | 拆分为 `routers/chat.py`, `routers/health.py` |
| **NO_DI** | `get_session()` 从未被 Depends 使用 | 端点注入 |
| **CACHE_PURE_MEM** | Session cache 纯内存 | 可选 Redis |
| **NO_OPENAPI_TAGS** | 端点无 tags | 加 `tags=["chat"]` |

---

## 二、Prompt & Docstring & LangGraph

### 🔴 P0 — 已修复

| ID | 问题 | 修复 |
|----|------|------|
| **A1** | `TOOL_GUIDANCE` 越过 depth_taste 自行禁止调 detail | 删"search 已够用"一句，改为"搜索深度遵循下方搜索深度指令"（`prompt_builder.py:184-186`） |

### 🟠 P1 — 4/5 已修复，1 项移除

| ID | 问题 | 修复 |
|----|------|------|
| **B1** | Few-shot 虚构字段名 | Example 4: `summary` → `info`（search 不返回 summary，info 才是实际字段） |
| **B2** | Few-shot 示例 5 参数错误 | `search_bangumi_subject(keyword="花泽香菜")` → 加 `entity_type="person"` |
| **A3** | "今天星期几" 被归为 realtime 但无工具能答 | 从 realtime 示例移除（`classifier.py:44`） |
| **A4** | `route_by_classification` 注释与代码不符 | 注释统一为"降级到 fallback"（`classifier.py:233`） |
| ~~A5~~ | ~~Fetch pipeline 工具集含 person/character~~ | **移除**——按"工具全量暴露、prompt 控深度"设计，这是正确的。extra tools 仅在 fetch 降级到 ReAct 时激活 |

### 🟡 P2 — 5/9 已修复，4 项待定

| ID | 问题 | 状态 |
|----|------|------|
| **A6** | `reasoning_node` docstring 说 deep "无 last_chance" | ✅ 已修正 |
| **A7** | `_WORD_LIMITS` 残留 v1 "facts" 概念 | ✅ 已修正 |
| **DOC_DRIFT** | 多处 docstring 与实际不一致 | ✅ 全部 8 处已修正 |
| **DOUBLE_INJECT** | `deep_strategies.build_system_prompt()` 调废弃函数 | ✅ 已删除（连同 `prompt_builder.build_system_prompt()` + `_render_tone()`，共 4 个符号） |
| **A2** | Scene hint discuss 措辞："社区评论作为弹药" | 🖐 内容项，留给用户 |
| **PROFILE_TRIGGER** | `_PROFILE_TRIGGER` 正则过宽 | 🖐 语义项，留给用户 |
| **FEWSHOT_MISSING** | 缺少 profile/realtime/错误处理示例 | 🖐 内容项，留给用户 |
| **FEWSHOT_NO_NEGATIVE** | 5 个示例全正路径无反例 | 🖐 内容项，留给用户 |

### 🟢 P3 — 全部已修复

- ~~`classifier.py:24`: `from typing import Optional` 未使用~~ ✅ 已删除
- ~~`TOOLS_BY_INTENT` 闭合括号缩进异常~~ ✅ 已统一
- LangGraph 未激活能力评估 → 供未来参考

---

## 三、Render + 人格系统

> **设计确认**：depth_taste 只控制 reasoning 层搜索深度，不影响 render 表达。R1 从原 review 移除。

### 🔴 P0

*无。（原 R1 移除——depth_taste 不进入 render 是正确设计。）*

### 🟠 P1

| ID | 问题 | 位置 | 影响 | 修复方向 |
|----|------|------|------|---------|
| **COLD_REDESIGN** | **bangumi_cold 人格完全偏离产品定位** | `profiles.py:327-347,455-472` | 当前 cold = "高冷腹黑的 ACGN 评论家"，`snark=0.95`（L5 毒舌全开）+ `initiative=0.25`（L2 寡言）。造出来的是一个毒舌寡言的 critic，不是 kuudere。产品定位：**高冷反差萌二次元人格**——外表高冷但有关心、有萌点、偶尔露出柔软一面。Character Card、identity、expression_guide、snark、initiative 全部需要重设 | 重新设计 cold 人格参数：降 snark（毒舌→高冷不是一回事）、适当提 initiative（话少但不是沉默）、重写 Character Card |
| **R3** | **bangumi 默认 snark 落 L4 而非 L3** | `profiles.py:420` | 阈值 `≤0.6=L3, ≤0.8=L4`。`snark=0.65 > 0.6` → L4（"标准很高、该被 diss"）。默认角色过于毒舌 | `0.65 → 0.55` |
| **R2** | **生成后零安全过滤** | `main.py` 渲染后处理 | 回复直接输出。无剧透检测、无年龄适宜性检查、无仇恨/引战言论过滤 | 至少加一层规则过滤器 |

### 🟡 P2 — 3/4 已修复，1 项待定

| ID | 问题 | 状态 |
|----|------|------|
| **R5** | `_VOICE` dict 死代码 | ✅ 已删除（`render.py`） |
| **R7** | `_style_modifiers()` 死代码 | ✅ 已删除（`render.py`） |
| **R8** | `get_render_tone_variables()` 死代码 | ✅ 已删除（`profiles.py`） |
| **R9** | hard cutoff 无句号文本边界 | ✅ 已修复——fallback 到换行截断 |
| **R10** | cute fast 200 字太紧 | 🖐 内容项，留给用户 |

### 🟡 P2 — Phase 3 prep 新增修复

| ID | 问题 | 状态 |
|----|------|------|
| **DEPTH_TASTE_HARDCODE** | `nodes.py:273` `depth_taste` 硬编码 0.90/0.70，角色实例值从未被读取 | ✅ 已修复——改为 `character.depth_taste`（cold=0.90, bangumi=0.70, cute=0.50, neutral=0.40 现在真正生效） |
| **DEAD_CHAIN_4** | `_render_tone()` → `prompt_builder.build_system_prompt()` → `deep_strategies.build_system_prompt()` 四层死代码链（含 `_DEPTH_LEVELS`） | ✅ 已删除（~380 行，5 个文件） |

### 🟢 P3

| ID | 问题 | 修复方向 |
|----|------|---------|
| **CUTE_NO_DIFF** | cute 在需要取舍时缺乏尖锐度（snark=0.15 什么都说好） | 可接受——cute 的设计意图 |
| **NEUTRAL_DEVTOOL** | neutral 是开发者测试工具，无人格需求 | 无需修改。当前 `_STYLE_BASE` 注入"聊天"语气对 neutral 无影响——neutral 不面向用户 |
| ~~_DEPTH_LEVELS~~ | ~~`profiles.py` 中 5 档 `_DEPTH_LEVELS` 文本无调用方~~ | ✅ 已删除（2026-08-09，Phase 3 prep） |

### 已移除条目

| 原 ID | 移除原因 |
|-------|---------|
| **R1** (depth_taste 人格轴失效) | 不是 bug——depth_taste 只控制 reasoning 搜索深度，不进入 render。正确设计 |
| **R6** (`_STYLE_BASE` 对所有人生注入"聊天"语气) | neutral 是开发者测试工具，无影响。生产人格都有 Character Card 主导表达 |
| **R4** (cold 字数限制) | 被 COLD_REDESIGN 取代——cold 问题不是字数，是整个人格参数设计错误 |
| **NEUTRAL_WEAK** | neutral 不需要审美体系——它是开发者测试工具 |

---

## 四、文档漂移清单 ✅ 全部已修正

| 位置 | 原内容 | 修正为 |
|------|--------|--------|
| `state.py:54` | `"v4: 4→6"` intent | `"v4: 7 intent"` |
| `state.py:78` | `depth: auto \| quick \| deep` | `fast \| deep` |
| `prompt_builder.py:313` | `depth: str = "auto"` | `"fast"` |
| `prompt_builder.py:372` | `"facts 中每条 summary"` | 当前 Aggregator 输出格式 |
| `deep_strategies.py:1` | `"Research Skill 深度意图策略"` | `"Companion Agent"` |
| `nodes.py:1` | `"v2 纯 ReAct"` | `"v5 异质拓扑"` |
| `nodes.py:231` | `"deep: ...无last_chance"` | 两种 depth 都注入 last_chance |
| `classifier.py:233` | 注释 `"profile 降级到 fetch"` | `"降级到 fallback"` |
| `profiles.py:14` (Phase 3 prep) | `_render_tone()` 描述为活跃接口 | 参数分流：snark/initiative → Render，depth_taste → Aggregator |
| `profiles.py:40` (Phase 3 prep) | `CharacterProfile` docstring 引用 `_render_tone()` | 引用 `_pick_level()` + `get_aggregator_depth_instruction()` |
| `nodes.py:5-6` (Phase 3 prep) | `"depth_taste=0.90"` 硬编码 | `"depth_taste 来自角色实例"` |
| `profiles.py:139` (Phase 3 prep) | 搜索深度注释引用已删除的 `_DEPTH_LEVELS` | 改为 `"depth_taste 参数在 reasoning 层的唯一作用点"` |

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
第 1 批（✅ 已完成 — Section 1 全部 P0 + 架构决策）：
  B-2~B-4, B-1, ERROR_LEAK, CORS, stream ainvoke 替换 astream

第 2 批（✅ 已完成 — Section 2 全部 P0-P1 + 文档漂移 + 死代码）：
  A1 TOOL_GUIDANCE, B1 few-shot 字段, B2 few-shot 参数
  A3 今天星期几, A4 注释修正, A6-A7 docstring
  R5/R7/R8 死代码删除, R9 hard cutoff
  DOC_DRIFT 全部 8 处, classifier import, TOOLS_BY_INTENT 缩进

第 3 批 prep（✅ 已完成 — 死代码链 + depth_taste 数据流修复）：
  DEAD_CHAIN_4: 删除 _DEPTH_LEVELS + _render_tone() + prompt_builder.build_system_prompt() + deep_strategies.build_system_prompt()（~380 行）
  DEPTH_TASTE_HARDCODE: nodes.py 硬编码 → character.depth_taste
  test_prompts.py: 废弃函数测试清理 + 预存 intent key bugfix

第 3 批（🖐 用户 DIY — 人格软内容）：
  COLD_REDESIGN → Character Card + 参数值
  R3 snark 默认值
  R10 cute 字数
  A2 discuss 措辞
  所有 Character Cards 复核

第 4 批（P1 后端 + P2，待修）：
  C-1 去重渲染管线
  A-1 中间件（request ID + TrustedHost）
  HEALTH 深度检查
  TIMEOUT 请求超时

第 5 批（P2-P3，持续）：
  R2 安全过滤
  补充 few-shot 示例
  APIRouter 拆分
  DEAD_CRITIC 清理（~300 行）

---

*最后更新: 2026-08-09 | v3: Phase 3 prep — 死代码链清理 + depth_taste 数据流修复；审查报告同步*
