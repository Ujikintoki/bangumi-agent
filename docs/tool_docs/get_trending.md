# Tool3: 获取热门条目
## 1. 核心意图
[大模型调用此工具的意图]
获取当前的热门条目，目前设置为只支持 "type=1,2",即小说和番剧

## 2. 聚合的 GET API 路由
1. `[GET] /p1/trending/subjects` （获取热门条目）

## 3. Pydantic 入参建议 (Input)
[大模型调用此工具时需要传入的参数。]
* `type` (int): 条目类型，1:书籍 2:番剧
* `limit` (int): 条目数量，默认为10
* `offset` (int): 固定为0

## 4. 核心 JSON 字段提取 (Output 契约)
**API 1: `/p1/trending/subjects`**
*(对返回的 `data` 列表进行遍历，丢弃冗余字段)*
* `id` (int): 条目纯数字 ID。
* `name` (str): 优先取 `subject.nameCN`，为空则取 `subject.name`。
* `score` (float): 提取自 `subject.rating.score`，条目评分。
* `total_votes` (int): 提取自 `subject.rating.total`，条目总评分人数。
* `trending_score` (int): 提取自外层的 `count`（如 2858），这是 Bangumi 衡量近期热度的核心数值，对 LLM 判断“趋势”极具参考价值。

*(注意：必须坚决丢弃 `images` 字典、`info`、`count` 数组、`nsfw`、`locked` 等无用字段！)*

## 5. 业务防御与特殊说明
* **参数映射优化**：大模型对自然语言更敏感。Pydantic 暴露的入参应为 `subject_type: Literal["anime", "book"]`，在 Client 层将其转换为对应的 `type=2` 和 `type=1` 进行 API 请求。
* **数据上限防线 (Hard Limit)**：暴露给大模型的 `limit` 参数必须通过 `Field(le=30)` 设置硬性上限。如果 LLM 贪心想看前 100 名，Pydantic 会自动拦截。
* **聚合摘要 (Aggregation)**：与日历类似，在返回给 LLM 的数据顶部增加一个一句话摘要，例如：`"当前 Anime 趋势 Top 3: [A], [B], [C]"`，快速建立上下文。
* **异常熔断**：捕获所有的 HTTP 状态异常和超时，转译为中文自然语言反馈，绝不允许引发状态机崩溃。
