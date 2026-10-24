"""Run the reviewed unit-test set with external network and PostgreSQL blocked.

Usage: TIKTOKEN_CACHE_DIR=/path/to/prepared/cache python scripts/test_offline.py
The tokenizer cache must be prepared separately; this command never downloads it.
"""

import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
sys.dont_write_bytecode = True
os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"


class ExternalAccessBlocked(BaseException):
    """Do not let production fallback handlers hide a missing test mock."""


attempts = []


def blocked(*args, **kwargs):
    attempts.append(True)
    raise ExternalAccessBlocked("Offline tests attempted external I/O; add a mock")


original_connect = socket.socket.connect
original_connect_ex = socket.socket.connect_ex


def connect(sock, address):
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        blocked()
    return original_connect(sock, address)


def connect_ex(sock, address):
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        blocked()
    return original_connect_ex(sock, address)


socket.socket.connect = connect
socket.socket.connect_ex = connect_ex
socket.getaddrinfo = blocked

import psycopg2

psycopg2.connect = blocked

import tiktoken

try:
    tiktoken.get_encoding("cl100k_base")
except ExternalAccessBlocked:
    print("Tokenizer cache missing. Prepare cl100k_base separately and set TIKTOKEN_CACHE_DIR.")
    sys.exit(2)
except Exception:  # noqa: BLE001 - tokenizer backends raise heterogeneous errors
    print("Tokenizer cache missing. Prepare cl100k_base separately and set TIKTOKEN_CACHE_DIR.")
    sys.exit(2)

# Never load the developer's .env or inherited provider credentials.
from core.config import Settings, get_settings

Settings.model_config["env_file"] = None
for name in ("LLM_API_KEY", "OPENAI_API_KEY", "AZURE_OPENAI_API_KEY", "ZHIPU_API_KEY"):
    os.environ[name] = "offline-test-placeholder"
for name in ("LLM_BASE_URL", "LLM_AZURE_ENDPOINT", "AZURE_OPENAI_ENDPOINT",
             "BANGUMI_ACCESS_TOKEN", "BANGUMI_APP_SECRET"):
    os.environ[name] = ""
os.environ.update(DATABASE_URL="postgresql://offline:offline@127.0.0.1:1/offline",
                  AUTH_ENABLED="false", API_USER_TOKENS="{}", ENVIRONMENT="test",
                  MEMORY_ENABLED="false", DEV_MODE="false")
get_settings.cache_clear()

import pytest

files = [
    "test_p0_contracts", "test_client", "test_endpoint", "test_rate_limit",
    "test_render_fallback", "test_render_degrade_outcome", "test_routes",
    "test_memory", "test_phase5_l1", "test_compression_shapes",
    "test_bgm_tools", "test_sanitizers", "test_schemas", "test_state",
    "test_reasoning", "test_devtools_tool_telemetry",
]
status = pytest.main(["-q", "-p", "asyncio", "-p", "no:cacheprovider",
                      *[f"test/{name}.py" for name in files]])
if attempts:
    print(f"Offline isolation failed: {len(attempts)} external I/O attempt(s).")
sys.exit(1 if attempts else status)
