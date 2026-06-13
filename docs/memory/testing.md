# 记忆系统测试

## 测试文件

| 文件 | 用例数 | 覆盖范围 |
|------|--------|---------|
| `test/test_memory.py` | 31 | L1 短记忆 (21) + 时间衰减评分 (10) |
| `test/test_memory_manager.py` | 15 | L2/L3 纯函数 (13) + DB 冒烟 (2) |

## 运行测试

```bash
# 运行全部记忆相关测试
pytest test/test_memory.py test/test_memory_manager.py -v

# 仅运行 L1 测试
pytest test/test_memory.py -v

# 仅运行 L2/L3 测试（跳过 DB 测试）
pytest test/test_memory_manager.py -v -m "not database"

# 仅运行 DB 测试（需要 PostgreSQL + pgvector）
pytest test/test_memory_manager.py -v -m "database"

# 运行特定测试类
pytest test/test_memory.py::TestComputeCombinedScore -v
```

## 测试覆盖详解

### L1 短记忆 (`test/test_memory.py`)

#### `TestCountTokens` (6 tests)

验证 tiktoken 精确计数：
- 英文、中文、混合文本
- 空字符串
- 长文本正比性
- 与直接 tiktoken 编码一致性

#### `TestEstimateTokens` (5 tests)

验证多消息类型 token 估算：
- 单条 HumanMessage
- 混合类型（System + Human + AI + Tool）
- 空列表
- content 长度正比性
- AIMessage list[dict] content

#### `TestTrimMessages` (6 tests)

验证滑动窗口截断：
- SystemMessage 保留
- 旧消息从头部截断
- 预算内不截断
- SystemMessage 超预算仍保留
- 返回类型一致
- 大量消息截断

#### `TestManageMemory` (4 tests)

验证两步截断入口：
- 预算内原样返回（引用相等）
- 超预算截断（返回新列表）
- 空列表处理
- 默认 max_tokens

#### `TestComputeCombinedScore` (10 tests)

验证时间衰减公式 `similarity × 0.5^(days/half_life)`：

| 测试 | 场景 | 期望 |
|------|------|------|
| `test_perfect_match_today` | cos_dist=0, 今天 | ~1.0 |
| `test_threshold_match_today` | cos_dist=0.5, 今天 | ~0.5 |
| `test_perfect_match_one_half_life` | cos_dist=0, 14天前 | ~0.5 |
| `test_threshold_match_one_half_life` | cos_dist=0.5, 14天前 | ~0.25 |
| `test_old_memory_decayed` | cos_dist=0.45, 60天前 | <0.10 |
| `test_recent_beats_old` | 近期(2天,0.48) vs 远期(60天,0.45) | 近期 > 远期 |
| `test_naive_utc_handled` | naive datetime | 正常处理 |
| `test_zero_half_life_clamped` | half_life=0 | clamp 到 1 |
| `test_future_date_clamped` | 未来时间戳 | clamp 到 0 天 |
| `test_completely_irrelevant` | cos_dist=1.0 | score=0 |

### L2/L3 长记忆 (`test/test_memory_manager.py`)

#### `TestExtractKeyEntities` (5 tests)

| 测试 | 输入 | 期望 |
|------|------|------|
| `test_chinese_brackets` | 「高达Seed」「星际牛仔」 | 提取 2 个 |
| `test_chinese_quotes` | "进击的巨人" | 提取 1 个 |
| `test_deduplication` | 重复「高达Seed」 | 去重为 1 |
| `test_empty_summary` | "" | [] |
| `test_no_brackets` | 无引号文本 | [] |

#### `TestFormatConversationText` (2 tests)

| 测试 | 场景 | 验证 |
|------|------|------|
| `test_basic_conversation` | Human + AIMessage | SystemMessage 被过滤 |
| `test_tool_messages_filtered` | 含 ToolMessage | ToolMessage 被过滤 |

#### `TestFormatMemoryContext` (3 tests)

| 测试 | 场景 |
|------|------|
| `test_formats_session_with_time_string` | 1 条 session → 含"今天" |
| `test_empty_sessions_no_profile_returns_empty` | 空 → "" |
| `test_includes_profile_when_present` | 含画像 → 含类型名 |

#### `TestFormatProfileSummary` (3 tests)

| 测试 | 场景 |
|------|------|
| `test_genres_and_affinities` | 含类型+亲和度 |
| `test_empty_prefs` | `preferences_json={}` → "" |
| `test_none_prefs` | `preferences_json=None` → "" |

#### `TestSessionMemoryDB` (2 tests, `@pytest.mark.database`)

| 测试 | 场景 |
|------|------|
| `test_insert_and_query` | 写入→按 user_id 查询→清理 |
| `test_anonymous_guard` | user_id="anonymous" → 返回 "" |

---

## 扩写测试指南

### 新增 L1 测试

在 `test/test_memory.py` 中添加测试类或方法。L1 为纯函数，不需要 DB。

```python
class TestNewFeature:
    def test_something(self):
        from agent.memory import count_tokens
        assert count_tokens("test") > 0
```

### 新增 L2 纯函数测试

在 `test/test_memory_manager.py` 的纯函数区域添加。不需要 `@pytest.mark.database`。

```python
class TestNewFeature:
    def test_something(self):
        result = MemoryManager._some_static_method(...)
        assert result == expected
```

### 新增 DB 集成测试

使用 `@pytest.mark.database` 标记，需要 PostgreSQL + pgvector 运行中。在测试方法中使用 `_init_tables` fixture（autouse）确保表存在。

```python
@pytest.mark.database
class TestNewDBFeature:
    @pytest.fixture(autouse=True)
    def _init_tables(self):
        from database.engine import init_db
        init_db()

    def test_db_something(self):
        # SQLModel Session 操作
        ...
```

### Mock 注意事项

- `MemoryManager._format_memory_context` 是实例方法，mock 时需要 patch `_format_profile_summary`
- `SessionMemory` mock 需要 `summary_text`、`created_at`（带时区）属性
- `UserProfile` mock 需要 `preferences_json`、`total_sessions` 属性
