<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/FastAPI-0.115+-009688?style=for-the-badge&logo=fastapi&logoColor=white" alt="FastAPI">
  <img src="https://img.shields.io/badge/PostgreSQL-16_%2B_pgvector-4169E1?style=for-the-badge&logo=postgresql&logoColor=white" alt="PostgreSQL">
  <img src="https://img.shields.io/badge/LangGraph-ReAct-ff6b35?style=for-the-badge" alt="LangGraph">
  <img src="https://img.shields.io/badge/tests-~570-brightgreen?style=for-the-badge" alt="Tests">
  <img src="https://img.shields.io/badge/version-0.1.1-informational?style=for-the-badge" alt="Version">
</p>

<h1 align="center">BGM Agent</h1>
<p align="center">
  <strong>Bangumi 的 AI 看板娘</strong><br>
  <sub>一个住在 <a href="https://bgm.tv">bangumi.tv</a> 站内的、有性格的二次元损友。<br>她可以查数据，但她存在的理由不是查数据——是陪你聊动画。</sub>
</p>

---

## 她是谁

一个 **Companion Agent**（知识型损友），卡在"ChatGPT 通用助手"和"Character.AI 角色扮演"之间——有真实数据支撑的聊天角色。

**四种人格**，一个入口：

| 人格 | key | 风格 | 适合 |
|------|-----|------|------|
| 二次元损友 | `bangumi` | 有态度、有褒贬，数据是吐槽的弹药 | 日常聊天 |
| 高冷腹黑 | `bangumi_cold` | 话少、精准、冷，标准极高，不迎合 | 想听真话 |
| 可爱安利 | `bangumi_cute` | 温暖、真诚、有感染力，像给朋友安利 | 找推荐 |
| 中性助手 | `neutral` | 客观、简洁、信息优先 | 只查数据 |

**三种深度**，对用户透明：

| 模式 | 迭代 | Token 预算 | 适合 |
|------|------|-----------|------|
| `quick` | ≤3 轮 | 6,000 | 快速查询、"这季有什么好看的" |
| `auto` | ≤5 轮 | 10,000 | 日常对话（默认） |
| `deep` | ≤12 轮 | 16,000 | 深度分析、导演风格演变 |

```
用户: EVA 评分怎么样？

BGM Agent (损友): TV版 8.69，旧剧场版 8.86 稳居全站前十。分数确实能打，
                 但我觉得情怀加成不少——真要论精神冲击，还是旧剧场版更凶。
                 你是从 TV 入坑的还是直接看的剧场版？

BGM Agent (高冷): 8.69。站内第22。数据不难查，自己去看。

BGM Agent (可爱): TV版评分8.69，旧剧场版更高！不过分数不是最重要的——
                 第一次看EOE的时候整个人都傻了…你是刚入坑吗？好羡慕你能第一次看！

BGM Agent (中性): TV版《新世纪福音战士》Bangumi评分8.69（全站第22名），
                 剧场版《Air/真心为你》8.86（第7名）。均基于数万用户评价。
```

**她不是：**
- 不是搜索引擎——不会穷举结果，够了就停
- 不是数据看板——不会列评分分布表
- 不是维基百科——不会逐条罗列所有信息
- 不会假装看过没看过的作品——诚实是她审美体系的一部分

---

## 核心特性

- **四种人格 × 三层深度** — 12 种组合，通过 `output_style` + `depth` 参数切换。人格不是 prompt 贴纸——Character Card（System Prompt 层，决定思考）和 Render Node（独立 LLM 调用，决定表达）两层管线独立工作
- **5 档离散人格参数** — snark（毒舌度）、depth_taste（分析深度）、initiative（主动性）各有 5 档 prompt 文本，按阈值查找注入，档位增加不膨胀 System Prompt
- **纯 ReAct 拓扑** — reasoning ⇄ tool → render → END。Critic 已屏蔽，三种深度共享同一推理逻辑，差异仅在预算和人格参数
- **16 个 Bangumi API 工具** — 条目搜索与详情、角色与声优、每日放送、热门趋势、单集讨论、社区评论、用户画像、本地 RAG 语义搜索。13 个无条件可用，3 个需 token
- **L1 短记忆** — 按 depth 三级 Token 预算（6K/10K/16K）+ SystemMessage 永不截断 + 上一轮 ToolMessage 自动压缩（2000→80 tokens）+ 孤儿消息清理，防止 API 400
- **L2 跨会话记忆** — pgvector 语义召回 + 时间衰减（14 天半衰期），fire-and-forget 写入，用户说"上次聊过的那部"时自动注入上下文
- **混合 RAG 检索** — 覆盖"类似命运石之门的烧脑番"这类 API 搜不到的模糊查询
- **SSE 流式输出** — `/chat/stream` 端点，按节点推送 reasoning → tool → render 过程
- **开发者可观测性** — `DEV_MODE=true` 时返回 token 统计 + 节点耗时

