"""有限匿名真实 HTTP 验收；不运行 lifespan，不初始化/连接 PostgreSQL。

运行此脚本会调用配置中的真实模型与 Bangumi 只读接口。
TIKTOKEN_CACHE_DIR=/private/tmp/bgm-agent-tiktoken .venv/bin/python scripts/verify_chat_http.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.update(MEMORY_ENABLED="false", AUTH_ENABLED="false", RATE_LIMIT_PER_MINUTE="0")


async def run(*, independent_only: bool = False) -> None:
    import httpx
    import psycopg2
    import uvicorn

    db_attempts = []

    def deny_db(*args, **kwargs):
        db_attempts.append(True)
        raise RuntimeError("HTTP acceptance forbids PostgreSQL access")

    psycopg2.connect = deny_db
    from langchain_core.messages import ToolMessage

    from agent.memory.cache import get_session_cache
    from main import _current_turn_start, _session_key, app

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, lifespan="off", log_level="warning",
    ))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    session_id = ""
    try:
        for _ in range(200):
            if server.started:
                break
            if task.done():
                await task
                raise RuntimeError("Acceptance server exited before startup")
            await asyncio.sleep(0.025)
        if not server.started:
            raise RuntimeError("Acceptance server startup timeout")
        questions = [
            "最近《赛博朋克：边缘行者2》上线了。它现在在站里的评分是多少？热度呢？和第一季相比如何？",
            "《边缘行者 2》口碑是不是暴雷了，感觉第二季比第一季差太多了。。。",
            "班友们对《边缘行者 2》的评论简直有才。。。你感觉最有梗的评论是哪一条，请你锐评一下这位班友的‘锐评’",
        ]
        if independent_only:
            questions = questions[-1:]
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=120) as client:
            for index, question in enumerate(questions, 1):
                response = await client.post("/chat", json={"message": question, "session_id": session_id})
                response.raise_for_status()
                data = response.json()
                session_id = data["session_id"]
                result = data.get("turn_result", {})
                messages = await get_session_cache().load(_session_key("anonymous", session_id))
                evidence = []
                selected_ids = set(re.findall(r"#likes_grid_(\d+)", data["reply"]))
                for message in messages[_current_turn_start(messages) + 1:]:
                    if not isinstance(message, ToolMessage):
                        continue
                    try:
                        payload = json.loads(message.content)
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(payload, dict):
                        continue
                    if message.name in {"search_bangumi_subject", "get_trending_subjects"}:
                        rows = payload.get("results", payload.get("items", []))
                        evidence.append({"tool": message.name, "records": [
                            {key: row[key] for key in ("id", "name", "name_cn", "score", "rank", "trending_score") if key in row}
                            for row in rows[:5] if isinstance(row, dict)
                        ]})
                    if message.name == "get_subject_opinions":
                        evidence.append({"tool": message.name, "selected_quotes": [
                            {key: item.get(key) for key in ("id", "author", "content", "url", "truncated")}
                            for item in payload.get("comments", {}).get("items", [])
                            if str(item.get("id")) in selected_ids
                        ]})
                print(json.dumps({
                    "case": "S3-independent" if independent_only else f"S{index}", "http_status": response.status_code,
                    "reply": data["reply"], "tools": data["tools_used"],
                    "execution_status": result.get("status"), "task_status": result.get("task_status"),
                    "missing": result.get("missing"),
                    "elapsed_ms": (data.get("telemetry") or {}).get("elapsed_ms"),
                    "source_ids": [item.get("source_id") for source in result.get("sources", [])
                                   for item in source.get("comments_sample", {}).get("items", [])],
                    "tool_evidence": evidence,
                }, ensure_ascii=False), flush=True)
            if independent_only:
                return
            correction = "我说的不是剧情好坏，是这条评论怎么把剧情讲成笑话的。继续聊这个表达手法。"
            async with client.stream("POST", "/chat/stream", json={
                "message": correction, "session_id": session_id,
            }) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data: ") or line == "data: [DONE]":
                        continue
                    event = json.loads(line[6:])
                    event.pop("session_id", None)
                    if "turn_result" in event:
                        event["turn_result"] = {key: event["turn_result"].get(key)
                                                for key in ("status", "task_status", "missing")}
                    print(json.dumps({"case": "correction-SSE", **event}, ensure_ascii=False), flush=True)
    finally:
        server.should_exit = True
        await task
        listener.close()
        print(json.dumps({"database_attempts": len(db_attempts), "lifespan": "off"}), flush=True)
        if db_attempts:
            raise RuntimeError("Database isolation failed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--independent-only", action="store_true", help="只运行新会话 S3 对照")
    asyncio.run(run(independent_only=parser.parse_args().independent_only))
