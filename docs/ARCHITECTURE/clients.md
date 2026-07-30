# 客户端层 (clients/)

> 数据层子系统 · ~3013 行源码 | 状态：✅ 稳定

---

## 一、架构概览

```
                    ┌──────────────────────────┐
                    │  tools/bgm_tools.py      │  ← 调用方
                    └──────────┬───────────────┘
                               │ 导入 BangumiClient
                               ▼
┌──────────────────────────────────────────────────┐
│              clients/client.py                    │
│              BangumiClient (BaseClient)           │
│                                                   │
│  search()  get_calendar()  get_subject_detail()  │  ← 业务方法
│  get_trending_subjects()  get_blog()  ...        │
│                                                   │
│  每个方法: 构造请求 → 调用 _get/_post →          │
│            sanitizers.xxx() 清洗 → 返回 dict      │
└──────────────────────┬───────────────────────────┘
                       │ 继承
┌──────────────────────┴───────────────────────────┐
│              clients/base.py                      │
│              BaseClient                           │
│                                                   │
│  _request()  _get()  _post()  _handle_http_error()│  ← HTTP 基础设施
│  重试 429/502/503/TimeoutException (指数退避)     │
│  Bearer Token 注入  User-Agent 设置              │
└──────────────────────┬───────────────────────────┘
                       │ 使用
┌──────────────────────┴───────────────────────────┐
│         httpx.AsyncClient                        │
│         base_url=https://next.bgm.tv             │
│         timeout=30s (connect=10s)                │
└──────────────────────────────────────────────────┘
```

独立于客户端层的纯函数模块：

```
clients/sanitizers.py   ← 纯函数集合，不依赖 self，可独立测试
clients/zhipu_client.py ← 智谱 AI embedding 基础设施，供 RAG 和 memory 共用
```

---

## 二、BaseClient — HTTP 基础设施

**文件**：`clients/base.py`（150 行）

### 2.1 初始化

```python
class BaseClient:
    def __init__(self, access_token: str | None = None) -> None:
        settings = get_settings()
        headers = {
            "User-Agent": "BangumiAgent/0.1.0 (...)",
            "Content-Type": "application/json",
        }
        token = access_token or settings.BANGUMI_ACCESS_TOKEN
        if token:
            headers["Authorization"] = f"Bearer {token}"

        self._client = httpx.AsyncClient(
            base_url="https://next.bgm.tv",
            headers=headers,
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
```

**认证策略**：
- 显式传 `access_token` 优先，若为 None 则从 `BANGUMI_ACCESS_TOKEN` 环境变量读取
- 两者均为空时 → 无 Authorization 头 → "匿名"模式（搜索/日历/热门等公开端点仍可用）
- Token 绝不暴露给 LLM Schema

### 2.2 重试策略

`_request()` 方法是所有 HTTP 通信的唯一入口。重试逻辑：

| 条件 | 重试次数 | 退避公式 | 备注 |
|------|---------|---------|------|
| **429** (限流) | 最多 3 次 | `Retry-After × 2^attempt` | 优先读 Retry-After 响应头，回退 5s |
| **502/503** (服务端错误) | 前 2 次 | `2 × 2^attempt` | 第 3 次不重试，返回错误字典 |
| **TimeoutException** | 前 2 次 | `1 × 2^attempt` | 第 3 次返回 `{"_error": "请求超时"}` |
| **其他 4xx/5xx** | 不重试 | — | 立即返回错误字典 |

```python
# 伪代码：重试核心逻辑
for attempt in range(3):
    try:
        response = await self._client.request(method, path, **kwargs)
        if response.status_code == 429:
            await asyncio.sleep(retry_after * 2**attempt)
            continue
        if response.status_code in (502, 503) and attempt < 2:
            await asyncio.sleep(2 * 2**attempt)
            continue
        response.raise_for_status()
        return response.json()
    except httpx.TimeoutException:
        if attempt < 2:
            await asyncio.sleep(1 * 2**attempt)
            continue
        return {"_error": "请求超时"}
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (502, 503) and attempt < 2:
            await asyncio.sleep(2 * 2**attempt)
            continue
        return self._handle_http_error(path, exc.response.status_code)

return {"_error": "请求失败"}  # 3 次重试耗尽
```