---

## 快速开始

### 前置条件

- Python 3.11+
- Docker（用于 PostgreSQL + pgvector）

### 4 步启动

```bash
# 1. 克隆
git clone <repo-url> && cd bgm-agent-dev

# 2. 安装依赖
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 3. 配置（最小可跑：只需要 LLM_API_KEY）
cp .env.example .env
# 编辑 .env，至少填：
#   LLM_MODEL=deepseek-v4-flash
#   LLM_API_KEY=sk-your-key
#   LLM_BASE_URL=https://api.deepseek.com/v1
# 可选：ZHIPU_API_KEY（RAG + L2 记忆）
# 可选：BANGUMI_ACCESS_TOKEN（用户相关工具）

# 4. 启动 PostgreSQL + pgvector，然后运行
docker run -d --name bangumi-pg \
  -e POSTGRES_USER=myuser -e POSTGRES_PASSWORD=mypassword \
  -e POSTGRES_DB=bangumidb -p 5432:5432 pgvector/pgvector:pg16

uvicorn main:app --reload --port 8000
```

### 发第一个请求

```bash
curl -s -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "你好，最近有什么好看的番？", "depth": "auto", "output_style": "bangumi"}' | python3 -m json.tool
```

---

## 配置

`.env` 中的关键环境变量：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `LLM_MODEL` | `gpt-4o` | 推荐 `deepseek-v4-flash` |
| `LLM_API_KEY` | — | DeepSeek / OpenAI / Azure API key |
| `LLM_BASE_URL` | — | 自定义 endpoint |
| `LLM_TEMPERATURE` | `0.3` | 推理温度 |
| `DATABASE_URL` | `postgresql://myuser:mypassword@localhost:5432/bangumidb` | PostgreSQL 连接 |
| `ZHIPU_API_KEY` | — | 智谱 API key（embedding，RAG + L2 记忆需要） |
| `BANGUMI_ACCESS_TOKEN` | — | Bangumi API token（用户相关工具，可选） |
| `MEMORY_ENABLED` | `True` | L2 跨会话记忆开关 |
| `CRITIC_MODE` | `llm` | 自省模式（`llm` 或 `rule`）。当前 Critic 已屏蔽——此配置暂时无效果 |
| `DEV_MODE` | `False` | 开启后 `/chat` 响应附带 token 统计 + 节点耗时 |

完整配置项见 `.env.example` 和 `core/config.py`。

---

## API

### POST /chat

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `message` | `str` | *必填* | 用户消息 |
| `depth` | `"auto"` \| `"quick"` \| `"deep"` | `"auto"` | 深度控制 |
| `output_style` | `"bangumi"` \| `"bangumi_cold"` \| `"bangumi_cute"` \| `"neutral"` | `"bangumi"` | 人格模式 |
| `session_id` | `str` | 自动生成 | 多轮会话 ID |
| `user_id` | `str` | `"anonymous"` | 跨会话记忆用户 ID |

| 响应字段 | 类型 | 说明 |
|----------|------|------|
| `reply` | `str` | Agent 回复 |
| `iterations` | `int` | ReAct 循环轮数 |
| `tools_used` | `list[str]` | 调用的工具列表 |
| `query_intent` | `str` | 意图分类（chitchat/factual/lookup/discovery/realtime/debate/emotional） |
| `output_style` | `str` | 实际使用的人格 |
| `depth` | `str` | 实际使用的深度模式 |
| `telemetry` | `dict` | 开发者可观测性数据（仅 `DEV_MODE=true` 时返回） |

### POST /chat/stream

同上参数，返回 SSE（`text/event-stream`），按节点推送 `reasoning` → `tool` → `render` 事件。

---

## 架构

