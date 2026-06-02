# Tool4: 获取条目详情
## 1. 核心意图
[大模型调用此工具的意图]
获取指定条目的完整档案,这是RAG和api调用的通用工具，当有关某个条目的时候调用

## 2. 聚合的 GET API 路由
1. `[GET] /p1/subjects/{subjectID}` （获取条目核心信息）

## 3. Pydantic 入参建议 (Input)
[大模型调用此工具时需要传入的参数。]
* `subject_id` (int): 条目ID

## 4. 核心 JSON 字段提取 (Output 契约)
**API 1: `/p1/subjects/{subjectID}`**
[保留RagEntity的相关栏目和meta_info信息]
* `id` (int): 条目纯数字 ID。
* `name` (str): 取 `name`。
* `name_cn` (str): 取`name_cn`，条目的中文名
* `summary` (str): 剧情简介，后续在Rag中需要拼接并Embedding。
**以下是meta_info的信息**
  * `type` (str): 条目类型。**在 Client 层将数字转为自然语言**：
  * `1` → `"书籍"`, `2` → `"动画"`, `3` → `"音乐"`, `4` → `"游戏"`, `6` → `"三次元"`
  * `score` (float): 提取自 `rating.score`，当前评分。
  * `rank` (int): 提取自 `rating.rank`，全站排名（越小越靠前）。
  * `total_votes` (int): 提取自 `rating.total`，总评分人数（核心热度指标）。
  * `air_date` (str): 提取自 `airtime.date`，格式 `YYYY-MM-DD`。为空时返回 `null`。
  * `eps` (int): 总集数。
  * `nsfw` (bool): R18 标记。**仅在值为 `true` 时出现此字段**（节省 Token），用于后续安全护栏判断。
  * `tags` (list[dict]): **按 `count` 降序排列后取前 10 个**。每个元素的格式为 `{"name": str, "count": int}`。
  * 丢弃原始 API 返回的无序全量标签列表，避免上下文污染。

## 5. 业务防御与特殊说明