### 2.3 错误字典映射

```python
@staticmethod
def _handle_http_error(path: str, status: int) -> dict:
    errors = {
        404: "未找到资源",
        401: "认证失败，Access Token 可能已过期",
        403: "无权限访问该资源",
        500: "Bangumi 服务器内部错误",
    }
    return {"_error": errors.get(status, f"HTTP {status}")}
```

### 2.4 连接管理

```python
async def close(self) -> None:
    await self._client.aclose()

async def __aenter__(self) -> "BaseClient":
    return self

async def __aexit__(self, *args) -> None:
    await self.close()
```

支持 `async with BangumiClient() as client:` 上下文管理器模式。

---

## 三、BangumiClient — 业务外观

**文件**：`clients/client.py`（591 行）

### 3.1 12 个公共 API 方法

| 方法 | HTTP | 端点 | 并发 | 清洗器 |
|------|------|------|------|--------|
| `search(input)` | POST | `/p1/search/{entity}s` | — | `sanitize_search_subjects/characters/persons` |
| `get_calendar(input)` | GET | `/p1/calendar` | — | `sanitize_calendar` |
| `get_trending_subjects(input)` | GET | `/p1/trending/subjects` | — | `sanitize_trending` |
| `get_hot_topics(input)` | GET | `/p1/trending/subjects/topics` | — | `sanitize_trending_topics` |
| `get_episode_discussion(input)` | GET ×2 | episodes + comments | ✅ | `sanitize_episode_detail` + `sanitize_episode_comments` |
| `get_subject_opinions(input)` | GET ×2 | comments + reviews | ✅ | `sanitize_subject_comments` + `sanitize_reviews` |
| `get_subject_episodes(input)` | GET | episodes | — | `sanitize_subject_episodes` |
| `get_entity_comments(input)` | GET ×2 | entity + comments | ✅ | `sanitize_entity_comments` |
| `get_user_profile(input)` | GET ×3-5 | user + collections + (...) | ✅ | 内联清洗 |
| `get_blog(input)` | GET ×1-3 | blog + comments + subjects | ✅ | 内联清洗 |
| `get_subject_detail(subject_id)` | GET | `/p1/subjects/{id}` | — | `sanitize_subject_detail` |
| `get_character_detail(character_id)` | GET | `/p1/characters/{id}` | — | `sanitize_character_detail` |
| `get_person_detail(person_id)` | GET | `/p1/persons/{id}` | — | `sanitize_person_detail` |
| `get_user_timeline(username, limit)` | GET | `/p1/users/{name}/timeline` | — | `sanitize_timeline_events` |
| `get_subject_characters(subject_id)` | GET | `/p1/subjects/{id}/characters` | — | `sanitize_subject_characters` |

**统一模板**：
```python
async def get_subject_detail(self, subject_id: int) -> dict:
    raw = await self._get(f"/p1/subjects/{subject_id}")
    if "_error" in raw:
        return raw
    return sanitizers.sanitize_subject_detail(raw)
```

### 3.2 并发模式

5 个方法使用 `asyncio.create_task` 并发调用多个端点：

```python
# 以 get_subject_opinions 为例
comments_task = asyncio.create_task(self._get(f"/p1/subjects/{sid}/comments?limit={limit}"))
reviews_task = asyncio.create_task(self._get(f"/p1/subjects/{sid}/reviews?limit={limit}"))

# 分别 await，一个失败不影响另一个
try:
    raw = await comments_task
except Exception:
    raw = {"_error": "获取评论失败"}

if "_error" in raw:
    result["comments_error"] = raw["_error"]  # 降级：该维度标记错误
else:
    result["comments"] = sanitizers.sanitize_subject_comments(...)
```

