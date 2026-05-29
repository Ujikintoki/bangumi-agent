"""
全局配置管理模块

使用 Pydantic v2 的 BaseSettings 从环境变量和 .env 文件中读取配置。
通过 get_settings() 函数缓存单例实例，避免重复解析。
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用全局配置。

    从环境变量或 .env 文件中加载配置项。
    BANGUMI_APP_ID 和 BANGUMI_APP_SECRET 为必填项，缺失将导致启动失败。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
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

    # ── Bangumi OAuth 凭证（必填） ────────────────────────────
    BANGUMI_APP_ID: str
    """Bangumi 应用的 Client ID(必填)。"""

    BANGUMI_APP_SECRET: str
    """Bangumi 应用的 Client Secret(必填)。"""


@lru_cache
def get_settings() -> Settings:
    """返回全局唯一的 Settings 实例。

    使用 @lru_cache 保证单例模式，首次调用后结果会被缓存，
    后续调用直接返回缓存实例，避免重复读取 .env 文件。

    Returns:
        Settings: 应用全局配置实例。
    """
    return Settings()
