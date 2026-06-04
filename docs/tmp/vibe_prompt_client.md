# Bangumi Agent — Client 层 Vibe Coding 提示词

> **目标**：在 `/clients/` 下实现三个文件：`base.py`、`sanitizers.py`、`client.py`。
> **契约参考**：`schemas/tools_input.py`（所有 Input Schema）、`docs/tool_docs/*.md`（Output 契约）
> **现有引用**：`core/config.py`（Settings）
> **继承关系**：`BangumiClient(BaseClient)` — BaseClient 提供 HTTP 基础设施，BangumiClient 提供业务方法并调用 sanitizers。

---

## 一、环境与约束

- **Python 3.11+**，所有 I/O 必须 `async/await`
- 所有网络请求使用 `httpx.AsyncClient`
- `BASE_URL = "https://next.bgm.tv/p1"`
- `User-Agent` 必须设置为 `"BangumiAgent/0.1.0 (https://github.com/Ujikintoki/bangumi-agent)"`
- Access Token 从 `core.config.get_settings().BANGUMI_ACCESS_TOKEN` 读取（如果不存在则从构造参数注入）
- 日志记录器：`logging.getLogger("bgm-agent.client.xxx")`
- 所有公开方法返回 `dict`，失败时在 dict 中包含 `"_error": str`

---

## 二、文件一：`clients/base.py`

### 2.1 职责

仅负责 HTTP 底层通信：session 管理、Token 注入、指数退避重试、错误状态码转译。

### 2.2 类定义

```python
class BaseClient:
    def __init__(self, access_token: str | None = None) -> None:
```

构造逻辑：
1. 读取 `core.config.get_settings()` 获取 BANGUMI_ACCESS_TOKEN
2. 构造 headers 字典：User-Agent、Content-Type: application/json，Authorization（如果 token 非空）
3. 创建 `httpx.AsyncClient(base_url=BASE_URL, headers=..., timeout=httpx.Timeout(30.0, connect=10.0))`

### 2.3 核心方法：`_request(method, path, **kwargs) -> dict`

实现带重试的通用请求，规则如下：

```
重试条件: 状态码 429、502、503 或 TimeoutException
最大重试: 3 次
退避策略: sleep(1 * 2^attempt) 递增，429 时优先取 Retry-After 头

非重试错误转译表:
  404 → "未找到资源 (path=...)"
  401 → "认证失败，Access Token 可能已过期"
  403 → "无权限访问该资源"
  500 → "Bangumi 服务器内部错误"
  其他 → "HTTP {status}"
全部放入 {"_error": 消息} 返回
```

### 2.4 辅助方法

```python
async def _get(self, path: str, **kwargs) -> dict     # 委托 _request("GET", ...)
async def _post(self, path: str, **kwargs) -> dict    # 委托 _request("POST", ...)
async def close(self) -> None                          # 关闭 self._client
```

---

## 三、文件二：`clients/sanitizers.py`

### 3.1 职责

纯函数集合。接收原始 API 响应 → 按 Output 契约瘦身 → 返回清洗后的 dict/list。
**绝不引发异常**，字段缺失返回默认值。

### 3.2 内部工具函数

```python
def _cn_name(name: str, name_cn: str | None) -> str
  """优先返回 name_cn，回退 name。"""

def _truncate(text: str, max_len: int = 500) -> str
  """硬截断至 max_len 字符，优先在句号处断开。若句号位置 < max_len/2 则直接在 max_len 处截断。"""

def _is_noise(text: str) -> bool
  """判断是否为无价值内容：len<4 或全由数字/日期/标点组成。"""
```

### 3.3 清洗函数清单

以下每个函数对应一份 `docs/tool_docs/*.md` 的 Output 契约：

| 函数签章 | 对应工具文档 | 核心逻辑 |
|---|---|---|
| `sanitize_search_subjects(raw: dict) -> dict` | `resolve_subject.md` | `results: [{id, name(_cn_name), type(_SUBJECT_TYPES)}]` + total |
| `sanitize_calendar(raw: list[dict], weekday: str, limit: int) -> dict` | `get_calendar.md` | 按 weekday 过滤（today 取当前系统星期几）→ `daily_summary: "今日热门：..."` + `items: [{id, name, score, watchers}]` 按 watchers 降序 |
| `sanitize_trending(raw: dict, subject_type: str) -> dict` | `get_trending.md` | `summary: "当前 {type} 趋势 Top 3: ..."` + `items: [{id, name, score, trending_score}]`，丢弃 images/count/nfsw |
| `sanitize_episode_detail(raw: dict) -> dict` | `get_episode.md` | `ep_sort, ep_name(_cn_name), subject_name, desc(截断500)` |
| `sanitize_episode_comments(raw: list[dict], limit: int) -> dict` | `get_episode.md` | 压扁为 `["({likes}赞) {content} 【回复: ...】"]`，过滤噪音，截断 limit |
| `sanitize_subject_comments(raw: list[dict], limit: int) -> dict` | `get_subject_comments.md` | `comments: ["[{rate}星] {content}"]` + `rating_distribution` 聚合 + 过滤噪音 |
| `sanitize_entity_comments(raw: list[dict], limit: int, entity_type: str) -> dict` | `get_character_comments.md`, `get_person_comments.md` | `comments: ["[{likes}赞] {content}"]` + 首层 `{entity_type}: name` |

