# 记忆系统手册

> 最后更新: 2026-06-13 | Phase 5 完成

## 快速导航

| 文档 | 内容 | 适合 |
|------|------|------|
| [`architecture.md`](architecture.md) | 三层记忆架构、数据流、模块关系 | 理解全局 |
| [`implementation.md`](implementation.md) | 核心算法、代码路径、关键函数 | 修改代码 |
| [`configuration.md`](configuration.md) | 配置项详解、调优指南 | 调参/优化 |
| [`testing.md`](testing.md) | 测试覆盖、运行方法、扩写指南 | 质量保障 |
| [`debugging.md`](debugging.md) | 日志关键字、常见问题排查 | 排错 |

## 项目中的位置

```
bgm-agent-dev/
├── agent/
│   ├── memory.py              ← L1 短记忆（滑动窗口 + 两层截断）
│   └── memory_manager.py      ← L2/L3 长记忆（召回 + 写入 + 画像）
├── database/
│   ├── memory_tables.py       ← ORM 模型（三张表）
│   └── engine.py              ← 索引创建（HNSW + B-tree）
├── core/
│   └── config.py              ← 10 个 MEMORY_* 配置项
├── clients/
│   └── zhipu_client.py        ← embedding 基础设施（共享）
├── main.py                    ← fire-and-forget 写入调度
├── agent/research/nodes.py    ← L2 记忆召回（首轮注入 System Prompt）
├── agent/dialogue/nodes.py    ← L2 记忆召回（首轮注入 System Prompt）
└── test/
    ├── test_memory.py         ← L1 测试 (21) + 时间衰减 (10)
    └── test_memory_manager.py ← L2/L3 测试 (15)
```

## 三层记忆一览

| 层级 | 存储内容 | 生命周期 | 存储介质 | 核心模块 |
|------|---------|---------|---------|---------|
| **L1** | 当前 session 对话历史 | 单 session | 内存 | `agent/memory.py` |
| **L2** | LLM 摘要 + embedding | 跨 session | PostgreSQL + pgvector | `agent/memory_manager.py` |
| **L3** | 用户偏好画像 | 跨 session | PostgreSQL JSONB | `agent/memory_manager.py` |

## 核心数据流（一句话版）

```
用户消息 → L1 截断 → L2 语义召回（双通道 + 时间衰减）→ L3 画像注入
→ System Prompt → LLM 推理 → 回复用户
                                    ↘ fire-and-forget → LLM 摘要 → embedding → DB 写入
```

## 快速诊断

```bash
# 观察记忆召回日志
grep "\[Memory\]" app.log

# 应该看到：
# [Memory] 召回 342 字 (user=xxx)           ← 成功
# [Memory] recency fallback 补齐 2 条 ...     ← 语义不足，回退补位
# [Memory] remember_session fire-and-forget 异常  ← 写入失败（不影响回复）
```
