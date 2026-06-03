# Tool1: 获取每日放送番剧
## 1. 核心意图
[大模型调用此工具的意图]
获得当日播放的番剧名称

## 2. 聚合的 GET API 路由
1. `[GET] /p1/calendar`

## 3. Pydantic 入参建议 (Input)
[大模型调用此工具时需要传入的参数。]
无

## 4. 核心 JSON 字段提取 (Output 契约)
**API 1: `/p1/calendar`**
* `id` (int): 条目纯数字 ID（用于后续调用其他 Tool）。
* `name` (str): 优先使用 `nameCN`，如果为空则回退到 `name`。
* `score` (float): 提取自 `rating.score`，当前评分。
* `total` (int): 提取自 `rating.total`，评分总人数。
* `watchers` (int): 在看人数（反映当前社区热度的核心指标）。

## 5. 业务防御与特殊说明
* **数据截断 (Data Truncation)**：
  虽然单日列表没有长文本，但条目数量可能极多。必须按 `watchers`（在看人数）降序排列。如果 LLM 没有明确要求看“全部”，则默认截断到单日 Top 10。
* **聚合计算 (Aggregation)**：
  为了降低大模型对长列表的推理负担，在返回的 JSON 结构顶部，新增一个 `daily_summary` 字段，直接由 Python 提取出当日 `watchers` 最高的 Top 3 番剧名称（如：“今日热门：[番剧A], [番剧B], [番剧C]”）。让 LLM 一眼抓住重点。
* **异常熔断**：
  如果 API 返回 502 或超时，捕获异常并返回诸如“Bangumi番组表接口响应超时”的自然语言，绝对禁止抛出 `Exception` 导致 LangGraph 状态机崩溃。
