# BGM Agent 参数审计与调优指南

> 2026-06-16 审计，覆盖 Agent / Tools / RAG / Memory 全部可调参数。
> 标注：✅ 已通过 `.env` 配置化，🔧 硬编码但可调，🔴 存在不一致。

---

## 一、Agent 层

### 1.1 迭代与熔断

| 参数 | 位置 | 当前值 | 状态 | 调优建议 |
|------|------|--------|------|---------|
| Research `_MAX_ITERATIONS` | `agent/research/state.py:70` | **12** | 🔧 | search→detail 串行 2 轮 + 1-2 轮容错，8-10 可能够。12 略宽松 |
| Dialogue `_MAX_ITERATIONS` | `agent/dialogue/state.py:48` | **4** | 🔧 🔴 | Prompt 写"最多 2 轮工具调用"但 graph 允许 4 轮——**与 prompt 不一致** |

### 1.2 Token 预算 (L1 滑动窗口)

| 参数 | 位置 | 当前值 | 状态 | 调优建议 |
|------|------|--------|------|---------|
| `DEFAULT_MAX_TOKENS` | `agent/memory.py:41` | **8000** | 🔧 | Research 总预算。DeepSeek-v4 上下文 1M，可上调至 12000-16000 |
| `DIALOGUE_MAX_TOKENS` | `agent/memory.py:51` | **4000** | 🔧 | Dialogue 总预算。对话简短，当前够用 |
| `L2_MEMORY_BUDGET_TOKENS` | `agent/memory.py:61` | **700** | 🔧 | Research L2 注入预算，与 config `MEMORY_MAX_INJECT_TOKENS` 一致 |
| `L2_MEMORY_BUDGET_DIALOGUE` | `agent/memory.py:64` | **300** | 🔧 | Dialogue L2 注入预算，与 config `MEMORY_DIALOGUE_MAX_INJECT_TOKENS` 一致 |
| `_MAX_SINGLE_MESSAGE_TOKENS` | `agent/memory.py:68` | **1500** | 🔧 | 单条 ToolMessage 最大 token。过大则一条挤占窗口，过小则 LLM 看不到足够数据。`get_subject_discussion` 拉 4 维度各 10 条可能超 1500 |
| 滑动窗口丢弃下限 | `agent/memory.py:312` | **100** tokens | 🔧 | 剩余空间 <100 tokens 时直接丢弃 ToolMessage 而非截断。可下调至 50 |

**Token 预算分配（Research Agent 8000）**：

```
System Prompt (BASE + intent):  ~1200 tokens
L2 记忆注入:                    ≤700 tokens
对话历史:                       ~5100 tokens  ← 可用上调
LLM 输出缓冲:                   ~1000 tokens
```

### 1.3 Critic 规则版阈值 (`agent/research/nodes.py`)

| 参数 | 位置 | 当前值 | 状态 | 调优建议 |
|------|------|--------|------|---------|
| 回复过短阈值 | `nodes.py:427` | **< 10 字** | 🔧 | 已从 20 下调到 10。考虑是否需进一步下调到 5-8 |
| critic_feedback 截断 | `nodes.py:287-293` | **> 200 字** | 🔧 | `build_system_prompt` 中对异常 feedback 的截断。当前合理 |
| Critic 模式 | `core/config.py:165` | **"rule"** | ✅ | `CRITIC_MODE` — rule（零 token）或 llm（三元维度评估） |

### 1.4 意图分类器 (`agent/classifier.py`)

| 参数 | 位置 | 当前值 | 状态 | 调优建议 |
|------|------|--------|------|---------|
| 短消息阈值 | `classifier.py:236` | **< 5 字** | 🔧 | <5 字不匹配规则 → 返回 `unknown`（绑工具），防止 "EVA"、"86" 被 LLM 误判为 chitchat。但对于 1-2 字的极端短消息（如"你好"），可以考虑直接判 chitchat |
| 分类器 LLM max_tokens | `research/nodes.py:89` | **10** | 🔧 | 硬编码在 reasoning_node 中，仅需一个词 |
| 分类器 LLM timeout | `research/nodes.py:89` | **10s** | 🔧 | 同上，轻量场景 |
| 分类器 LLM temperature | `research/nodes.py:89` | **0** | 🔧 | 显式传 temperature=0 保证确定性 |
| `INTENT_RULES` | `classifier.py:31-170` | — | 🔧 | 关键词/正则列表。每次误分类需手动加词，考虑是否需要外置到配置文件 |

### 1.5 LLM 调用参数

