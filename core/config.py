"""
全局配置管理模块

使用 Pydantic v2 的 BaseSettings 从环境变量和 .env 文件中读取配置。
通过 get_settings() 函数缓存单例实例，避免重复解析。
"""

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# 无论从哪个目录运行，始终指向项目根目录的 .env
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    """应用全局配置。

    从环境变量或 .env 文件中加载配置项。
    """

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── 通用配置 ──────────────────────────────────────────────
    PROJECT_NAME: str = "BGM Agent"
    """项目名称，用于日志、API 文档标题等场景。"""

    VERSION: str = "0.1.0"
    """当前版本号。"""

    ENVIRONMENT: str = "development"
    """运行环境标识，可选值如 development / staging / production。"""

    # ── 数据库配置 ────────────────────────────────────────────
    DATABASE_URL: str = "postgresql://myuser:mypassword@localhost:5432/bangumidb"
    """PostgreSQL 数据库连接 URL，默认连接本地 bangumidb 库。"""

    # ── Bangumi OAuth 凭证 ────────────────────────────────────
    BANGUMI_APP_ID: str = ""
    """Bangumi 应用的 Client ID，调用 Bangumi API 时必填。"""

    BANGUMI_APP_SECRET: str = ""
    """Bangumi 应用的 Client Secret，调用 Bangumi API 时必填。"""

    BANGUMI_ACCESS_TOKEN: str = ""
    """Bangumi Bearer Access Token，用于 p1 private API 认证。

    可通过环境变量 BANGUMI_ACCESS_TOKEN 或 .env 文件注入。
    部分工具（如用户时光机、日志）需要有效 Token 才能调用。
    """

    # ── LLM 通用配置 ──────────────────────────────────────────
    LLM_API_KEY: str = Field(
        default="",
        validation_alias=AliasChoices(
            "LLM_API_KEY", "AZURE_OPENAI_API_KEY", "OPENAI_API_KEY"
        ),
    )
    """LLM API 密钥。

    支持以下环境变量名（优先级从左到右）：
    LLM_API_KEY → AZURE_OPENAI_API_KEY → OPENAI_API_KEY
    """

    LLM_MODEL: str = Field(
        default="gpt-4o",
        validation_alias=AliasChoices("LLM_MODEL", "AZURE_OPENAI_CHAT_DEPLOYMENT"),
    )
    """LLM 模型名称。Azure 模式下为部署名（deployment name）；其他模式下为模型名。

    常用值：gpt-4o, gpt-4o-mini, deepseek-chat, qwen-plus。
    """

    LLM_BASE_URL: str = ""
    """自定义 API Base URL（DeepSeek、Qwen 等 OpenAI 兼容 API）。

    示例：https://api.deepseek.com/v1, https://dashscope.aliyuncs.com/compatible-mode/v1。
    Azure 用户应使用 LLM_AZURE_ENDPOINT 而非此字段。
    """

    LLM_TEMPERATURE: float = 0.3
    """LLM 温度参数。工具调用场景建议 0.1-0.3，创意生成可调至 0.7+。"""

    LLM_MAX_TOKENS: int = 4096
    """LLM 单次输出最大 Token 数。"""

    # ── LLM — Azure 专用 ───────────────────────────────────────
    LLM_AZURE_ENDPOINT: str = Field(
        default="",
        validation_alias=AliasChoices("LLM_AZURE_ENDPOINT", "AZURE_OPENAI_ENDPOINT"),
    )
    """Azure OpenAI 端点 URL。

    示例：https://<resource>.openai.azure.com。
    """

    LLM_AZURE_API_VERSION: str = Field(
        default="2024-10-21",
        validation_alias=AliasChoices("LLM_AZURE_API_VERSION", "AZURE_OPENAI_API_VERSION"),
    )
    """Azure OpenAI API 版本。"""

    # ── LLM — Critic 可选模型 ─────────────────────────────────
    LLM_CRITIC_MODEL: str = ""
    """Critic 节点专用模型（可选）。留空则默认使用 LLM_MODEL。

    允许为 Critic 使用更便宜的小模型以降低评估成本。
    """

    # ── Critic 模式 ───────────────────────────────────────────
    CRITIC_MODE: str = "rule"
    """Critic 评估模式：``"rule"``（零 Token 规则版，默认）或 ``"llm"``（LLM 定向反馈）。

    推荐先用规则版验证流程，确认 ReAct 循环和 feedback 注入机制正确后，
    切换到 LLM 版获得更精准的评估。"""
    """Critic 节点专用模型（可选）。留空则默认使用 LLM_MODEL。

    允许为 Critic 使用更便宜的小模型（如 gpt-4o-mini）以降低评估成本。
    """

    # ── 智谱 AI 配置 ──────────────────────────────────────────
    ZHIPU_API_KEY: str = ""
    """智谱 API 密钥，用于调用 embedding-3 等模型生成向量嵌入。

    可通过环境变量 ZHIPU_API_KEY 或 .env 文件注入。
    在尚未缴费的开发阶段可留空，此时 embedding 功能不可用。
    """

    ZHIPU_BASE_URL: str = "https://open.bigmodel.cn/api/paas/v4"
    """智谱 API 基础 URL，默认使用官方地址。"""

    # ── Embedding 模型配置 ────────────────────────────────────
    EMBEDDING_MODEL: str = "embedding-3"
    """embedding 模型编码，默认智谱 embedding-3 (2048 维)。

    常用备选: OpenAI text-embedding-3-small (1536 维) 或
    text-embedding-3-large (3072 维)。切换模型时需同步修改
    EMBEDDING_DIMENSION 以匹配 pgvector 列定义。
    """

    EMBEDDING_DIMENSION: int = 2048
    """embedding 向量维度，必须与 EMBEDDING_MODEL 的实际输出一致。

    智谱 embedding-3 → 2048, OpenAI ada-002 / 3-small → 1536,
    OpenAI 3-large → 3072。
    """


@lru_cache
def get_settings() -> Settings:
    """返回全局唯一的 Settings 实例。

    使用 @lru_cache 保证单例模式，首次调用后结果会被缓存，
    后续调用直接返回缓存实例，避免重复读取 .env 文件。

    Returns:
        Settings: 应用全局配置实例。
    """
    return Settings()
