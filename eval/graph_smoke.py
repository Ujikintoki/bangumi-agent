"""
端到端场景评测 runner v0

用法::
    python -m eval.graph_smoke                   # 默认跑 eval/data/e2e_scenarios.json
    python -m eval.graph_smoke --data <path>     # 指定场景集
    python -m eval.graph_smoke --smoke 3         # 只跑前 N 条（冒烟/联调用）

确定性断言（无 LLM-judge，全部可复现）：
  1. query_intent == expect_intent                  —— 意图命中
  2. tools_used ∩ forbid_tools == ∅（"*"=全部禁调） —— 禁调工具
  3. require_tools ⊆ tools_used                    —— 必调工具
  4. word_range[0] <= len(reply) <= word_range[1]   —— 字数合规（P0#1）
  5. no_markdown_table → reply 不含 "|" 表格行       —— 格式泄漏（_degrade_render_input 存在的原因）

产出：eval/results/graph_smoke-<date>-<githash>.json + .md
  包含：A-E 维度分组通过率 / by-intent 分组通过率 / 延迟 p50 p95 / token 汇总与成本估算（粗估常量，见 _PRICE）。

v0 已知限制（写进报告头）：
  - Bangumi API 真实调用（站点数据漂移 → 断言只测行为不测数据内容）
  - LLM 非确定 → 单条结果有抖动，看趋势与 delta 不看单次绝对值
  - requires_token=true 的场景在无 BANGUMI_ACCESS_TOKEN 时跳过
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

# 确保项目根目录在 Python path 中（python -m eval.graph_smoke 时已满足，防御性保留）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

RESULTS_DIR = Path(__file__).resolve().parent / "results"

# 成本估算常量（DeepSeek 粗估，¥/1M tokens；随模型定价调整，仅用于预算意识）
_PRICE = {"input_yuan_per_1m": 1.0, "output_yuan_per_1m": 2.0}


def _git_hash() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=str(Path(__file__).resolve().parent.parent),
            timeout=5,
        ).stdout.strip() or "nogit"
    except Exception:
        return "nogit"


def _assert_scenario(scen: dict, res: dict) -> list[str]:
    """确定性断言，返回失败原因列表（空=通过）。"""
    failures: list[str] = []
    reply = res.get("reply") or ""
    wc = len(reply)

    if res.get("query_intent") != scen["expect_intent"]:
        failures.append(
            f"意图: expect={scen['expect_intent']} got={res.get('query_intent')}"
        )

    tools = set(res.get("tools_used") or [])
    forbid = set(scen.get("forbid_tools") or [])
    if forbid == {"*"}:
        if tools:
            failures.append(f"禁调全部工具但调用了: {sorted(tools)}")
    elif forbid & tools:
        failures.append(f"调用了禁调工具: {sorted(forbid & tools)}")

    require = set(scen.get("require_tools") or [])
    if require and not require <= tools:
        failures.append(f"未调用必调工具: {sorted(require - tools)}（实际: {sorted(tools)}）")

    # require_any_tools: 每组内任一工具被调用即通过（如探索类可用 search_bangumi_subject 或 search_local_bangumi）
    for group in scen.get("require_any_tools") or []:
        if not set(group) & tools:
            failures.append(f"未调用任一要求工具 {group}（实际: {sorted(tools)}）")

    lo, hi = scen.get("word_range", [1, 300])
    if not (lo <= wc <= hi):
        failures.append(f"字数 {wc} 超出 [{lo}, {hi}]")

    if scen.get("no_markdown_table"):
        if any(line.lstrip().startswith("|") and line.rstrip().endswith("|") for line in reply.splitlines()):
            failures.append("回复包含 markdown 表格行")

    if res.get("error_flag") or res.get("status_code") != 200:
        failures.append(f"端点状态 {res.get('status_code')} / error_flag={res.get('error_flag')}")

    return failures


async def _run_scenario(client, scen: dict, run_id: str) -> dict:
    payload = {
        "message": scen["message"],
        "depth": scen.get("depth", "fast"),
        "output_style": scen.get("output_style", "bangumi"),
        "session_id": f"eval-{scen['id']}",
        # ★ 密闭性：L2 记忆召回按 user_id 检索（agent/memory/long_term.py 里
        #   `SessionMemory.user_id == user_id`），session_id 压根不参与查询。
        #   原来固定写 "eval"，于是第 N 次运行一开局就带着第 N-1 次的记忆，
        #   同一轮里前面的场景也会污染后面的 —— 门禁的 PASS/FAIL 因此不可复现。
        #   带上 run_id 之后每次运行都是干净起点；场景 id 让场景之间也互不干扰。
        "user_id": f"eval-{run_id}-{scen['id']}",
    }
    t0 = time.monotonic()
    resp = await client.post("/chat", json=payload)
    latency_ms = (time.monotonic() - t0) * 1000
    body = resp.json()
    return {
        "status_code": resp.status_code,
        "reply": body.get("reply", ""),
        "iterations": body.get("iterations", 0),
        "tools_used": body.get("tools_used", []),
        "query_intent": body.get("query_intent", "unknown"),
        "error_flag": body.get("reply", "").startswith("抱歉")
        or "查询处理超时" in body.get("reply", ""),
        "latency_ms": latency_ms,
        "telemetry": body.get("telemetry"),
    }


async def run(scenarios: list[dict], smoke: int = 0) -> dict:
    from core.config import get_settings
    settings = get_settings()
    settings.DEV_MODE = True  # 拿 telemetry（token/节点耗时）

    # import main 必须在 DEV_MODE 置位之后（main 读取 settings 单例）
    from main import app

    from httpx import ASGITransport, AsyncClient

    results: list[dict] = []
    skipped: list[dict] = []
    to_run = scenarios[:smoke] if smoke else scenarios
    # 本次运行的隔离标识 —— 见 _run_scenario 里 user_id 那段
    run_id = time.strftime("%Y%m%d-%H%M%S")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://eval") as client:
        for i, scen in enumerate(to_run):
            if scen.get("requires_token") and not settings.BANGUMI_ACCESS_TOKEN:
                skipped.append({"id": scen["id"], "reason": "requires_token 但无 BANGUMI_ACCESS_TOKEN"})
                continue
            try:
                res = await _run_scenario(client, scen, run_id)
                res["failures"] = _assert_scenario(scen, res)
                res["scenario_id"] = scen["id"]
                res["expect_intent"] = scen["expect_intent"]
                results.append(res)
            except Exception as exc:  # 单场景失败不中断整轮
                results.append({
                    "scenario_id": scen["id"], "status_code": 0, "reply": "",
                    "iterations": 0, "tools_used": [], "query_intent": "error",
                    "error_flag": True, "latency_ms": 0, "telemetry": None,
                    "failures": [f"异常: {type(exc).__name__}: {exc}"],
                })
            if (i + 1) % 5 == 0 or i == len(to_run) - 1:
                print(f"  进度: {i + 1}/{len(to_run)}")

    return _aggregate(results, skipped)


def _aggregate(results: list[dict], skipped: list[dict]) -> dict:
    passed = [r for r in results if not r["failures"]]
    failed = [r for r in results if r["failures"]]
    lat = [r["latency_ms"] for r in results if r["latency_ms"] > 0]
    prompt_tok = 0
    comp_tok = 0
    for r in results:
        tel = r.get("telemetry")
        if tel:
            for call in tel.get("llm_calls", []):
                prompt_tok += call.get("prompt_tokens", 0) or 0
                comp_tok += call.get("completion_tokens", 0) or 0

    def by_dim(dim: str) -> dict:
        group = [r for r in results if r["scenario_id"].startswith(dim)]
        if not group:
            return {"n": 0, "passed": 0, "rate": None}
        ok = sum(1 for r in group if not r["failures"])
        return {"n": len(group), "passed": ok, "rate": round(ok / len(group), 3)}

    def by_intent(intent: str) -> dict:
        group = [r for r in results if r["expect_intent"] == intent]
        if not group:
            return {"n": 0, "passed": 0, "rate": None}
        ok = sum(1 for r in group if not r["failures"])
        return {"n": len(group), "passed": ok, "rate": round(ok / len(group), 3)}

    cost = (prompt_tok / 1e6 * _PRICE["input_yuan_per_1m"]) + (comp_tok / 1e6 * _PRICE["output_yuan_per_1m"])

    return {
        "total": len(results),
        "passed": len(passed),
        "failed": len(failed),
        "skipped": skipped,
        "pass_rate": round(len(passed) / len(results), 3) if results else None,
        "by_dimension": {d: by_dim(d) for d in "ABCDE"},
        "by_intent": {i: by_intent(i) for i in
                      ["chat", "fetch", "explore", "discuss", "realtime", "profile", "fallback"]},
        "latency": {"p50_ms": round(statistics.median(lat), 1) if lat else None,
                    "p95_ms": round(sorted(lat)[int(len(lat) * 0.95) - 1], 1) if len(lat) >= 20 else None},
        "tokens": {"prompt": prompt_tok, "completion": comp_tok},
        "cost_yuan_estimate": round(cost, 4),
        "failures": failed,
    }


def _print_summary(agg: dict) -> None:
    print("\n" + "=" * 60)
    print(f"  E2E 场景评测 — total={agg['total']} passed={agg['passed']} failed={agg['failed']}"
          f" rate={agg['pass_rate']}")
    print("=" * 60)
    print("  分维度通过率:")
    for dim, v in agg["by_dimension"].items():
        print(f"    {dim}: {v['passed']}/{v['n']} ({v['rate']})")
    print(f"  延迟 p50={agg['latency']['p50_ms']}ms p95={agg['latency']['p95_ms']}ms")
    print(f"  tokens: prompt={agg['tokens']['prompt']} completion={agg['tokens']['completion']}"
          f" 成本≈¥{agg['cost_yuan_estimate']}")
    if agg["failures"]:
        print(f"\n  失败场景 ({len(agg['failures'])} 条):")
        for f in agg["failures"]:
            print(f"    ✗ {f['scenario_id']}: {f['failures'][:3]}")


def _save(agg: dict, data_path: Path) -> tuple[Path, Path]:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = f"graph_smoke-{stamp}-{_git_hash()}"
    json_path = RESULTS_DIR / f"{name}.json"
    md_path = RESULTS_DIR / f"{name}.md"
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(agg, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [f"# 图接线冒烟报告", f"\n**时间**: {stamp} | **git**: {_git_hash()} | **数据**: {data_path.name}",
             f"\n> v0 限制：Bangumi API 真实调用、LLM 非确定（看趋势）、requires_token 场景可能跳过",
             f"\n## 总览\n", f"| 指标 | 值 |", f"|---|---|",
             f"| 通过率 | {agg['pass_rate']} ({agg['passed']}/{agg['total']}) |",
             f"| 延迟 p50 / p95 | {agg['latency']['p50_ms']}ms / {agg['latency']['p95_ms']}ms |",
             f"| 成本估算 | ¥{agg['cost_yuan_estimate']} |",
             f"\n## 分维度\n", f"| 维度 | 通过 | 率 |", f"|---|---|---|"]
    for dim, v in agg["by_dimension"].items():
        lines.append(f"| {dim} | {v['passed']}/{v['n']} | {v['rate']} |")
    lines += [f"\n## 分意图\n", f"| intent | 通过 | 率 |", f"|---|---|---|"]
    for i, v in agg["by_intent"].items():
        lines.append(f"| {i} | {v['passed']}/{v['n']} | {v['rate']} |")
    if agg["failures"]:
        lines += [f"\n## 失败明细\n"]
        for f in agg["failures"]:
            lines.append(f"- **{f['scenario_id']}** (expect={f['expect_intent']}, got={f['query_intent']}, wc={len(f.get('reply') or '')})")
            for reason in f["failures"]:
                lines.append(f"  - {reason}")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path


def main() -> None:
    parser = argparse.ArgumentParser(description="端到端场景评测 runner v0")
    parser.add_argument("--data", default="eval/data/e2e_scenarios.json")
    parser.add_argument("--smoke", type=int, default=0, help="只跑前 N 条（0=全部）")
    args = parser.parse_args()

    data_path = Path(args.data)
    data = json.loads(data_path.read_text(encoding="utf-8"))
    scenarios = data["scenarios"]
    print(f"加载 {len(scenarios)} 条场景 (smoke={args.smoke or 'all'})")

    # 抑制 sqlalchemy INFO 日志，只留评估输出
    import logging
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    agg = asyncio.run(run(scenarios, smoke=args.smoke))
    _print_summary(agg)
    json_path, md_path = _save(agg, data_path)
    print(f"\n已归档: {json_path}")
    print(f"报告:   {md_path}")


if __name__ == "__main__":
    main()