| 场景 | temperature | max_tokens | timeout | 位置 |
|------|------------|------------|---------|------|
| **Reasoning 推理** | 0.3 (config) | 4096 (config) | 60s (config) | `agent/llm.py` |
| **Dialogue 对话** | 0.3 (config) | 4096 (config) | 60s (config) | `agent/llm.py` |
| **Classifier 分类** | 0 (hardcoded) | 10 (hardcoded) | 10s (hardcoded) | `research/nodes.py:89` |
| **Critic LLM 评估** | 0 (hardcoded) | 4096 (config) | 60s (config) | `research/nodes.py:498` |
| **Memory 摘要** | 0 (hardcoded) | **500** (hardcoded) | 10s (hardcoded) | `memory_manager.py:484-487` |

Critic 可选模型：`LLM_CRITIC_MODEL` (config) — 留空用 `LLM_MODEL`，可设为更便宜的小模型。

---

## 二、Memory 记忆系统

### 2.1 L2 召回参数

| 参数 | 位置 | 当前值 | 状态 | 调优建议 |
|------|------|--------|------|---------|
| `MEMORY_RECALL_TOP_K` | `core/config.py:129` | **5** | ✅ | 语义检索召回 session 数。加大 → 更多历史注入 → 更多 token 消耗 |
| `MEMORY_RECALL_THRESHOLD` | `core/config.py:132` | **0.5** | ✅ | 语义通道主阈值。越小越严格 |
| `MEMORY_RECENCY_FALLBACK_THRESHOLD` | `core/config.py:151` | **0.60** | ✅ | 时效回退锚定阈值。比主阈值 (0.5) 宽松——回退记忆是近期对话，允许较弱语义匹配。**注意：RAG 检索用 0.65，和这里不一致** |
| `MEMORY_TIME_DECAY_HALF_LIFE_DAYS` | `core/config.py:142` | **14** | ✅ | 时间衰减半衰期。14 天：1天→0.95, 7天→0.71, 14天→0.50, 30天→0.23 |
| `MEMORY_MAX_INJECT_TOKENS` | `core/config.py:126` | **700** | ✅ | Research 记忆注入 token 预算 |
| `MEMORY_DIALOGUE_MAX_INJECT_TOKENS` | `core/config.py:158` | **300** | ✅ | Dialogue 记忆注入 token 预算 |
| `MEMORY_MIN_SESSIONS_FOR_PROFILE` | `core/config.py:135` | **5** | ✅ | L3 画像冷启动保护 |

### 2.2 L2/L3 内部硬编码参数 (`agent/memory_manager.py`)

| 参数 | 位置 | 当前值 | 调优建议 |
|------|------|--------|---------|
| 摘要长度限制 | `memory_manager.py:31` (prompt) | **不超过 200 字** | 复杂多轮对话可能偏短，可上调至 300 字 |
| 摘要 LLM max_tokens | `memory_manager.py:486` | **500** | 含 JSON wrapper，目前够用 |
| 摘要 LLM timeout | `memory_manager.py:487` | **10s** | fire-and-forget 场景，够用 |
| `_format_conversation_text` max_tokens | `memory_manager.py:549` | **3000** | 发给摘要 LLM 的对话历史上限 |
| 注入时摘要截断 | `memory_manager.py:906` | **`[:150]` 字** | 每条记忆在 prompt 中的显示长度 |
| 新实体初始兴趣分 | `memory_manager.py:721,803` | **0.5** | 中位初始值 |
| 实体亲和度 EMA | `memory_manager.py:798` | **0.9 × old + 0.1** | 衰减系数——越大旧记忆越"顽固" |
| 实体亲和截断 | `memory_manager.py:812` | **top-20** | 保留最近 20 个感兴趣的实体 |
| 偏好类型截断 | `memory_manager.py:772` | **top-10** | 保留 top-10 偏好类型 |
| avg_session_length EMA | `memory_manager.py:690` | **0.8 × old + 0.2** | 平均会话长度的指数移动平均 |
| 画像摘要 top-N | `memory_manager.py:957-968` | **top-2 类型 + top-2 实体** | 精简策略 |

---

## 三、Tools 层 (`tools/bgm_tools.py`)

### 3.1 工具默认拉取量

| 工具 | 参数 | 默认值 | 状态 | 调优建议 |
|------|------|--------|------|---------|
| `search_bangumi_subject` | `limit` | **5** | 🔧 | 太少可能漏掉目标（如 "EVA" 返回 TV+剧场版×4 瞬间填满），太多浪费 token。可上调至 8 |
| `get_calendar` | `limit_per_day` | **10** | 🔧 | 每日番剧数，当前合理 |
| `get_trending_topics` | `limit` | **10** | 🔧 | 热门条目/讨论数，当前合理 |
| `get_episode_comments` | `comments_limit` | **30** (最大 200) | 🔧 | 单集吐槽数。30 条 ≈ 1000-2000 tokens |
| `get_subject_discussion` | `limit` | **10** | 🔧 | 每个维度（评论/评测/讨论/剧集）的条数。4 维度 × 10 = 40 条，可能超 `_MAX_SINGLE_MESSAGE_TOKENS` |
| `get_entity_comments` | `limit` | **20** | 🔧 | 角色/人物评论数 |
| `get_user_profile` | `collections_limit` | **50** | 🔧 | 用户收藏数。50 条收藏 ≈ 1500-3000 tokens |
| `get_user_timeline` | `limit` | **20** (最大 50) | 🔧 | 动态条数 |
| `search_local_bangumi` | `limit` | **5** | 🔧 | RAG 检索结果数 |

