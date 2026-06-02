# Tool2: 获取条目的单个章节
## 1. 核心意图
[大模型调用此工具的意图]
获取条目单个章节的信息，主要是分析吐槽箱内容

## 2. 聚合的 GET API 路由
1. `[GET] /p1/episodes/{episodeID}` （获取章节信息）
2. `[GET] /p1/episodes/{episodeID}/comments` (获取条目的章节吐槽箱)

## 3. Pydantic 入参建议 (Input)
[大模型调用此工具时需要传入的参数。]
* `episodeID` (int): 章节ID

## 4. 核心 JSON 字段提取 (Output 契约)
**API 1: `/p1/episodes/{episodeID}`**
* `ep_sort` (int): 集数。
* `ep_name` (str): 单集名称（可能为空）。
* `subject_name` (str): 提取自嵌套的 subject。
* `desc` (str): 剧情简介与 STAFF（必须硬截断至前 500 字符）。

**API 2: `/p1/episodes/{episodeID}/comments`**
*(极致瘦身方案：抛弃所有 user 字段，将嵌套压平为单行字符串)*
* `comments` (list[str]): 纯文本吐槽列表。
  * 格式定义：`"{content} 【回复: {reply_1_content} | {reply_2_content}】"`
  * 例如：`"啥时候给川面真也来一集吧 【回复: 川面和山组有什么交集吗】"`

## 5. 业务防御与特殊说明
* **双重请求与并发**：这个 Tool 需要调用两个接口，在 Client 层应该使用 `asyncio.gather` 并发请求，以缩短 TTFT（首字响应时间）。
* **数据截断 (Data Truncation)**：
  * 单集的 `desc` 可能因为过长的演职员表浪费 Token，截断前 500 字。
  * `comments` 列表可能非常长，通过 Pydantic 暴露一个 `limit` 参数（默认 20，最大 50），只取前 N 条。
* **异常熔断**：404 错误通常意味着该章节不存在，或者所属条目是 NSFW 隐藏条目。需要捕获 404 并转译为自然语言提示。
