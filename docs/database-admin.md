# 数据库管理手册

> 面向管理员/开发者 — PostgreSQL + pgvector 日常运维。
> 最后更新: 2026-06-15

## 速查

```bash
# PostgreSQL 容器状态
docker ps --filter name=bangumi-pg

# 进入 SQL shell
docker exec -it bangumi-pg psql -U myuser -d bangumidb

# 容器生命周期
docker start bangumi-pg    # 启动（已存在的容器）
docker stop bangumi-pg     # 停止
docker restart bangumi-pg  # 重启
```

---

## 连接信息

| 参数 | 值 |
|------|-----|
| Host | `localhost` |
| Port | `5432` |
| Database | `bangumidb` |
| User | `myuser` |
| Password | `mypassword` |

GUI 客户端（可选）：

```bash
# TablePlus (Mac, 免费版够用)
brew install --cask tableplus
# 打开后填上述连接信息即可

# 或者 VS Code 插件 "Database Client" (by cweijan)
# 无需离开编辑器
```

---

## 表结构

```
bangumidb
├── rag_entities             ← RAG 语义搜索（Subject/Character/Person）
├── session_memories         ← L2 会话摘要 + embedding
├── user_profiles            ← L3 用户画像
└── public_memories          ← Phase 6 预留（全局共识记忆）
```

---

## 日常巡检

### 看看有多少数据

```sql
SELECT
  'session_memories' as table_name,
  COUNT(*) as rows,
  COUNT(DISTINCT user_id) as users,
  COUNT(DISTINCT session_id) as sessions,
  pg_size_pretty(pg_total_relation_size('session_memories')) as total_size
FROM session_memories
UNION ALL
SELECT
  'user_profiles',
  COUNT(*),
  COUNT(DISTINCT user_id),
  NULL,
  pg_size_pretty(pg_total_relation_size('user_profiles'))
FROM user_profiles
UNION ALL
SELECT
  'rag_entities',
  COUNT(*),
  NULL,
  NULL,
  pg_size_pretty(pg_total_relation_size('rag_entities'))
FROM rag_entities;
```

### 最近活跃的用户

```sql
SELECT
  user_id,
  COUNT(*) as sessions,
  MAX(created_at) as last_active
FROM session_memories
GROUP BY user_id
ORDER BY last_active DESC
LIMIT 20;
```

### 数据库总大小

```sql
SELECT pg_size_pretty(pg_database_size('bangumidb')) as db_size;
```

### 索引健康检查

```sql
-- 查看所有索引及其大小
SELECT
  tablename,
  indexname,
  pg_size_pretty(pg_relation_size(indexname::regclass)) as size
FROM pg_indexes
WHERE schemaname = 'public'
ORDER BY pg_relation_size(indexname::regclass) DESC;
```

---

## 数据清理

### 删测试用户（推荐日常用这个）

```sql
-- 删除前缀匹配的所有测试数据
DELETE FROM session_memories WHERE user_id LIKE 'test_user_%';
DELETE FROM user_profiles WHERE user_id LIKE 'test_user_%';

-- 检查是否清干净
SELECT user_id, COUNT(*) FROM session_memories
WHERE user_id LIKE 'test_user_%' GROUP BY user_id;
```

### 删特定用户

```sql
-- 用 curl 中的 user_id 值替换
DELETE FROM session_memories WHERE user_id = 'test_user_m1';
DELETE FROM user_profiles WHERE user_id = 'test_user_m1';
```

### 删太久远的数据

```sql
-- 30 天前的会话摘要
DELETE FROM session_memories
WHERE created_at < NOW() - INTERVAL '30 days';

-- 90 天未活跃的用户画像
DELETE FROM user_profiles
WHERE last_active_at < NOW() - INTERVAL '90 days';
```

### 全量清空（慎用）

```sql
-- 只清 L2/L3，不动 RAG
DELETE FROM session_memories;
DELETE FROM user_profiles;

-- 真空回收空间（DELETE 不会自动释放磁盘）
VACUUM FULL session_memories;
VACUUM FULL user_profiles;
```

### 检查 UPSERT 重复（Bug 1 回归检测）

```sql
-- 正常应为空（每 (user_id, session_id) 只有一行）
SELECT user_id, session_id, COUNT(*)
FROM session_memories
GROUP BY user_id, session_id
HAVING COUNT(*) > 1;
```

---

## 数据查看

### Session 摘要