这些默认值通过 Pydantic `Field(default=...)` 暴露给 LLM 的 tool schema，LLM 倾向于接受默认值。调整它们可以显著改变工具返回的数据量和下游 token 消耗。

### 3.2 格式化截断阈值

| 位置 | 参数 | 当前值 | 影响 |
|------|------|--------|------|
| `_format_subject_detail_full` | 标签截断 | **`[:10]`** | 全量详情中标签数 |
| `_format_subject_detail_discovery` | 标签截断 | **`[:3]`** | discovery 模式中标签数 |
| `search_local_bangumi` | RAG 结果标签 | **`[:5]`** | RAG 搜索结果中标签数 |
| `search_local_bangumi` | chunk_text 截断 | **`[:150]` 字** | RAG 结果中简介文本 |
| `get_user_profile` | 收藏列表 | **`[:10]`** | |
| `get_user_profile` | 角色列表 | **`[:10]`** | |
| `get_user_profile` | 人物列表 | **`[:10]`** | |
| `get_user_profile` | 日志列表 | **`[:5]`** | |
| `get_blog` | 评论列表 | **`[:10]`** | |
| `get_blog` | 关联条目 | **`[:10]`** | |
| `get_user_timeline` | 动态文本截断 | **`[:100]` 字** | 每条动态正文 |

### 3.3 派生信号阈值 (`_compute_subject_signals`)

| 信号 | 边界值 | 标签 |
|------|--------|------|
| 完成率 | ≥85% / ≥60% / ≥35% / else | 高 → 正常 → 偏低 → 低 |
| 口碑集中度 | ≥75% / ≥50% / ≥35% / else | 一致好评 → 正常 → 两极化 → 严重两极 |
| 热度评分比 | <0.3 / <1.0 / <3.0 / else | 冷门高分 → 小众精品 → 正常 → 热门 |
| 最小评分人数 | **> 100** | 低于此值不计算完成率 |

---

## 四、RAG 层

### 4.1 检索器 (`rag/retriever.py`)

| 参数 | 位置 | 当前值 | 状态 | 调优建议 |
|------|------|--------|------|---------|
| `distance_threshold` | `hybrid_search:141` | **0.65** | 🔧 🔴 | **与 memory 系统不一致**：Memory 主阈值 0.5 / fallback 0.6，RAG 用 0.65。RAG 更宽松——搜索结果宁可多一些让 LLM 筛选。需确认这是有意为之 |
| `semantic_bucket_size` | `hybrid_search:142` | **0.03** | 🔧 | 语义梯队步长。调小 → 梯队更细 → 热度影响更窄。调大 → 更多结果挤在同桶 → 热度决定性更大 |
| `candidate_limit` | `hybrid_search:186` | **`limit × 2`** | 🔧 | 候选集倍数。`×2` 意味着最多一半被阈值过滤 |

### 4.2 文本处理器 (`rag/text_processor.py`)

| 参数 | 位置 | 当前值 | 状态 | 调优建议 |
|------|------|--------|------|---------|
| `chunk_size` | 构造函数 | **300** tokens | 🔧 | ~400-600 中文字。Zhipu embedding-3 最优 chunk 通常在 256-512 之间，300 合理。若检索精度不够可尝试 400-500 |
| `chunk_overlap` | 构造函数 | **50** tokens | 🔧 | 重叠率 50/300=16.7% |

### 4.3 摄入 (`rag/ingestion.py`)

| 参数 | 位置 | 当前值 | 调优建议 |
|------|------|--------|---------|
| 角色 casts 截断 | `_rerank_casts:217` | **top-10** | 每角色最多保留 10 部相关作品 |
| 人物 works 截断 | `_rerank_works:270` | **top-10** | 每人物最多保留 10 部代表作 |

### 4.4 热度信号对数归一化 (`_extract_heat_signal`)

| 参数 | 当前值 | 说明 |
|------|--------|------|
| 归一化公式 | `math.log(1 + raw)` | 把指数级差距（50000 vs 200 = 250x）压缩到对数级（~2x） |
| Subject 热度源 | `meta_info.rating_total` | 评分人数 |
| Character/Person 热度源 | `meta_info.collects` | 收藏数 |

---

## 五、Guardrails (`agent/guardrails.py`)

