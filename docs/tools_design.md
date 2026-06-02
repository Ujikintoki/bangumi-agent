# Agent Tools list
## Tool1: calendar (每日放送)
## 核心意图
[]
## 聚合的 GET API 路由
1. `[GET] /p1/calendar`
## 3. Pydantic 入参建议 (Input)

###
# blog
## 1./p1/blogs/{entryID}
## 2./p1/blogs/{entryID}/comments
## 3./p1/blogs/{entryID}/subjects

# calender
## 1./p1/calendar

# character
## 1./p1/characters/{characterID}
## 2./p1/characters/{characterID}/casts
## 3./p1/characters/{characterID}/comments

# episode
## 1./p1/episodes/{episodeID}
## 2./p1/episodes/{episodeID}/comments

# group

# topic

# person
## 1./p1/persons/{personID}
## 2./p1/persons/{personID}/casts
## 3./p1/persons/{personID}/comments
## 5./p1/persons/{personID}/relations
## 6./p1/persons/{personID}/works

# user
## 1./p1/users/{username}
## 2./p1/users/{username}/blogs
## 3./p1/users/{username}/collections/characters
## 4./p1/users/{username}/collections/persons
## 5./p1/users/{username}/collections/subjects

# subject
## 3./p1/subjects/{subjectID}
## 4./p1/subjects/{subjectID}/characters
## 5./p1/subjects/{subjectID}/comments
## 6./p1/subjects/{subjectID}/episodes

# trending
## 1./p1/trending/subjects
## 2./p1/trending/subjects/topics

## 1. 核心意图
[用一两句话描述这个工具用来回答用户的什么问题。例如：用于全面了解一部作品的社区评价、评分分布和讨论热点。]

## 2. 聚合的 GET API 路由
[列出该工具需要调用的所有 Bangumi API 路由，包含路径参数。]
1. `[GET] /p1/...`
2. `[GET] /p1/...`

## 3. Pydantic 入参建议 (Input)
[大模型调用此工具时需要传入的参数。]
* `[参数名1]` ([类型]): [描述/来源，例如：subject_id (int): 条目纯数字 ID]
* `[参数名2]` ([类型]): [可选，默认值，描述...]

## 4. 核心 JSON 字段提取 (Output 契约)
[这是最重要的部分！剔除废弃字段，只列出你需要保留给大模型的字段及层级结构。]

**API 1: `[路由名称]`**
* `[字段名]` ([类型]): [说明，例如：total (int): 总评论数]
* `[字段名]` ([类型]): [说明，例如：data (list): 评论列表]
  * `[子字段名]` ([类型]): [说明，例如：rate (int): 1-10的评分]
  * `[子字段名]` ([类型]): [说明，例如：comment (str): 评论正文（注意：此处可能需要截断）]

**API 2: `[路由名称]`**
* [依此类推...]

## 5. 业务防御与特殊说明 (可选)
[你注意到的任何坑点或特殊逻辑。例如：]
* 权限问题：如果是 404，可能是 NSFW 条目。
* 数据截断：正文字段如果太长，硬截断到前 500 字。
* 聚合计算：不需要把所有评分给大模型，最好在客户端算一个“平均分”传过去。
