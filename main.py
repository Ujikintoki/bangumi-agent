"""
FastAPI 应用启动入口

初始化 FastAPI 实例，配置 CORS 中间件与生命周期事件，
并提供基础健康检查端点。
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.config import get_settings

logger = logging.getLogger("bgm-agent")
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """管理应用的生命周期。

    替代旧的 startup/shutdown 事件装饰器，在应用启动和关闭时
    执行必要的初始化和清理工作。
    """
    logger.info("🚀 系统启动 — %s v%s", settings.PROJECT_NAME, settings.VERSION)
    print(f"[lifespan] {settings.PROJECT_NAME} v{settings.VERSION} 启动成功")
    yield
    logger.info("🛑 系统关闭 — %s v%s", settings.PROJECT_NAME, settings.VERSION)
    print(f"[lifespan] {settings.PROJECT_NAME} v{settings.VERSION} 已关闭")


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    lifespan=lifespan,
)

# ── CORS 中间件（允许所有来源，方便本地调试） ──────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health_check() -> dict:
    """基础健康检查端点。

    Returns:
        dict: 包含系统状态、运行环境和版本号的状态信息。
    """
    return {
        "status": "ok",
        "environment": settings.ENVIRONMENT,
        "version": settings.VERSION,
    }
