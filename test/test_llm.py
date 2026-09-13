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

    @pytest.mark.parametrize(
        "kwargs",
        [
            {},                                              # 默认
            {"temperature": 0.4, "_telemetry_label": "render"},            # render
            {"temperature": 0, "max_tokens": 200, "request_timeout": 10},   # 分类器
            {"temperature": 0, "max_tokens": 500, "request_timeout": 10},   # L2 记忆摘要
        ],
    )
    def test_thinking_is_always_disabled(self, kwargs):
        """全模型关思考是项目决定（2026-09-13）—— 调用点不该能"忘掉"它。

        实测 `LLM_MODEL`（deepseek-v4-flash）**不传该参数时默认思考**：同一道平凡题，
        不传 = 输出 54 tok / 思考 84 字符，显式 disabled = 输出 1 tok / 思考 0 字符。
        这条测试按生产里真实的调用形态参数化，覆盖曾经漏掉的 render / 分类器 /
        L2 记忆摘要 —— 它们当时都没传，等于一直在花钱思考。
        """
        assert create_llm(**kwargs).extra_body == {"thinking": {"type": "disabled"}}

    def test_disabled_thinking_dict_is_not_shared(self):
        """预设要浅拷贝：多个 LLM 实例不该共用同一个 extra_body dict。"""
        assert create_llm().extra_body is not create_llm().extra_body

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
