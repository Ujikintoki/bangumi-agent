# Tool6: 获取人物详情
## 1. 核心意图
[大模型调用此工具的意图]
获取指定人物的完整档案,这是RAG和api调用的通用工具，当有关某个具体人物的时候调用

## 2. 聚合的 GET API 路由
1. `[GET] /p1/persons/{personID}` （获取人物核心信息）

## 3. Pydantic 入参建议 (Input)
[大模型调用此工具时需要传入的参数。]
* `subject_id` (int): 条目ID
