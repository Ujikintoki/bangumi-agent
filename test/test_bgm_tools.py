"""
工具输出格式测试

验证 search_local_bangumi 异步化 + 产出侧投影的跨层契约。
"""
from __future__ import annotations

import asyncio
import inspect
import json

from agent.memory.short_term import _MAX_SINGLE_MESSAGE_TOKENS, count_tokens
from tools.bgm_tools import (
    _local_payload,
    search_local_bangumi,
)


# ═══════════════════════════════════════════════════════════════════════════
# Agent type contextvar — removed (last consumer _format_person_detail deleted)
# ═══════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════
# search_local_bangumi 异步化
# ═══════════════════════════════════════════════════════════════════════════


class TestSearchLocalAsync:
    def test_underlying_func_is_async(self):
        """底层函数应为 async def（@tool 装饰器将其包装为 StructuredTool）。"""
        # LangChain @tool 将 async def 包装为 StructuredTool，coroutine 属性指向原函数
        coro_func = getattr(search_local_bangumi, "coroutine", None)
        assert coro_func is not None, "search_local_bangumi 应有 coroutine 属性"
        assert inspect.iscoroutinefunction(coro_func)

    def test_no_block_event_loop(self):
        """调用不应阻塞事件循环（即使 RAG 不可用，函数应在超时前返回）。

        v2 契约：search_local_bangumi 返回结构化 dict
        （{"total", "shown", "results"}，无结果/出错时 {"_error": ...}）。
        """
        coro_func = search_local_bangumi.coroutine
        result = asyncio.run(
            asyncio.wait_for(
                coro_func("测试查询", limit=1),
                timeout=5.0,
            )
        )
        assert isinstance(result, dict)
        assert "results" in result or "_error" in result


# ═══════════════════════════════════════════════════════════════════════════
# 索引卡投影 —— 跨层契约：工具产出必须塞得进 L1 单条消息上限
# ═══════════════════════════════════════════════════════════════════════════
#
# 背景（2026-09-14 实测）：投影前 11/11 次真实调用超限，真实 payload 中位
# 20,522 token（上限的 10 倍），114 条候选只有 5 条完整进了模型 ——
# 模型以为自己在一个 10-15 条的列表里挑，实际只看得见前 1-2 条。
#
# 上限定义在记忆层、封顶发生在数据层，所以这条契约只能在这里断言
# （与 test_sanitizers.py 的 test_record_fits_l1_single_message_limit 同款）。


def _fake_settings():
    """替身 Settings：只要 ZHIPU_API_KEY 一个字段（检索器构造用）。"""
    from types import SimpleNamespace
    return SimpleNamespace(ZHIPU_API_KEY="test-key")


def _subject(i: int, name_len: int = 10, info_len: int = 20,
             n_tags: int = 10) -> tuple[dict, str]:
    """一条 subject rag_output（带全部该被砍掉的胖字段）+ entity_type。"""
    return (
        {
            "id": 1000 + i,
            "name": "アニメ" + "長" * name_len,
            "name_cn": "动画" + "长" * name_len,
            "type": "TV",
            "date": "2024-07-13",
            "eps": 12,
            "score": 7.8,
            "rank": 1024,
            "info": "TV动画 2024年7月" + "情" * info_len,
            "tags": [{"name": f"标签{j}", "count": 100 + j} for j in range(n_tags)],
            # ↓ 以下都必须不出现在卡片里
            "summary": "剧情" * 500,
            "infobox": {"导演": "某人" * 100},
            "collection": {"想看": 1, "看过": 2},
            "rating_count": [1] * 10,
            "rating_total": 999,
            "_next": "如需口碑数据调 get_subject_opinions(1000)",
            "_source": "rag",
            "subject_type": 2,
        },
        "subject",
    )


