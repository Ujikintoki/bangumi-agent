# 记忆系统调试指南

## 日志关键字

所有记忆相关日志使用 `bgm-agent.memory` 和 `bgm-agent.memory_manager` logger。

### 关键日志行速查

```bash
# 记忆召回成功
grep "\[Memory\] 召回" app.log
# → [Memory] 召回 342 字 (user=test_user)

# 回退通道触发
grep "recency fallback" app.log
# → [Memory] recency fallback 补齐 2 条 (语义命中 3, user=test_user)

# 写入调度
grep "remember_session" app.log

# L1 截断
grep "memory: 截断" app.log
# → memory: 截断 3 条旧消息（15 → 12 条），Token: 7850/8000

# embedding 失败
grep "embed_single 失败\|embedding 失败" app.log

# 语义检索失败
grep "语义检索失败" app.log

# 画像更新失败
grep "画像更新失败\|画像读取失败" app.log

# 摘要生成失败
grep "会话摘要 LLM" app.log

# 记忆关闭/匿名
grep "MEMORY_ENABLED" app.log  # 不打印，直接 no-op
```

---

## 常见问题排查

### 1. 记忆完全没有被召回

**检查清单**:

1. `user_id` 是否为 `"anonymous"`？
   ```bash
   # 查看请求日志中的 user_id
   grep "user_id" app.log
   ```
   匿名用户不触发记忆。

2. `MEMORY_ENABLED` 是否为 `True`？
   ```python
   from core.config import get_settings
   print(get_settings().MEMORY_ENABLED)
   ```

3. embedding API 是否可用？
   ```bash
   grep "embed_single 失败\|智谱客户端不可用" app.log
   ```
   如果 embedding 不可用，check `ZHIPU_API_KEY` 和 `zai-sdk` 安装。

4. `recall_for_prompt` 是否只在 `iterations==0` 执行？
   ```bash
   grep "\[Memory\]" app.log
   ```
   只应在首轮出现一次。如果完全不出现，检查 reasoning_node 的 `iterations` 条件。

### 2. 记忆召回但相关性很差

**排查步骤**:

1. 查看召回日志确认语义命中数：
   ```bash
   grep "recency fallback" app.log
   ```
   - 如果 `语义命中 0, fallback 补齐 5` → 语义通道全未命中，全靠回退 → 收紧 `MEMORY_RECALL_THRESHOLD` 无济于事，需改善 embedding 质量
   - 如果 `语义命中 3, fallback 补齐 2` → 语义 + 回退混合，正常

2. 查看注入的 memory_context 内容：
   临时在 `reasoning_node` 中加 debug log：
   ```python
   logger.debug("[Memory] 注入内容:\n%s", memory_context)
   ```

3. 调整阈值：
   ```bash
   # 收紧主阈值
   MEMORY_RECALL_THRESHOLD=0.40
   # 收紧回退锚定
   MEMORY_RECENCY_FALLBACK_THRESHOLD=0.55
   ```

### 3. 近期对话没有被召回（"金鱼记忆"）

**检查**:

1. 时间衰减是否过强？
   ```bash
   # 查看当前半衰期
   grep "MEMORY_TIME_DECAY_HALF_LIFE_DAYS" .env
   ```
   默认 14 天。如果 7 天前的记忆就应该被召回但没有，尝试增大到 30。

2. `_remember_session` 是否成功写入？
   ```bash
   grep "remember_session 异常\|session_memory 写入失败\|摘要为空" app.log
   ```
   如果写入失败，查不到数据自然召回不到。

3. 直接查 DB 确认数据存在：
   ```sql
   SELECT id, summary_text, created_at
   FROM session_memories
   WHERE user_id = 'your_user_id'
   ORDER BY created_at DESC
   LIMIT 10;
   ```

### 4. 记忆注入导致 token 超限

**症状**: L1 日志显示大量截断，对话历史被过度裁剪。

**检查**:
```bash
grep "memory: 截断" app.log
```

**调整**:
```bash
# 减少注入预算
MEMORY_MAX_INJECT_TOKENS=300
# 或减少召回数量
MEMORY_RECALL_TOP_K=3
```

**原理**: 总 token 预算固定，L2 注入多 → L1 滑动窗口小 → 对话历史裁剪多。

### 5. 新用户被错误地打上画像标签

**症状**: 只聊了 2-3 次的用户看到"偏好机战类作品"。

**检查**:
```bash
grep "MEMORY_MIN_SESSIONS_FOR_PROFILE" .env
```

默认值 5。检查是否有老数据（之前阈值更低时的残留）。

```sql
SELECT user_id, total_sessions, preferences_json
FROM user_profiles
WHERE total_sessions < 5;
```

### 6. embedding API 频繁超时

**症状**: 日志中大量 `embed_single 失败`，记忆回退到纯时效排序。

**检查**:
```bash
grep "embed_single 失败" app.log | wc -l
```

**处理**:
- 检查 `ZHIPU_API_KEY` 有效性和余额
- 检查 `ZHIPU_BASE_URL` 网络可达性
- embedding API 失败不阻塞主流程——回退到纯 recency 排序

---

## 调试开关

### 启用详细日志

在 `main.py` 或启动脚本中设置日志级别：

```python
logging.getLogger("bgm-agent.memory").setLevel(logging.DEBUG)
logging.getLogger("bgm-agent.memory_manager").setLevel(logging.DEBUG)
```

或通过环境变量：
```bash
LOG_LEVEL=DEBUG uvicorn main:app --reload
```

### 临时禁用记忆

```bash
MEMORY_ENABLED=False uvicorn main:app --reload
```

用于对比测试：有记忆 vs 无记忆的回复质量。

### 直接调用 MemoryManager（REPL 调试）

```python
import asyncio
from agent.memory_manager import get_memory_manager

mm = get_memory_manager()

# 测试召回
result = asyncio.run(
    mm.recall_for_prompt(user_id="test_user", query="推荐机战番")
)
print(result)

# 查看 DB 中的 session 摘要
from sqlmodel import Session, select
from database.engine import engine
from database.memory_tables import SessionMemory

with Session(engine) as s:
    rows = s.exec(
        select(SessionMemory)
        .where(SessionMemory.user_id == "test_user")
        .order_by(SessionMemory.created_at.desc())
        .limit(5)
    ).all()
    for r in rows:
        print(f"[{r.created_at}] {r.summary_text[:100]}")
```

### 验证时间衰减计算

```python
from datetime import datetime, timedelta, timezone
from agent.memory_manager import MemoryManager

# 今天 perfect match
score = MemoryManager._compute_combined_score(
    cosine_distance=0.0,
    created_at=datetime.now(timezone.utc),
    half_life_days=14,
)
print(f"今天 perfect: {score:.4f}")  # ~1.0

# 14 天前 perfect match
score = MemoryManager._compute_combined_score(
    cosine_distance=0.0,
    created_at=datetime.now(timezone.utc) - timedelta(days=14),
    half_life_days=14,
)
print(f"14 天前 perfect: {score:.4f}")  # ~0.5
```
