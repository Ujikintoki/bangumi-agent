# 记忆系统手册

> 最后更新: 2026-10-03 | L3 已废弃，单 Agent 架构

## 快速导航

| 文档 | 内容 | 适合 |
|------|------|------|
| [`configuration.md`](configuration.md) | 配置项详解、调优指南 | 调参/优化 |
| [`testing.md`](testing.md) | 测试覆盖、运行方法、扩写指南 | 质量保障 |
| [`debugging.md`](debugging.md) | 日志关键字、常见问题排查 | 排错 |

> ⚠️ `architecture.md` / `implementation.md` 曾列入本表，但从未写出（2026-09-22 核实）。
> 记忆子系统在全局架构中的位置见 [`../architecture-blueprint.md`](../architecture-blueprint.md)。

## 项目中的位置

```
bgm-agent-dev/
├── agent/
│   ├── memory/short_term.py   ← L1 短记忆（滑动窗口 + 两层截断）
│   ├── memory/long_term.py    ← L2 长记忆（召回 + 写入），L3 画像已废弃
│   └── helpers.py             ← L2 记忆召回封装（recall_memory_step，由 nodes/ 调用）
├── database/
│   ├── memory_tables.py       ← 记忆相关表的 ORM 模型
│   ├── engine.py              ← 连接池 + 扩展 + Alembic 迁移（不含 DDL）
│   └── migrations/versions/   ← 表结构与索引的实际创建处
├── core/
│   └── config.py              ← MEMORY_* 配置项定义
├── clients/
│   └── zhipu_client.py        ← embedding 基础设施（共享）
├── main.py                    ← fire-and-forget 写入调度
└── test/
    ├── test_memory.py         ← L1 截断与组合评分测试
    └── test_memory_manager.py ← MemoryManager 召回 / 格式化 / 写入测试
```

## 记忆层级一览

| 层级 | 存储内容 | 生命周期 | 存储介质 | 核心模块 |
|------|---------|---------|---------|---------|
| **L1** | 当前 session 对话历史 | 单 session | 内存 | `agent/memory/short_term.py` |
| **L2** | LLM 摘要 + embedding | 跨 session | PostgreSQL + pgvector | `agent/memory/long_term.py` |

> L3 用户画像已于 2026-07-20 废弃，不再参与召回；`user_profiles` 表保留，运行时读写已移除。

## 核心数据流（一句话版）

```
用户消息 → L1 截断 → L2 语义召回（双通道 + 时间衰减）
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