class TestLocalSearchCardContract:
    def test_payload_fits_l1_single_message_limit(self):
        """最胖输入下，整个 payload 必须塞得进 L1 单条上限。

        对整个 json.dumps 数 token，不是对卡片串求和 —— 信封与分隔符也算。
        """
        recs = [_subject(i, name_len=40, info_len=300, n_tags=30) for i in range(20)]
        payload = _local_payload(recs)
        size = count_tokens(json.dumps(payload, ensure_ascii=False))
        assert size <= _MAX_SINGLE_MESSAGE_TOKENS, (
            f"payload {size} token 超过 L1 单条上限 {_MAX_SINGLE_MESSAGE_TOKENS}"
        )

    def test_card_keeps_only_index_fields(self):
        """卡片是「选择用索引卡」：留定位字段，不留剧情/班底。"""
        payload = _local_payload([_subject(0)])
        card = payload["results"][0]
        for key in ("id", "name", "name_cn", "type", "date", "eps",
                    "score", "rank", "info", "entity_type"):
            assert key in card, f"定位字段 {key} 不该被砍"
        for key in ("summary", "infobox", "collection", "rating_count",
                    "rating_total", "_next", "_source", "subject_type"):
            assert key not in card, f"{key} 能耗尽预算且对「选哪一部」无价值"
        # id 是下游（detail 工具、eval 判官、录制侧）识别记录的唯一依据
        assert card["id"] == 1000
        assert card["entity_type"] == "subject"

    def test_tags_keep_names_only(self):
        """标签名是卡片里最便宜的味道信号；count 是无用重量。"""
        payload = _local_payload([_subject(0, n_tags=30)])
        card = payload["results"][0]
        assert card["tags"] == [f"标签{j}" for j in range(8)]
        assert all(isinstance(t, str) for t in card["tags"])

    def test_drops_whole_records_and_says_so(self):
        """超预算整条丢（不切单条），并显式报出还剩几条没显示。"""
        recs = [_subject(i, name_len=40, info_len=300, n_tags=30) for i in range(20)]
        payload = _local_payload(recs)
        assert payload["shown"] < payload["total"]
        assert len(payload["results"]) == payload["shown"]
        assert str(payload["total"] - payload["shown"]) in payload["note"]

    def test_never_drops_everything(self):
        """保底 1 条。

        全丢光会让 main._tool_payload_kind 把 {"results": [], "total": 10}
        判成 "empty"（它只看容器非空），于是降级话术对用户说
        「翻过了，没有找到匹配的内容」—— 明明找到了 10 条。
        """
        monster = ({"id": 1, "name": "長" * 100_000, "entity_type": "subject"},
                   "subject")
        payload = _local_payload([monster] + [_subject(i) for i in range(1, 10)])
        assert payload["total"] == 10
        assert payload["shown"] >= 1, "保底条数被破坏，降级话术会撒谎"
        assert payload["results"]

    def test_envelope_precedes_results(self):
        """信封键排在 results 之前 —— 头部截断保头弃尾。"""
        payload = _local_payload([_subject(i, n_tags=30) for i in range(20)])
        keys = list(payload)
        assert keys[-1] == "results"
        assert keys[:2] == ["total", "shown"]

    def test_shown_is_monotonic_in_limit(self):
        """limit 调大，shown 不会变少（结果按相关度排序，投影是前缀截断）。"""
        recs = [_subject(i) for i in range(20)]
        prev = 0
        for limit in (3, 5, 8, 10, 15, 20):
            shown = _local_payload(recs[:limit])["shown"]
            assert shown >= prev, f"limit={limit} 的 shown 比更小的 limit 还少"
            prev = shown

    def test_no_note_when_nothing_dropped(self):
        payload = _local_payload([_subject(0), _subject(1)])
        assert payload["shown"] == payload["total"] == 2
        assert "note" not in payload