**关键设计**：不使用 `asyncio.gather()`（一个失败会取消其他），而是逐个 `await`，实现分支级优雅降级。

### 3.3 输入模式不一致（已知问题）

大多数方法接受 Pydantic Schema，但有 4 个接受裸参数：
- `get_subject_detail(subject_id: int)` — 无 Schema 包装
- `get_character_detail(character_id: int)` — 无 Schema 包装
- `get_person_detail(person_id: int)` — 无 Schema 包装
- `get_user_timeline(username: str, limit: int = 20)` — 无 Schema 包装

这不影响功能，但违反了"所有客户端方法接受 Schema"的约定。

---

## 四、Sanitizers — 数据清洗

**文件**：`clients/sanitizers.py`（1120 行）

### 4.1 设计原则

- **纯函数**：无 `self`，不修改输入，不读写外部状态
- **白名单优先**：显式声明"要什么字段"，而非"丢什么字段"
- **兜底值**：任意字段缺失返回默认值，绝不崩溃
- **硬截断**：summary 500 字、评论 200 字、日志正文 300 字

### 4.2 公共函数全景（~28 个）

**内部辅助**：
| 函数 | 职责 |
|------|------|
| `_cn_name(name, name_cn)` | 优先中文名 |
| `_truncate(text, max_len)` | 句号处硬截断 |
| `_is_noise(text)` | 判别无价值短评（<2 字符、纯日期、纯重复字符） |
| `_strip_bbcode(text)` | BBCode → 自然语言标注 |

**主体清洗**：
| 函数 | 输入 | 输出 | 关键逻辑 |
|------|------|------|---------|
| `sanitize_search_subjects(raw)` | API 搜索结果 | `{results: [...], total: N}` | 丢弃 images(~88t), rating.count[10], metaTags, locked |
| `sanitize_subject_detail(raw)` | 条目详情 | `dict` | 评分+排名+收藏分布+标签(前10)+infobox+summary(300 字) |
| `sanitize_calendar(raw)` | 日历 | `{daily_summary, items, total}` | 按 watchers 降序 |
| `sanitize_trending(raw, type)` | 热门条目 | `{summary, items, total}` | 类型标签+摘要 |
| `sanitize_trending_topics(raw)` | 热门讨论 | `{items, total}` | 帖子标题+回复数+关联条目 |
| `sanitize_comments(raw, limit)` | 通用评论 | `list[str]` | 去重+BBCode 清洗+200 字截断+时间倒序 |
| `sanitize_subject_comments(raw, limit)` | 条目评论 | `{comments, rating_distribution}` | 评分聚合 (1-3/4-6/7-8/9-10) |
| `sanitize_episode_detail(raw)` | 剧集 | `dict` | 元数据+所属条目引用 |
| `sanitize_episode_comments(raw, limit)` | 剧集评论 | `{comments, comment_count}` | |
| `sanitize_reviews(raw)` | 长评 | `{items, total}` | 摘要 200 字 |
| `sanitize_subject_episodes(raw)` | 剧集列表 | `{subject_id, items, total}` | 仅主线(type=0)，按集数升序 |
| `sanitize_entity_comments(...)` | 角色/人物评论 | `dict` | 含实体名称归属 |
| `sanitize_subject_characters(raw, id)` | 条目角色 | `{subject_id, characters}` | 角色类型+声优(casts) |
| `sanitize_character_detail(raw)` | 角色详情 | `dict` | summary 200 字+infobox |
| `sanitize_person_detail(raw)` | 人物详情 | `dict` | 同角色模板 |
| `sanitize_user_collections(raw, limit)` | 用户收藏 | `{items, score_stats}` | 15 条上限 |
| `sanitize_user_stats(raw)` | 用户统计 | `dict` | 整数代码→可读标签 |
| `sanitize_timeline_events(raw, limit)` | 时光机 | `{events, total}` | 过滤每日签到(type=2) |