```
用户请求 (POST /chat)
        │
        ▼
    FastAPI — depth + output_style 参数解析
        │
        ▼
┌──────────────────────────────────────────────┐
│              Companion Agent                  │
│                                               │
│  reasoning_node — LLM invoke（始终绑定工具）   │
│       │                                       │
│       ├── tool_calls → tool_node ──┐          │
│       │                            │          │
│       └── 无 tool_calls            │          │
│              │                     │          │
│              ▼                     │          │
│         render_node                │          │
│   (per-personality voice hint      │          │
│    + 参数感知风格微调)              │          │
│              │                     │          │
│              ▼                     │          │
│             END                    │          │
│                                    ◄──────────┘          │
│                                                          │
│  两层人格管线:                                            │
│    System Prompt (Character Card) → 决定 WHAT to think   │
│    Render Node (独立 LLM 调用)    → 决定 HOW to say it   │
│                                                          │
│  记忆注入: L1 滑动窗口 + 压缩 + L2 语义召回                │
└──────────────────────────────────────────────────────────┘
```

### 四层架构

| 层 | 职责 | 核心文件 | 状态 |
|-----|------|---------|------|
| **编排层** | StateGraph 拓扑、路由、意图分类、策略、护栏 | `agent/orchestrate/`, `agent/graph.py`, `agent/state.py` | 🟡 刚稳定 |
| **人格层** | 4 种 CharacterProfile + 5 档离散参数 + Render 层 per-personality voice hints | `agent/persona/` | 🟡 活跃调参 |
| **记忆层** | L1 滑动窗口 + 工具压缩 + 孤儿清理 + L2 语义召回 + session 缓存 | `agent/memory/` | ✅ 稳定 |
| **数据层** | 16 个工具 + HTTP 客户端 + RAG + pgvector | `tools/`, `clients/`, `rag/`, `database/` | ✅ 稳定 |

**上层依赖下层，下层完全不感知上层。** 数据层不知道谁在用它；编排层知道要调用哪些工具但不知道返回的 dict 长什么样。

---

## 开发

```bash
# 运行全部测试（需要 PostgreSQL + pgvector）
pytest test/ -v

# 仅不需要数据库的测试
pytest test/ --ignore=test/test_rag.py -v

# 仅记忆系统测试
pytest test/test_memory.py test/test_memory_manager.py test/test_phase5_l1.py -v

# 运行批量场景测试（需先启动服务）
./scripts/test_api_v3.sh > docs/test_output/v3_results.md
```

### 项目结构

```
agent/
├── state.py                       # 统一 AgentState（含 depth 字段）
├── graph.py                       # 纯 ReAct StateGraph（Critic 屏蔽但保留注册）
├── llm.py                         # LLM 工厂（多 Provider）
├── devtools.py                    # Token 统计 + 节点计时
├── orchestrate/                   # 编排层
│   ├── nodes.py                   #   reasoning_node + critic_node（保留未路由）
│   ├── strategies.py              #   Companion 浅层 intent 策略
│   ├── deep_strategies.py         #   Deep Scene Hints
│   ├── prompt_builder.py          #   System Prompt 组装（TOOL_GUIDANCE 五合一）
│   ├── classifier.py              #   意图分类 + 深度信号检测
│   ├── guardrails.py              #   终端检测 / XML 泄漏 / 重复调用
│   └── helpers.py                 #   共享辅助函数
├── persona/                       # 人格层
│   ├── profiles.py                #   4 种 CharacterProfile + 5 档离散参数
│   └── render.py                  #   Render Node — per-personality voice hints
├── memory/                        # 记忆层
│   ├── short_term.py              #   L1 滑动窗口 + 工具压缩 + SystemMessage 免疫
│   ├── long_term.py               #   L2 语义召回 + 时间衰减
│   └── cache.py                   #   跨 HTTP 请求 session 缓存
tools/                              # 16 个 LangChain @tool 函数
clients/                            # HTTP 客户端（httpx 异步 + 指数退避重试）+ sanitizers
rag/                                # RAG 检索（语义前缀、分桶排序）
database/                           # SQLModel ORM + pgvector
schemas/                            # Pydantic v2 工具输入 schema
core/                               # pydantic-settings 全局配置
test/                               # ~570 个测试（20 个文件）
scripts/                            # 批量测试脚本（test_api_v2.sh / test_api_v3.sh）
docs/                               # 设计文档、记忆手册、测试输出、产品评测
```

### 调参速查