class TestLocalSearchToolWiring:
    """走真实工具装配路径（只 patch 检索器与设置，不碰库、不联网）。"""

    def _run(self, monkeypatch, raw_records):
        import core.config
        import rag.retriever
        from tools import bgm_tools

        class _FakeRetriever:
            def __init__(self, **kwargs):
                pass

        fake_results = [
            rag.retriever.RagSearchResult(
                entity_id=f"{et}_{rec['id']}",
                entity_type=et,
                rag_output=json.dumps(rec, ensure_ascii=False),
                name=rec.get("name", ""),
                cosine_distance=0.1,
            )
            for rec, et in raw_records
        ]
        monkeypatch.setattr(rag.retriever, "RagEntityRetriever", _FakeRetriever)
        monkeypatch.setattr(core.config, "get_settings", _fake_settings)
        monkeypatch.setattr(
            bgm_tools, "_unified_search", lambda **kwargs: fake_results
        )
        return bgm_tools._search_local_bangumi_sync("测试", limit=len(fake_results))

    def test_projects_real_rag_output(self, monkeypatch):
        """工具出口不再原样透传 rag_output。"""
        recs = [_subject(i, n_tags=30) for i in range(10)]
        payload = self._run(monkeypatch, recs)
        assert payload["total"] == 10
        assert "summary" not in json.dumps(payload["results"], ensure_ascii=False)
        assert count_tokens(json.dumps(payload, ensure_ascii=False)) <= \
            _MAX_SINGLE_MESSAGE_TOKENS

    def test_entity_type_comes_from_the_result_not_the_record(self, monkeypatch):
        """character 的 rag_output 里没有 type，卡片靠 entity_type 才认得出。"""
        char = ({"id": 7, "name": "角色", "name_cn": "角色", "role": "主角",
                 "info": "某作品的主角", "summary": "剧情" * 100}, "character")
        person = ({"id": 8, "name": "某人", "name_cn": "某人", "type": "个人",
                   "career": ["声优"], "info": "日本声优", "summary": "简介" * 100},
                  "person")
        payload = self._run(monkeypatch, [char, person])
        cards = {c["id"]: c for c in payload["results"]}
        assert cards[7]["entity_type"] == "character"
        assert cards[7]["role"] == "主角"
        assert "type" not in cards[7], "character 本就没有 type，别造假字段"
        assert cards[8]["entity_type"] == "person"
        assert cards[8]["career"] == ["声优"]

    def test_broken_rag_output_skipped_not_fatal(self, monkeypatch):
        import core.config
        import rag.retriever
        from tools import bgm_tools

        class _FakeRetriever:
            def __init__(self, **kwargs):
                pass

        good, et = _subject(1)
        results = [
            rag.retriever.RagSearchResult(
                entity_id="subject_0", entity_type="subject",
                rag_output="{ 不是 JSON", name="坏", cosine_distance=0.1,
            ),
            rag.retriever.RagSearchResult(
                entity_id="subject_1", entity_type=et,
                rag_output=json.dumps(good, ensure_ascii=False),
                name="好", cosine_distance=0.2,
            ),
        ]
        monkeypatch.setattr(rag.retriever, "RagEntityRetriever", _FakeRetriever)
        monkeypatch.setattr(core.config, "get_settings", _fake_settings)
        monkeypatch.setattr(bgm_tools, "_unified_search", lambda **kwargs: results)
        payload = bgm_tools._search_local_bangumi_sync("测试", limit=2)
        assert payload["total"] == 1
        assert payload["results"][0]["id"] == 1001

    def test_all_unparsable_returns_error(self, monkeypatch):
        import core.config
        import rag.retriever
        from tools import bgm_tools

        class _FakeRetriever:
            def __init__(self, **kwargs):
                pass

        results = [
            rag.retriever.RagSearchResult(
                entity_id="subject_0", entity_type="subject",
                rag_output="{ 不是 JSON", name="坏", cosine_distance=0.1,
            )
        ]
        monkeypatch.setattr(rag.retriever, "RagEntityRetriever", _FakeRetriever)
        monkeypatch.setattr(core.config, "get_settings", _fake_settings)
        monkeypatch.setattr(bgm_tools, "_unified_search", lambda **kwargs: results)
        payload = bgm_tools._search_local_bangumi_sync("测试", limit=1)
        assert "_error" in payload