| 参数 | 当前值 | 调优建议 |
|------|--------|---------|
| `TERMINAL_RESPONSE_PATTERNS` | 15 个正则模式 | 硬编码。漏掉的终端回复模式需要手动加，考虑是否需要更通用的判断方式 |
| `TOOL_CALL_XML_BLOCK` | `<function_calls>...</function_calls>` | DeepSeek DSML 格式，当前覆盖够用 |
| 重复调用检测 | 连续两轮，相同名称+相同参数 | 合理 |

---

## 六、全局不一致与建议

### 🔴 不一致项

| 问题 | 详情 |
|------|------|
| **余弦距离阈值不统一** | Memory 0.5/0.6 vs RAG 0.65。语义空间相同（embedding-3），应明确差异理由或对齐 |
| **Dialogue 迭代数不一致** | `_MAX_ITERATIONS=4` vs prompt "最多 2 轮工具调用"。模型被 prompt 约束为 2 轮，但 graph 允许 4 轮——有效迭代数被 prompt 而非 graph 控制 |
| **Memory 注入预算冗余定义** | `agent/memory.py` 中 `L2_MEMORY_BUDGET_TOKENS=700` 与 `core/config.py` 的 `MEMORY_MAX_INJECT_TOKENS=700` 重复定义 |

### 🟡 建议优先考虑的调优项

1. **`_MAX_SINGLE_MESSAGE_TOKENS = 1500`**：考虑按工具类型差异化截断。`get_subject_discussion` 多维度数据需要更大空间。

2. **搜索默认 `limit=5`**：对热门关键词（"EVA"）可能不够。可上调到 8 或让 LLM 在需要时主动传更大 limit。

3. **`chunk_size=300`**：如果检索精度不够，尝试 400-500。

4. **`DEFAULT_MAX_TOKENS = 8000`**：DeepSeek-v4 支持 1M 上下文，8000 偏保守。如果经常看到截断日志，上调到 12000-16000。

5. **意图分类短消息处理**：<5 字 → `unknown` 的策略对 "EVA"、"86" 正确，但对 "你好" (2 字) 浪费了一次绑工具推理。考虑对 1-2 字消息直接判 chitchat。

6. **`semantic_bucket_size = 0.03`**：基于 Bangumi 简介的向量距离分布（通常 0.02-0.08）设置。如果数据分布变了，需要相应调整。

---

## 七、.env 可配置参数速查

以下是 `core/config.py` 中所有可通过 `.env` 调整的参数及其默认值：

```bash
# LLM
LLM_TEMPERATURE=0.3
LLM_MAX_TOKENS=4096
LLM_REQUEST_TIMEOUT=60.0
LLM_MODEL=gpt-4o
LLM_BASE_URL=           # DeepSeek/Qwen 等自定义 endpoint
LLM_CRITIC_MODEL=        # 留空用 LLM_MODEL

# Memory
MEMORY_ENABLED=true
MEMORY_MAX_INJECT_TOKENS=700
MEMORY_RECALL_TOP_K=5
MEMORY_RECALL_THRESHOLD=0.5
MEMORY_MIN_SESSIONS_FOR_PROFILE=5
MEMORY_TIME_DECAY_HALF_LIFE_DAYS=14
MEMORY_RECENCY_FALLBACK_THRESHOLD=0.60
MEMORY_DIALOGUE_MAX_INJECT_TOKENS=300

# Critic
CRITIC_MODE=rule         # "rule" | "llm"

# Embedding
EMBEDDING_MODEL=embedding-3
EMBEDDING_DIMENSION=2048
```

---

## 八、参数调整记录

| 日期 | 参数 | 旧值 | 新值 | 原因 |
|------|------|------|------|------|
| 2026-06-13 | `_MAX_ITERATIONS` (Research) | 10 | 12 | 更宽松的迭代容错 |
| 2026-06-13 | `_MAX_ITERATIONS` (Dialogue) | 3 | 4 | 留出 search→detail 串行 + 重试余量 |
| 2026-06-13 | Critic 回复过短 | <20 字 | <10 字 | 减少对合法短回复的误伤 |
| 2026-06-13 | `_MAX_SINGLE_MESSAGE_TOKENS` | 2000 | 1500 | 收紧单条消息预算 |
| 2026-06-13 | `MEMORY_RECENCY_FALLBACK_THRESHOLD` | 0.70 | 0.60 | 更严格的锚定过滤，减少不相关近期记忆 |
| 2026-06-13 | Memory 摘要 LLM max_tokens | 300 | 500 | 容纳 JSON wrapper |
| 2026-06-16 | RAG 热度信号 | rating_total 原始值 | `log(1+raw)` 对数归一化 | 防热门碾压冷门 |
| 2026-06-16 | RAG 同源去重 | 无 | MMR 按 name 去重 | 防 TV/OVA/剧场版刷屏 |