### 4.3 A/B/C/D 字段方法论

每个 API 返回的字段按价值分四类：

| 类别 | 含义 | 决策 | 示例 |
|------|------|------|------|
| **A** | 不可替代的核心字段 | 保留 | `id`, `name`, `name_cn`, `score` |
| **B** | 有上下文价值的结构字段 | 转换 | `type: 2 → "动画"` |
| **C** | 噪音或无价值 | 丢弃 | `images`（~88 tokens/item）、`metaTags` |
| **D** | 需截断的大文本 | 截断 | `summary` 500 字、评论 200 字 |

---

## 五、Zhipu 客户端 — Embedding 基础设施

**文件**：`clients/zhipu_client.py`（152 行）

### 5.1 架构

```python
# 初始化（返回 (client, None) 或 (None, error_msg)）
def init_zhipu_client(api_key="", base_url="https://open.bigmodel.cn/api/paas/v4")

# 进程级单例
def get_zhipu_client() -> Optional[object]

# Embedding 封装
async def embed_single(text, client=None, model="embedding-3") -> Optional[list[float]]
async def embed_batch(texts, client=None, model="embedding-3") -> Optional[list[list[float]]]
```

### 5.2 降级策略

- `zai-sdk` 未安装 → `init_zhipu_client()` 返回 `(None, "zai-sdk 未安装")`
- API Key 无效 → 返回 `(None, error_msg)`
- API 调用失败 → `embed_single/embed_batch` 返回 `None`

所有失败不抛异常，调用方（RAG、MemoryManager）自行处理 None。

### 5.3 消费者

```
clients/zhipu_client.py
  ├── rag/retriever.py       → RagEntityRetriever 初始化时获取 client
  ├── rag/ingestion.py       → RagEntityIngestor 初始化时获取 client
  ├── agent/memory/long_term.py → embed_single() (L2 记忆写入)
  └── rag/utils.py           → 向后兼容 re-export 桩
```

---

## 六、硬编码值与配置建议

| 位置 | 当前值 | 建议 |
|------|--------|------|
| `base.py:20` | `P1_BASE_URL = "https://next.bgm.tv"` | 移入 `config.py` → `BANGUMI_API_BASE_URL` |
| `base.py:24` | `USER_AGENT = "BangumiAgent/0.1.0"` | 版本号用 `settings.VERSION` |
| `base.py:51` | `timeout=30.0, connect=10.0` | 移入 `config.py` |
| `base.py:61` | `max_retries = 3` | 移入 `config.py` |
| `base.py:71` | `Retry-After` 回退 `5` 秒 | 移入 `config.py` |
| `sanitizers.py` | 多处理截断 (100/150/200/300/500) | 集中到 `_TRUNCATION_LIMITS` dict |

---

## 七、已知问题

| # | 问题 | 文件 | 严重度 |
|---|------|------|--------|
| 1 | `_ROLE_MAP`（第 50 行）和 `_TYPE_ICONS`（第 57 行）在 `bgm_tools.py` 中定义但从未使用 | `tools/bgm_tools.py` | 🟢 死代码 |
| 2 | 4 个 BangumiClient 方法接受裸参数而非 Pydantic Schema | `clients/client.py` | 🟢 不一致 |
| 3 | `_request()` 返回类型标注 `dict[str, Any]` 但 API 可能返回 `list` | `clients/base.py` | 🟢 类型不精确 |
| 4 | 502/503 在 `try` 块和 `except HTTPStatusError` 中重复处理 | `clients/base.py` | 🟢 代码重复 |
| 5 | 无客户端速率限制（依赖 API 返回 429 后被动重试） | `clients/base.py` | 🟡 需关注 |
