# 记忆系统配置

## 配置项一览

**文件**: `core/config.py`

### 开关

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `MEMORY_ENABLED` | `True` | L2/L3 记忆总开关。关闭后所有记忆操作变为 no-op |

### 召回控制

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `MEMORY_RECALL_TOP_K` | `5` | 召回的最大 session 摘要数 |
| `MEMORY_RECALL_THRESHOLD` | `0.5` | 语义检索余弦距离主阈值。**越小越严格**（0=完全相同，1=完全不相关） |
| `MEMORY_RECENCY_FALLBACK_THRESHOLD` | `0.70` | 回退通道的松弛阈值。比主阈值宽松，但仍有底线 |
| `MEMORY_TIME_DECAY_HALF_LIFE_DAYS` | `14` | 时间衰减半衰期（天）。**越小越强调近期** |

### 注入预算

| 配置项 | 默认值 | Agent |
|--------|--------|-------|
| `MEMORY_MAX_INJECT_TOKENS` | `500` | Research Agent 记忆注入上限 |
| `MEMORY_DIALOGUE_MAX_INJECT_TOKENS` | `300` | Dialogue Agent 记忆注入上限 |

### 用户画像

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `MEMORY_MIN_SESSIONS_FOR_PROFILE` | `5` | 开始注入画像的最低 session 数（冷启动保护） |

---

## 调优指南

### 场景 1: 记忆相关性过低（召回了不相关的内容）

**症状**: agent 回复中出现与当前话题无关的历史内容。

**调整**:
```bash
# 收紧主阈值（降低噪音）
MEMORY_RECALL_THRESHOLD=0.40

# 收紧回退锚定
MEMORY_RECENCY_FALLBACK_THRESHOLD=0.55
```

**原理**: `cosine_distance` 越小越相关。0.5 → 0.4 减少了 20% 的宽松度。

### 场景 2: 近期对话没有被召回（期望的记忆没出现）

**症状**: 昨天聊过的内容，今天问相关问题时 agent 不记得。

**调整**:
```bash
# 增加半衰期（让记忆"老得慢"）
MEMORY_TIME_DECAY_HALF_LIFE_DAYS=30

# 或增加召回数量
MEMORY_RECALL_TOP_K=8
```

**原理**: 半衰期从 14 天提高到 30 天，7 天前的记忆分数从 0.71 提高到 0.85。

### 场景 3: 记忆注入占用过多 token，挤压对话空间

**症状**: agent 回复变短、截断，或对话历史被过度裁剪。

**调整**:
```bash
# 减少注入预算
MEMORY_MAX_INJECT_TOKENS=300
MEMORY_DIALOGUE_MAX_INJECT_TOKENS=150
```

**原理**: L1 对话窗口 = 总预算 - System Prompt - L2 注入。减小注入给对话留更多空间。

### 场景 4: 用户画像太早出现（新用户就被打标签）

**症状**: 新用户只聊了 2-3 次就看到"偏好XX类作品"。

**调整**:
```bash
# 提高冷启动门槛
MEMORY_MIN_SESSIONS_FOR_PROFILE=10
```

### 场景 5: 完全关闭记忆（调试/对比测试）

```bash
MEMORY_ENABLED=False
```

Agent 退化回纯 L1 滑动窗口模式。

---

## 配置组合建议

### 默认（平衡）

```env
MEMORY_ENABLED=True
MEMORY_RECALL_TOP_K=5
MEMORY_RECALL_THRESHOLD=0.5
MEMORY_RECENCY_FALLBACK_THRESHOLD=0.70
MEMORY_TIME_DECAY_HALF_LIFE_DAYS=14
MEMORY_MAX_INJECT_TOKENS=500
MEMORY_DIALOGUE_MAX_INJECT_TOKENS=300
MEMORY_MIN_SESSIONS_FOR_PROFILE=5
```

适用: 通用场景，日活用户。

### 精准优先（减少噪音）

```env
MEMORY_RECALL_THRESHOLD=0.40
MEMORY_RECENCY_FALLBACK_THRESHOLD=0.55
MEMORY_RECALL_TOP_K=3
MEMORY_MIN_SESSIONS_FOR_PROFILE=10
```

适用: 对记忆精度要求高，宁可漏掉也不愿意错。

### 记忆优先（长上下文）

```env
MEMORY_TIME_DECAY_HALF_LIFE_DAYS=30
MEMORY_RECALL_TOP_K=8
MEMORY_MAX_INJECT_TOKENS=800
MEMORY_RECALL_THRESHOLD=0.55
```

适用: 低频用户，希望尽可能找回历史上下文。

---

## 环境变量设置方式

```bash
# 方式 1: .env 文件（推荐）
echo "MEMORY_RECALL_THRESHOLD=0.40" >> .env

# 方式 2: 启动时注入
MEMORY_RECALL_THRESHOLD=0.40 uvicorn main:app --reload

# 方式 3: Docker
docker run -e MEMORY_RECALL_THRESHOLD=0.40 ...
```

**优先级**: 环境变量 > .env 文件 > 默认值（pydantic-settings 自动处理）
