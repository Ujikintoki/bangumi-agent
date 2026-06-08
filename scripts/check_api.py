#!/usr/bin/env python3
"""Bangumi API 连通性检查 — 在启动 Agent 前运行，排除 API 问题。"""
import asyncio
import sys
import time
from pathlib import Path

# 确保项目根目录在 sys.path 中
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"

passed = 0
failed = 0


def ok(name: str, detail: str = ""):
    global passed
    passed += 1
    print(f"  {GREEN}✅ {name}{RESET} {detail}")


def fail(name: str, detail: str = ""):
    global failed
    failed += 1
    print(f"  {RED}❌ {name}{RESET} {detail}")


async def main():
    global passed, failed
    print(f"\n{YELLOW}Bangumi API 连通性检查{RESET}\n")

    # ── 1. check .env ────────────────────────────────────────
    from core.config import get_settings
    settings = get_settings()
    print("── 1. 配置检查 ──")
    if settings.BANGUMI_ACCESS_TOKEN:
        token_preview = settings.BANGUMI_ACCESS_TOKEN[:8] + "..."
        ok("BANGUMI_ACCESS_TOKEN", f"({token_preview})")
    else:
        fail("BANGUMI_ACCESS_TOKEN", "未设置！P1 API 需要此 Token")
    if settings.ZHIPU_API_KEY:
        ok("ZHIPU_API_KEY", f"({settings.ZHIPU_API_KEY[:8]}...)")
    else:
        fail("ZHIPU_API_KEY", "未设置！RAG embedding 不可用")
    print(f"  LLM: {settings.LLM_MODEL} @ {settings.LLM_BASE_URL or 'OpenAI'}")

    # ── 2. test P1 API ───────────────────────────────────────
    print("\n── 2. Bangumi P1 API ──")
    try:
        from tools.bgm_tools import search_bangumi_subject
        t0 = time.time()
        r = await search_bangumi_subject.ainvoke({"keyword": "进击的巨人", "limit": 1})
        elapsed = time.time() - t0
        if "系统提示" in str(r) or "_error" in str(r).lower():
            fail("search_bangumi_subject", f"{elapsed:.1f}s — {str(r)[:100]}")
        else:
            ok("search_bangumi_subject", f"{elapsed:.1f}s")
    except Exception as e:
        fail("search_bangumi_subject", str(e)[:100])

    try:
        from tools.bgm_tools import get_calendar
        t0 = time.time()
        r = await get_calendar.ainvoke({"weekday": "today", "limit_per_day": 1})
        elapsed = time.time() - t0
        if "系统提示" in str(r) or "抱歉" in str(r)[:20]:
            fail("get_calendar", f"{elapsed:.1f}s — {str(r)[:100]}")
        else:
            ok("get_calendar", f"{elapsed:.1f}s")
    except Exception as e:
        fail("get_calendar", str(e)[:100])

    try:
        from tools.bgm_tools import get_trending_topics
        t0 = time.time()
        r = await get_trending_topics.ainvoke({"limit": 1})
        elapsed = time.time() - t0
        if "系统提示" in str(r) or "抱歉" in str(r)[:20]:
            fail("get_trending_topics", f"{elapsed:.1f}s — {str(r)[:100]}")
        else:
            ok("get_trending_topics", f"{elapsed:.1f}s")
    except Exception as e:
        fail("get_trending_topics", str(e)[:100])

    try:
        from tools.bgm_tools import get_bangumi_subject_detail
        t0 = time.time()
        r = await get_bangumi_subject_detail.ainvoke({"subject_id": 8})
        elapsed = time.time() - t0
        if "系统提示" in str(r) or "抱歉" in str(r)[:20]:
            fail("get_bangumi_subject_detail", f"{elapsed:.1f}s — {str(r)[:100]}")
        else:
            ok("get_bangumi_subject_detail", f"{elapsed:.1f}s")
    except Exception as e:
        fail("get_bangumi_subject_detail", str(e)[:100])

    # ── 3. test RAG ──────────────────────────────────────────
    print("\n── 3. RAG (智谱 + pgvector) ──")
    try:
        from tools.bgm_tools import search_local_bangumi
        t0 = time.time()
        r = search_local_bangumi.ainvoke({"query": "进击的巨人", "limit": 1})
        elapsed = time.time() - t0
        if "无匹配" in str(r):
            ok("search_local_bangumi", f"{elapsed:.1f}s (DB 通，无此条目数据)")
        elif "系统提示" in str(r):
            fail("search_local_bangumi", f"{elapsed:.1f}s — {str(r)[:100]}")
        else:
            ok("search_local_bangumi", f"{elapsed:.1f}s")
    except Exception as e:
        fail("search_local_bangumi", str(e)[:100])

    # ── 4. summary ───────────────────────────────────────────
    total = passed + failed
    print(f"\n── 结果: {passed}/{total} 通过 ──")
    if failed == 0:
        print(f"  {GREEN}全部通过，可以启动 Agent。{RESET}\n")
    else:
        print(f"  {RED}{failed} 项失败，Agent 行为不可预期。先修复再调试 Agent。{RESET}\n")
    return failed == 0


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
