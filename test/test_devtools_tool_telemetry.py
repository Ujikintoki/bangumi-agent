from __future__ import annotations

import pytest
from langchain_core.tools import tool

from agent.devtools import RequestTelemetry, instrument_tools, set_current_telemetry


@tool
async def _private_lookup(keyword: str, token: str) -> dict:
    """Test lookup."""
    return {"_error": "provider body must not be recorded"}


@pytest.mark.asyncio
async def test_tool_telemetry_is_timed_and_redacted():
    telemetry = RequestTelemetry()
    set_current_telemetry(telemetry)
    try:
        result = await instrument_tools([_private_lookup])[0].ainvoke({
            "keyword": "作品名",
            "token": "top-secret",
        })
    finally:
        set_current_telemetry(None)

    assert "_error" in result
    record = telemetry.to_dict()["tool_calls"][0]
    assert record["tool"] == "_private_lookup"
    assert record["status"] == "failed"
    assert record["argument_keys"] == ["keyword", "token"]
    assert record["elapsed_ms"] >= 0
    assert "top-secret" not in str(record)
    assert "provider body" not in str(record)
