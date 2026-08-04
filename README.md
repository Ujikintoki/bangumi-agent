<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/FastAPI-0.115+-009688?style=for-the-badge&logo=fastapi&logoColor=white" alt="FastAPI">
  <img src="https://img.shields.io/badge/PostgreSQL-16_%2B_pgvector-4169E1?style=for-the-badge&logo=postgresql&logoColor=white" alt="PostgreSQL">
  <img src="https://img.shields.io/badge/LangGraph-ReAct-ff6b35?style=for-the-badge" alt="LangGraph">
  <img src="https://img.shields.io/badge/tests-570+-brightgreen?style=for-the-badge" alt="Tests">
  <img src="https://img.shields.io/badge/version-0.1.1-informational?style=for-the-badge" alt="Version">
</p>

<h1 align="center">BGM Agent</h1>
<p align="center">
  <strong>Bangumi 的 AI 看板娘</strong><br>
  <sub>一个住在 <a href="https://bgm.tv">bangumi.tv</a> 站内的、有性格的二次元损友。<br>她可以查数据，但她存在的理由不是查数据——是陪你聊动画。</sub>
</p>

---

## 她是谁

一个 **Companion Agent（知识型损友）**，卡在"ChatGPT 通用助手"和"Character.AI 角色扮演"之间——有真实数据支撑的聊天角色。

**四种人格**，一个入口：

| 人格 | key | 风格 | 适合 |
|------|-----|------|------|
| 二次元损友 | `bangumi` | 有态度、有褒贬，数据是吐槽的弹药 | 日常聊天 |
| 高冷腹黑 | `bangumi_cold` | 话少、精准、冷，标准极高，不迎合 | 想听真话 |
| 可爱安利 | `bangumi_cute` | 温暖、真诚、有感染力，像给朋友安利 | 找推荐 |
| 中性助手 | `neutral` | 客观、简洁、信息优先 | 只查数据 |

**两种深度**，对用户透明：

| 模式 | 适合 |
|------|------|
| `fast` | 日常对话、快速查询（默认） |
| `deep` | 深度分析、多步探索、导演风格演变 |

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

- **四种人格 × 两种深度** — 8 种组合，通过 `output_style` + `depth` 参数切换。两层独立管线：Character Card（System Prompt 层，决定思考方式）和 Render Node（独立 LLM 调用，决定语言风格）
- **5 档离散人格参数** — 毒舌度、分析深度、主动性各有 5 档，随 depth 和人格自动调节。档位增加不膨胀 System Prompt
- **异质拓扑（Pipeline + ReAct）** — fetch/realtime/profile 走确定性 pipeline 节点，explore/discuss 走 ReAct 自主探索。chat 直通 render，0 工具调用
- **隐式终止** — 标准 function calling 模式：LLM 输出文本（无 tool_calls）= 结束。无语义扭曲的"提交"工具
- **Bangumi API 工具集** — 条目搜索与详情、角色与声优、每日放送、热门趋势、单集讨论、社区评论、用户画像、本地 RAG 语义搜索（15 个工具）
- **多轮记忆** — L1 短记忆按深度自适应管理上下文窗口 + 工具结果自动压缩；L2 跨会话语义记忆召回（pgvector + 时间衰减）
- **混合 RAG 检索** — 覆盖"类似命运石之门的烧脑番"这类 API 搜不到的模糊查询
- **SSE 流式输出** — `/chat/stream` 端点，按节点推送推理过程
- **开发者可观测性** — `DEV_MODE=true` 时返回 token 统计 + 节点耗时

---

## 路线图

| | 内容 |
|---|------|
| **Current** (v0.2.0-beta) | 4 种人格 × 2 层深度，异质拓扑（Pipeline + ReAct），隐式终止，15 个工具 |
| **Next** (v0.2) | 基础功补强——字数控制、deep 模式可靠性、classifier 精度提升 |
| **Future** | 事件驱动主动推送、逐 token streaming、更多人格探索 |

详细状态和待解决问题见 [`ROADMAP.md`](docs/design/ROADMAP.md)。架构演化历史见 [`architecture-evolution.md`](docs/design/architecture-evolution.md)。

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
  -d '{"message": "你好，最近有什么好看的番？", "depth": "fast", "output_style": "bangumi"}' | python3 -m json.tool
```

---

## 配置

`.env` 中的关键环境变量：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `LLM_MODEL` | `gpt-4o` | 推荐 `deepseek-v4-flash` |
| `LLM_API_KEY` | — | DeepSeek / OpenAI / Azure API key |
| `LLM_BASE_URL` | — | 自定义 endpoint |
| `DATABASE_URL` | `postgresql://myuser:mypassword@localhost:5432/bangumidb` | PostgreSQL 连接 |
| `ZHIPU_API_KEY` | — | 智谱 API key（embedding，RAG + L2 记忆需要） |
| `BANGUMI_ACCESS_TOKEN` | — | Bangumi API token（用户相关工具，可选） |
| `MEMORY_ENABLED` | `True` | L2 跨会话记忆开关 |
| `DEV_MODE` | `False` | 开启后 `/chat` 响应附带 token 统计 + 节点耗时 |

完整配置项见 `.env.example` 和 `core/config.py`。

---

## API

### POST /chat

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `message` | `str` | *必填* | 用户消息 |
| `depth` | `"fast"` \| `"deep"` | `"fast"` | 深度控制 |
| `output_style` | `"bangumi"` \| `"bangumi_cold"` \| `"bangumi_cute"` \| `"neutral"` | `"bangumi"` | 人格模式 |
| `session_id` | `str` | 自动生成 | 多轮会话 ID |
| `user_id` | `str` | `"anonymous"` | 跨会话记忆用户 ID |