| 想改的效果 | 文件 | 改什么 |
|-----------|------|--------|
| 切换人格 | 请求参数 | `output_style="bangumi_cold"` / `"bangumi_cute"` |
| 回复太长/太短 | `persona/render.py` | `_WORD_LIMIT` dict |
| 吐槽太狠/太温和 | `persona/profiles.py` | 角色 `snark` 默认值，或 `_SNARK_LEVELS` 文本 |
| 分析太深/太浅 | `persona/profiles.py` | 角色 `depth_taste` 默认值 |
| AI 调了太多轮工具 | `state.py` | `_MAX_ITERATIONS_*` |
| 多轮对话丢上下文 | `memory/short_term.py` | `DEPTH_TOKEN_BUDGETS` |
| 忘了之前聊过什么 | `core/config.py` | `MEMORY_*` 阈值 |
| Render 太保守/太放飞 | `persona/render.py` | `RENDER_TEMPERATURE` |

完整调参手册见 [`CLAUDE.md`](CLAUDE.md)。

---

## 路线图

```
Phase 1-3 ✅    Phase 4 ✅     Phase 5 ✅    Phase 5.5 ✅   Phase 6 ✅
地基             双 Agent       记忆          人格化          纠正错配
──■───────────■─────────────■────────────■──────────────■───────────
FastAPI         拆 Research   L1 滑动窗口   CharacterProfile 合并双 Agent
BangumiClient   + Dialogue    L2 语义召回   AgentProfile      depth 参数
RAG + pgvector  引入 Critic   L3 废弃       角色优先          纯 ReAct 拓扑
第一个 ReAct     ← Tool Agent 错配开始 →

Phase 6.5 ✅    Phase 8 ✅         Phase 9 ✅       ● Phase 10+
解耦风格         Context 重构       人格深化          基础功补强
──■───────────■────────────────■──────────────■──→
render_node     三级 Token 预算    Critic 屏蔽       字数控制机制
极简 prompt     SystemMsg 免疫     5档离散人格       长程记忆增强
风格解耦         TOOL_GUIDANCE     四种人格模式      deep 工具调用保证
                工具结果压缩        Render 重设计      搜索效率优化
```

**当前（v0.1.1）**: 人格系统成熟（4种人格 × 5档参数 × 两层管线），纯 ReAct 拓扑稳定。
**产品评测**: 人格表达 8/10 — 最大竞争力；输出规范 3/10 — 字数控制急需修复。
**详细评测**: [`docs/test_output/product_review_2026-07-28.md`](docs/test_output/product_review_2026-07-28.md)

### 下一步：Phase 10+ 基础功补强

| 优先级 | 任务 | 说明 |
|--------|------|------|
| P0 | 字数控制机制 | auto 模式近半数回复超标，需 render_node 硬截断或加强 prompt 约束 |
| P0 | Deep 模式工具调用保证 | 当前 deep 有时 0 工具闭卷答题，违背产品设计 |
| P0 | 常识 → realtime 误分类 | "今天星期几"不应触发 get_calendar |
| P0 | 搜索空结果快速放弃 | 不存在条目搜 5 轮才停，需 2 轮后终止 |
| P1 | L1 长程多轮记忆 | 8 轮话题跳转后 R8 完全失忆 |
| P1 | Cute 模式推荐差异化 | 避免和 neutral 用同一数据源 |
| P2 | 新人格探索 | 玩梗资历/老宅（otaku mode）等 |

---

## 文档

| 文档 | 内容 |
|------|------|
| [`CLAUDE.md`](CLAUDE.md) | 四层架构详解、调参速查、编码规范 |
| [`docs/design/ROADMAP.md`](docs/design/ROADMAP.md) | 架构状态 & 详细路线图 |
| [`docs/test_output/product_review_2026-07-28.md`](docs/test_output/product_review_2026-07-28.md) | 最新产品评测（45 场景） |
| [`docs/design/`](docs/design/) | 设计决策记录（架构评审、Phase 1-3 审计、记忆系统设计） |
| [`docs/memory/`](docs/memory/) | 记忆系统手册（6 文件） |
| [`docs/Rag/`](docs/Rag/) | RAG 策略与表结构 |
| [`docs/tool-guide.md`](docs/tool-guide.md) | 工具增/改/删操作指南 |

---

## License

MIT © [Ujikintoki](https://github.com/Ujikintoki)
