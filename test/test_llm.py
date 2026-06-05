"""
LLM Factory 测试

验证多 Provider 初始化、API Key 解析优先级。
可独立运行: python -m pytest test/test_llm.py -v
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from langchain_openai import ChatOpenAI

from agent.llm import _resolve_api_key, create_llm
from core.config import Settings


class TestCreateLLM:
    """LLM 工厂"""

    def test_creates_llm_with_defaults(self):
        llm = create_llm()
        assert hasattr(llm, "invoke")

    def test_creates_llm_with_custom_params(self):
        llm = create_llm(temperature=0.7, max_tokens=512)
        assert hasattr(llm, "invoke")

    @patch.dict("os.environ", {}, clear=True)
    def test_resolve_api_key_raises_when_all_empty(self):
        with pytest.raises(ValueError, match="未找到 LLM API Key"):
            _resolve_api_key(Settings(_env_file=None))

    @patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test-env"}, clear=True)
    def test_resolve_api_key_from_openai_env(self):
        assert _resolve_api_key(Settings(_env_file=None)) == "sk-test-env"

    @patch.dict("os.environ", {"AZURE_OPENAI_API_KEY": "azure-key-env"}, clear=True)
    def test_resolve_api_key_from_azure_env(self):
        assert _resolve_api_key(Settings(_env_file=None)) == "azure-key-env"
