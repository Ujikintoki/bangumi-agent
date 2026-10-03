# 当前状态与开发顺序

> 核对日期：2026-10-02。运行事实以代码及对应测试为准；历史计划见 Git。
> 本页只记录当前决策和待办，不重复历史 Phase 台账。

## 当前产品阶段

当前代码是具备三个人格、fast/deep、Pipeline + ReAct、L1/L2 和本地 RAG 的 FastAPI Agent 后端，
尚未形成用户可完成真实任务的产品。当前先按 [`PRODUCT.md`](PRODUCT.md) 完成产品定型和仓库治理，
再选择一条纵向场景开发；不因现有能力或历史 Phase 自动推进功能。

## 已完成的本轮工程底线

| 项目 | 实现与验证入口 |
|---|---|
| Pipeline L2 召回进入每步 prompt | `agent/nodes/pipeline.py`；`test/test_p0_contracts.py` |
| 服务端身份绑定，拒绝自报他人 user_id | `core/auth.py`；认证关闭时仅匿名，production 拒绝关闭认证 |
| L1 按用户和 session 隔离 | `main.py` 中组合 key；原始 session ID 仍用于客户端和 L2 |
| JSON / SSE 返回实际 session ID | 成功及执行错误均返回；认证/校验失败不创建会话 |
| 同会话整轮互斥 | `agent/memory/cache.py` claim；HTTP 409；普通/流式、取消/异常均释放 |
| 模型异常正文不传给用户 | reasoning / pipeline 固定降级话术，不记录供应商异常正文 |

上述能力只保证单应用进程。匿名 session ID 是续聊凭据，应保密；
已有实名调用方须配置个人 Bearer 凭据，不能继续靠任意 user_id 访问 L2。
旧的内存 session 缓存在重启后清空；本轮不迁移数据库或历史记忆。

## 当前只推进

1. **固定产品基线**：用一页文档明确用户、核心任务、第一版形态、非目标与价值信号。
2. **机械盘点仓库**：不逐篇精读，输出活文档、历史台账、重复说明、历史注释和疑似死代码候选。
3. **收敛权威入口**：以 AGENTS 为唯一开发指令，精简 README / ROADMAP 职责，历史愿景保留为北极星而非任务表。
4. **定义一个纵向场景**：为“条目 + 用户提供的内容 → 理解 → 讨论 → 可编辑草稿”写输入、输出、来源与验收卡片。
5. **映射已有资产**：只对该场景标记直接复用、需要改造、冻结和候选删除；之后再制定实现计划。

本轮不开始 Web、UserScript、页面角色、长期记忆扩张或新的 Agent 编排。工程上线、隔离集成验收、
请求可观测性和多进程状态仍然必要，但在纵向场景确定后按它的交付需要排序。

## 待验证，不能当成已修或已坏

- deep 零工具调用、常识误分类、8 轮以上记忆保持率：旧观察缺当前复测。
- chat 路径目前只把当前问题交给 Render，历史对话/L2 如何进入需单独设计和测量。
- 多轮 Render 取最后 AI 消息、替换历史回复的边界需补本轮/历史分隔测试。
- 工具输出整体预算：本地搜索已有限额，其余工具要逐项测量。
- 真实模型质量、数据库状态、sanitize 浏览器模拟测试之外的 E2E 均未在本轮验收。

## 已过时的旧待办

硬字数截断、基础 IP 限流、子图历史重复、低置信度父图路由错误已有代码修复；
2048d 向量问题随 1024d embedding 迁移解决；生产 RAG 已用 RagEntity。
cold/cute 四人格、旧 orchestrate 路径和旧测试数量不再作为当前依据。

## 验证入口

`scripts/test_offline.py` 运行审阅过的单元测试集合，禁网络、禁 PostgreSQL，
不读取开发者 .env；需预先准备 tiktoken 缓存。它不是全量集成或产品质量评测。
不要再把“忽略 test_rag.py”当作无数据库测试命令。

[产品基线](PRODUCT.md) · [使用说明](README.md) · [开发规则](AGENTS.md) · [文档地图](docs/README.md) ·
[架构蓝图](docs/architecture-blueprint.md) · [产品愿景](docs/design/claude-on-bangumi-vision.md)