| 响应字段 | 类型 | 说明 |
|----------|------|------|
| `reply` | `str` | Agent 回复 |
| `iterations` | `int` | ReAct 循环轮数 |
| `tools_used` | `list[str]` | 调用的工具列表 |
| `query_intent` | `str` | 意图分类 |
| `output_style` | `str` | 实际使用的人格 |
| `depth` | `str` | 实际使用的深度模式 |
| `telemetry` | `dict` | 开发者可观测性数据（仅 `DEV_MODE=true` 时返回） |

### POST /chat/stream

同上参数，返回 SSE（`text/event-stream`），按节点推送 `reasoning` → `tool` → `render` 事件。

---

## 如何扩展

### 添加新的人格

```python
# 1. 在 agent/persona/profiles.py 中定义角色
MY_CHARACTER = CharacterProfile(
    key="my_style",
    snark=0.5, depth_taste=0.6, initiative=0.5,
    identity="你是...",
    expression_guide="语气...",
    # ... 其他字段见 CharacterProfile 定义
)
# 2. 注册到 CHARACTER_REGISTRY
# 3. 在 agent/persona/render.py _VOICE dict 添加对应的 voice hint
# 4. 请求时传 output_style="my_style"
```

### 添加新的工具

```python
# 1. 在 schemas/tools_input.py 定义 Pydantic v2 输入模型
# 2. 在 tools/bgm_tools.py 用 @tool 装饰器注册函数（返回 dict）
# 3. get_agent_tools() 自动发现——无需手动注册
```

详细操作指南见 [`CLAUDE.md`](CLAUDE.md) 调参速查和 [`docs/tool-guide.md`](docs/tool-guide.md)。

---

## 架构

拓扑：异质图——pipeline 路径（fetch/realtime/profile 经专用节点确定性执行）+ ReAct 路径（explore/discuss/fallback 自主探索）。Render 在 main.py 后处理。

两层人格管线：Character Card（System Prompt → 决定思考）+ Render Node（独立 LLM → 决定表达）。

| 层 | 职责 | 核心文件 |
|-----|------|---------|
| **编排层** | Graph 拓扑、路由、意图分类、策略 | `agent/orchestrate/`, `agent/graph.py`, `agent/state.py` |
| **人格层** | CharacterProfile + 5 档离散参数 + Render 风格转换 | `agent/persona/` |
| **记忆层** | L1 滑动窗口 + 压缩 + L2 语义召回 | `agent/memory/` |
| **数据层** | 工具函数 + HTTP 客户端 + RAG + pgvector | `tools/`, `clients/`, `rag/`, `database/` |

上层依赖下层，下层完全不感知上层。详细架构、调参速查、编码规范见 [`CLAUDE.md`](CLAUDE.md)。

---

## 开发

```bash
# 运行全部测试（需要 PostgreSQL + pgvector）
pytest test/ -v

# 仅不需要数据库的测试
pytest test/ --ignore=test/test_rag.py -v

# 仅记忆系统测试
pytest test/test_memory.py test/test_memory_manager.py test/test_phase5_l1.py -v
```

### 项目结构

```
agent/
├── state.py                       # 统一 AgentState（含 depth 字段）
├── graph.py                       # 异质拓扑 StateGraph（v5: Pipeline + ReAct）
├── llm.py                         # LLM 工厂（多 Provider）
├── devtools.py                    # Token 统计 + 节点计时
├── orchestrate/                   # 编排层
│   ├── nodes.py                   #   reasoning_node + 5 pipeline 节点
│   ├── strategies.py              #   浅层 intent 策略
│   ├── deep_strategies.py         #   Deep Scene Hints
│   ├── prompt_builder.py          #   System Prompt 组装
│   ├── classifier.py              #   意图分类
│   ├── guardrails.py              #   终端检测 / XML 泄漏 / 重复调用
│   └── helpers.py                 #   共享辅助函数
├── persona/                       # 人格层
│   ├── profiles.py                #   CharacterProfile + 5 档离散参数
│   └── render.py                  #   Render Node — per-personality voice hints
├── memory/                        # 记忆层
│   ├── short_term.py              #   L1 滑动窗口 + 工具压缩
│   ├── long_term.py               #   L2 语义召回 + 时间衰减
│   └── cache.py                   #   Session 缓存
tools/                              # LangChain @tool 函数
clients/                            # HTTP 客户端（httpx 异步 + 指数退避重试）+ sanitizers
rag/                                # RAG 检索管线
database/                           # SQLModel ORM + pgvector
schemas/                            # Pydantic v2 工具输入 schema
core/                               # pydantic-settings 全局配置
test/                               # 测试（~20 个文件）
scripts/                            # 批量测试脚本
docs/                               # 设计文档、记忆手册、评测体系
```

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [`CLAUDE.md`](CLAUDE.md) | 架构详解、调参速查、编码规范、已知问题 |
| [`docs/design/ROADMAP.md`](docs/design/ROADMAP.md) | 当前状态 & 待解决问题 |
| [`docs/design/architecture-evolution.md`](docs/design/architecture-evolution.md) | 架构演化历史（Phase 1–10） |
| [`docs/design/`](docs/design/) | 设计决策记录 |
| [`docs/eval/`](docs/eval/) | 评测体系设计 |
| [`docs/memory/`](docs/memory/) | 记忆系统手册 |
| [`docs/Rag/`](docs/Rag/) | RAG 策略与表结构 |

---

## License

MIT © [Ujikintoki](https://github.com/Ujikintoki)
