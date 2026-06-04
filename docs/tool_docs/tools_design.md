# Agent Tools 设计决策 & API 路由评估

> 版本 0.3.0 | 2026-06-03

---

## 设计原则：API 独有 ≠ LLM 共识

| 信息分类 | 数据来源 | 示例 | 是否需要 Tool |
|---|---|---|---|
| **Bangumi 社区独有内容** | API | 吐槽箱、评分分布、热门趋势、用户动态 | ✅ **必须** |
| **实时/时效性数据** | API | 每日放送、最新一集、当前热榜 | ✅ **必须** |
| **公共知识/百科事实** | LLM | 声优姓名、剧情简介、播出年份 | ❌ 不需要 |
| **ID 映射/精确路由** | RAG + API | 模糊名称 → 精确 ID | ✅ **必须**（RAG） |

核心判断逻辑：**如果 LLM 的训练数据中已经有这个信息，就不要为此设计 Tool。Tool 只提供 LLM"不可能知道"的 Bangumi 专属内容。**

---

## API 端点评估与 Tool 建议

### calendar（放送日历）
| 端点 | 评估 | 理由 |
|---|---|---|
| `GET /p1/calendar` | ✅ **Tool1: get_calendar** | 实时播出信息，LLM 无法预测 |

### episode（单集）
| 端点 | 评估 | 理由 |
|---|---|---|
| `GET /p1/episodes/{id}` | ✅ **Tool2: get_episode** | 单集元数据作为上下文锚点 |
| `GET /p1/episodes/{id}/comments` | ✅ **合并入 Tool2** | 单集吐槽箱——LLM 不知道"大家怎么评价这一集" |

### trending（热门趋势）
| 端点 | 评估 | 理由 |
|---|---|---|
| `GET /p1/trending/subjects` | ✅ **Tool3: get_trending** | 实时社区热度，LLM 无法计算 |
| `GET /p1/trending/subjects/topics` | ❌ 暂不实现 | 与 subjects 高度重叠 |

### subject（条目）
| 端点 | 评估 | 理由 |
|---|---|---|
| `GET /p1/subjects/{id}` | ❌ **不做独立 Tool** | name/summary/eps 等客观信息属于公共知识 |
| `GET /p1/subjects/{id}/comments` | ✅ **Tool5: get_subject_comments** | 社区评分和短评——这是 LLM 不知道的 |
| `GET /p1/subjects/{id}/characters` | ❌ 不做 | 角色-作品关联属于公共知识，可被 RAG 覆盖 |
| `GET /p1/subjects/{id}/episodes` | ❌ 不做 | 可通过 RAG meta_info.eps + 推算 episode ID 解决 |
| `POST /p1/search/subjects` | ✅ **Tool4: resolve_subject** | ID ↔ 名称双向解析，串联所有 Tool 的枢纽 |

### character（角色）
| 端点 | 评估 | 理由 |
|---|---|---|
| `GET /p1/characters/{id}` | ❌ **不做独立 Tool** | name/role/summary 属于公共知识 |
| `GET /p1/characters/{id}/casts` | ❌ 不做 | 角色-作品关联属于公共知识 |
| `GET /p1/characters/{id}/comments` | ✅ **Tool6: get_character_comments** | 社区对角色的主观评价 |

### person（人物）
| 端点 | 评估 | 理由 |
|---|---|---|
| `GET /p1/persons/{id}` | ❌ **不做独立 Tool** | name/career/summary 属于公共知识 |
| `GET /p1/persons/{id}/casts` | ❌ 不做 | 人物-作品关联属于公共知识 |
| `GET /p1/persons/{id}/works` | ❌ 不做 | 同上 |
| `GET /p1/persons/{id}/relations` | ❌ 不做 | 关系图属于公共知识 |
| `GET /p1/persons/{id}/comments` | ✅ **Tool7: get_person_comments** | 社区对现实人物的主观评价 |

### user（用户）
| 端点 | 评估 | 理由 |
|---|---|---|
| `GET /p1/users/{username}` | ❌ 不做 | 用户简介属于基本信息 |
| `GET /p1/users/{username}/blogs` | ✅ **Tool8: get_user_blogs** | 用户日志是纯粹 UGC |
| `GET /p1/users/{username}/timeline` | ✅ **Tool9: get_user_timeline**（已有） | 用户收藏/评分——Bangumi 独有行为数据 |
| `GET /p1/users/{username}/collections/*` | ❌ 暂不实现 | 可通过 timeline 覆盖 |

### blog（日志）
| 端点 | 评估 | 理由 |
|---|---|---|
| `GET /p1/blogs/{entryID}` | ✅ **合并入 Tool8** | 日志正文是纯 UGC |
| `GET /p1/blogs/{entryID}/comments` | ✅ **合并入 Tool8** | 日志评论是纯 UGC |
| `GET /p1/blogs/{entryID}/subjects` | ❌ 不做 | 关联条目可由 RAG 覆盖 |

---

## Tool 最终清单（9 个）

| # | Tool 名称 | 核心价值 | 实现状态 |
|---|---|---|---|
| 1 | `get_calendar` | 今日播出的番剧（LLM 不知道） | 📄 ✅ 💻 ✅ |
| 2 | `get_episode` | 单集信息 + 吐槽箱 | 📄 ✅ 💻 ✅ |
| 3 | `get_trending` | 全站热门趋势（LLM 不知道） | 📄 ✅ 💻 ✅ |
| 4 | `resolve_subject` | ID ↔ 名称解析（串联枢纽） | 📄 ✅ 💻 ⏳ |
| 5 | `get_subject_comments` | 条目社区评价 | 📄 ✅ 💻 ⏳ |
| 6 | `get_character_comments` | 角色社区评价 | 📄 ✅ 💻 ⏳ |
| 7 | `get_person_comments` | 人物社区评价 | 📄 ✅ 💻 ⏳ |
| 8 | `get_user_blogs` | 用户日志 + 日志评论 | 📄 ⏳ 💻 ⏳ |
| 9 | `get_user_timeline` | 用户收藏/评分动态 | 💻 ✅ |

---

## RAG 与 API Tools 的协作关系

```
用户模糊查询
     │
     ▼
RAG 语义路由器 ──→ 精确 ID ──→ API Tools ──→ 社区独有内容
     │                │
     │                └── Subject ID / Character ID / Person ID
     │
     └── "类似《命运石之门》的烧脑番" → 向量相似度直接回答（无需 API）
```

**结论：RAG 解决"什么是什么"，API Tools 解决"大家怎么说"。两者互补，不可替代。**
