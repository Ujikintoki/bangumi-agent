# 意图分类评测集 v7 重标注报告

> 2026-08-17 | 将 `eval/data/intent_queries.json`（旧 8-intent，91 条）重标注为当前 7-intent
> 产出：`eval/data/intent_queries_v7.json`（91 条）+ `eval/data/intent_adversarial.json`（20 条对抗集）
> 状态：**初稿标注完成，等待 owner 复核后冻结**

## 1. 标注锚点（为什么这么标）

黄金标签编码的是**产品期望路由**，不是代码现状、也不是旧标签平移。依据：

- `agent/nodes/classify.py:29-43` 意图定义（fetch=单确定实体属性、explore=多实体/无确定目标/了解作品内容、discuss=观点驱动、realtime=时效、profile=用户、fallback=无法判断）
- `agent/prompts/scene_hints.py` 场景提示（chat=闲聊/情绪/常识，不查数据）
- classify.py 判定规则：裸短标题→fetch；"最近"看语境；**过去年份→explore 非 realtime**；@用户名→profile；fetch/explore 存疑→explore

## 2. 映射总表

| 旧标签 | 条数 | 新标签 | 说明 |
|--------|------|--------|------|
| chitchat | 12 | → chat (12) | 直接映射 |
| factual | 10 | → chat (8) + fetch (2) | **拆分了**：常识问答（无实体可查）→chat；实体属性（EVA类型/导演对比）→fetch |
| lookup | 15 | → fetch (14) + explore (1) | "具体说说第一部的剧情"→explore（了解作品内容，非单属性） |
| discovery | 12 | → explore (12) | 直接映射 |
| realtime | 10 | → realtime (9) + explore (1) | "2024年最受期待"→explore（过去年份，规则 5 直接命中） |
| debate | 10 | → discuss (10) | 直接映射 |
| emotional | 12 | → chat (12) | 直接映射（情绪→闲聊，不查数据） |
| unknown | 10 | → fallback (6) + chat (4) | "md/在/哦/嗯"→chat（应答词/口头语，倾向闲聊） |

**新标签分布：chat 36 / fetch 16 / explore 14 / discuss 10 / realtime 10 / profile 8 / fallback 8**（91 重标注 + 11 新增 = 102 条；profile 由新增补齐，旧分类法无对应类别）

## 3. 与代码内置迁移表 `_INTENT_ALIASES` 的差异（14 条，请重点复核）

`classify.py:86-94` 有一张运行时向后兼容表（旧 Action 名→v4 intent）。它服务于旧状态归一化，**不是语义定义**。如果黄金标签抄它，eval 就成了自证——所以差异处明确列出：

| 条目 | _INTENT_ALIASES 会给 | 本标注 | 理由 |
|------|---------------------|--------|------|
| 什么是三集定律 / 总集篇 / OVA / 动画制作流程 / 声优选拔 / BD与TV区别 / 第二季原因 / 评分系统运作（8 条 factual） | fetch | **chat** | 常识问答无实体可查；fetch 会空搜索浪费轮次；scene hints 明说 chat=常识 |
| 具体说说第一部的剧情 | fetch | **explore** | 了解作品内容=explore（定义 30 行明确列出） |
| 2024年最受期待的新番 | realtime | **explore** | 规则 5："2024年"已过去→explore，不是 realtime |
| md / 在 / 哦 / 嗯（4 条 unknown） | fallback | **chat** | 应答词/口头语，闲聊属性；fallback 会触发不必要的工具 |

## 4. 需人审清单（语境敏感或边界）

| 条目 | 标注 | 犹豫点 |
|------|------|--------|
| 刚才说的那部具体讲讲 / 你上周推荐的那部好看吗 | fetch / fallback | 依赖会话上下文，单轮 eval 无法体现；迭代版 eval 需带历史 |
| EVA的最新剧场版叫什么 | fetch | 带轻微时效性，也可 argue realtime；判定为实体属性查询 |
| 最近评分上升最快的是哪部 | realtime | "最近"歧义；动态变化属时效，维持 realtime |
| 最近什么都看不进去了 | chat | 情绪为主，隐含"求推荐"暗流；维持情绪优先 |
| 新海诚和宫崎骏的风格有什么区别 | fetch | 也可 argue chat（直接知识回答）；双实体资料查询更稳 |

## 5. 对抗集（`intent_adversarial.json`，20 条）

覆盖：常识误分类 realtime（今天星期几/几号/几点——P0#3 的直接回归用例）、裸短标题（钢炼/巨人/芙莉莲）、@用户名→profile、过去年份→explore、跨领域闲聊→chat、无上下文→fallback、能力外请求→fallback、'这季霸权'时间锚点对照。

## 6. 下一步（复核通过后）

1. **同步 `eval_classifier.py` 关键词基线**：`_KEYWORD_RULES` 仍输出旧标签（chitchat/discovery/debate…），需改为 7-intent 输出，否则 baseline 对比无效
2. `python eval/eval_classifier.py --output results/classifier_report.md` 跑出第一份基线（LLM 分类器 vs 关键词基线）
3. 冻结：`intent_queries_v7.json` + `intent_adversarial.json` 提交进 git，此后修改走版本记录