**字段类型映射常量**：
```python
_SUBJECT_TYPES = {1: "书籍", 2: "动画", 3: "音乐", 4: "游戏", 6: "三次元"}
_CHARACTER_ROLES = {1: "主角", 2: "配角", 3: "客串"}
_COLLECTION_TYPES = {1: "想看", 2: "看过", 3: "在看", 4: "搁置", 5: "抛弃"}
```

### 3.4 重要边界处理

- **日历**：今日无放送时返回 `{"daily_summary": "今日无番剧放送", "items": []}`
- **热门**：limit 默认 10，但函数内部不设限（由调用方控制）
- **评论噪音过滤**：`_is_noise` 剔除短评；Subject 评论还要按 `rate` 分为 `[x星]` 或 `[未评分]`
- **评论 likes 计算**：取 `reactions[*].users` 总长度

---

## 四、文件三：`clients/client.py`

### 4.1 类定义

```python
class BangumiClient(BaseClient):
```

继承 `BaseClient`，不覆盖 `__init__`。

### 4.2 方法清单

每个方法签名：接受 `tools_input.py` 中对应的 Input schema，返回 `dict`。

**模板**：
```python
async def xxx(self, input: XxxInput) -> dict:
    raw = await self._get(f"/p1/...")
    if "_error" in raw:
        return raw
    return sanitize_xxx(raw, ...)
```

方法 | Input 类型 | API 路由 | 清洗函数
---|---|---|---
`search` | `SearchBangumiInput` | `POST /p1/search/{entity_type}s` | `sanitize_search_subjects` 等
`get_calendar` | `GetCalendarInput` | `GET /p1/calendar` | `sanitize_calendar`
`get_trending` | `GetTrendingInput` | `GET /p1/trending/subjects` | `sanitize_trending`
`get_episode_discussion` | `GetEpisodeDiscussionInput` | `GET /p1/episodes/{id}` + `/comments` | `sanitize_episode_detail` + `sanitize_episode_comments`
`get_subject_discussion` | `GetSubjectDiscussionInput` | `GET /p1/subjects/{id}/comments` | `sanitize_subject_comments`
`get_entity_comments` | `GetEntityCommentsInput` | `GET /p1/{entity_type}s/{id}/comments` | `sanitize_entity_comments`

### 4.3 特殊逻辑

**search() 的分发逻辑**：
```
if input.entity_type == "subject":
    path = "/p1/search/subjects"
    构建 json body: {"keyword": ..., "filter": {}}（含可选的 subject_type）
elif input.entity_type == "character":
    path = "/p1/search/characters"
elif input.entity_type == "person":
    path = "/p1/search/persons"
```

**get_calendar() 的 weekday 处理**：
```
today_map = {"mon": 1, "tue": 2, ..., "sun": 7}
today_num = datetime.now().isoweekday()
如果 weekday == "today", 取 today_num
如果 weekday == "all", 不过滤
否则从 today_map 取对应数字
API 返回按星期几索引的 dict（key 为 1-7），取对应 key 的数组
```

**get_episode_discussion() 的并发请求**：
```
使用 asyncio.gather 同时请求 /episodes/{id} 和 /episodes/{id}/comments
如果评论请求失败，仍返回集信息 + 空评论列表 + "comments_error"
```

---

## 五、依赖声明

在 `requirements.txt` 中确保包含：
```
httpx>=0.27.0
pydantic>=2.0.0
```

---

## 六、验收标准

1. `from clients import BangumiClient` 不报错
2. `BangumiClient` 是 `BaseClient` 的子类，拥有 `_get`、`_post`、`close` 方法
3. 所有 sanitizer 函数可被独立导入 `from clients.sanitizers import sanitize_xxx`
4. 每个 sanitizer 处理空输入时返回空结构（空列表/空字典），不抛异常
5. `_request` 在 429 时至少重试 1 次，最终失败返回 `{"_error": "..."}`