```sql
-- 某用户的全部记忆
SELECT
  session_id,
  summary_text,
  key_entities,
  tools_used,
  created_at
FROM session_memories
WHERE user_id = 'test_user_m1'
ORDER BY created_at DESC;

-- 最近的摘要（全局）
SELECT user_id, session_id, summary_text, created_at
FROM session_memories
ORDER BY created_at DESC
LIMIT 10;
```

### 用户画像

```sql
-- 详细画像
SELECT
  user_id,
  total_sessions,
  dominant_intent,
  preferences_json->>'favorite_genres' as genres,
  preferences_json->'entity_affinities' as affinities,
  preferences_json->'activity_profile' as activity,
  first_seen_at,
  last_active_at
FROM user_profiles
ORDER BY total_sessions DESC;

-- 冷启动中的用户（未达到 L3 注入阈值）
SELECT user_id, total_sessions, first_seen_at
FROM user_profiles
WHERE total_sessions < 5
ORDER BY first_seen_at DESC;
```

### LLM 摘要质量抽检

```sql
-- 看最近 N 条摘要文本和提取的实体
SELECT
  LEFT(summary_text, 200) as summary_preview,
  jsonb_array_length(key_entities) as entity_count,
  key_entities,
  created_at
FROM session_memories
ORDER BY created_at DESC
LIMIT 20;
```

---

## 备份与恢复

### 快速导出（测试数据）

```bash
# 仅导出 L2/L3 数据（不含 RAG——那个太大且可重建）
docker exec bangumi-pg pg_dump -U myuser -d bangumidb \
  --table=session_memories \
  --table=user_profiles \
  --data-only \
  --no-owner \
  > memory_backup_$(date +%Y%m%d).sql
```

### 恢复

```bash
docker exec -i bangumi-pg psql -U myuser -d bangumidb < memory_backup_20260615.sql
```

### 全库备份（含 RAG）

```bash
# RAG 表很大（几 GB），谨慎使用
docker exec bangumi-pg pg_dump -U myuser -d bangumidb \
  --no-owner \
  > full_backup_$(date +%Y%m%d).sql
```

---

## 容器管理

### Docker Desktop

GUI 操作路径：
1. 打开 Docker Desktop → Containers
2. 找到 `bangumi-pg` → Start / Stop / Restart
3. 点击容器名 → Terminal 标签 → 可直接敲 `psql -U myuser -d bangumidb`

### 如果容器丢了

```bash
# 重新创建（数据卷还在就没事）
docker run -d --name bangumi-pg \
  -e POSTGRES_USER=myuser \
  -e POSTGRES_PASSWORD=mypassword \
  -e POSTGRES_DB=bangumidb \
  -p 5432:5432 \
  pgvector/pgvector:pg16

# 查看数据卷
docker volume ls | grep bangumi
```

---

## 常见问题

### 索引创建失败

```sql
-- 检查 pgvector 扩展是否安装
SELECT * FROM pg_extension WHERE extname = 'vector';

-- 检查 HNSW 索引是否在
SELECT indexname FROM pg_indexes WHERE tablename = 'session_memories';
```

如果 HNSW 索引缺失，重启服务后 `database/engine.py:init_db()` 会自动重建。日志中 `索引创建跳过` 的 warning 不影响基本功能——只是语义检索会走纯时效回退。

### 连接被拒绝

```bash
# 确认容器在跑
docker ps --filter name=bangumi-pg

# 确认端口映射
docker port bangumi-pg

# 大概率是容器没启动
docker start bangumi-pg
```

### 想把数据库迁到别处

```bash
# 1. 导出
docker exec bangumi-pg pg_dump -U myuser -d bangumidb --no-owner > dump.sql

# 2. 传送到目标机器
scp dump.sql user@target-host:/tmp/

# 3. 在目标机器导入
docker exec -i target-pg psql -U myuser -d bangumidb < /tmp/dump.sql
```

---

## 建议

| 阶段 | 策略 |
|------|------|
| **本地开发** | 测试用户统一前缀 `test_user_`，一条 SQL 清干净。数据量 < 10 MB |
| **内测**（3-5 人） | 正常用，不用清。每周看一眼 `pg_total_relation_size` |
| **公测**（10-50 人） | 加 `is_test_user` 列区分生产/测试数据；定期清理 30 天前的僵尸 session |
| **上线** | 独立 dev/prod 数据库实例；写入监控 dashboard 看 embedding API 成功率和 DB 写入延迟 |
