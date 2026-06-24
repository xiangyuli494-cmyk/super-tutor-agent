"""Super Tutor — FastAPI application entry point.

启动多角色智能教学系统后端服务。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from super_tutor import __version__
from super_tutor.core.exceptions import TutorError
from super_tutor.core.limiter import limiter
from super_tutor.routes import (
    dashboard_router,
    materials_router,
    quizzes_router,
    tokens_router,
)
from super_tutor.routes.dependencies import init_app_state, shutdown_app_state

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("super_tutor.main")

# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期管理。

    - 启动时：初始化 Database、LLMClient、RoleManager，挂载到 app.state。
    - 关闭时：关闭数据库连接。
    """
    logger.info("Super Tutor v%s starting up...", __version__)

    try:
        await init_app_state(app.state)
        logger.info("All services initialized. Ready to accept requests.")
    except Exception as exc:
        logger.critical("Failed to initialize services: %s", exc, exc_info=True)
        raise

    yield

    logger.info("Super Tutor shutting down...")
    await shutdown_app_state(app.state)
    logger.info("Shutdown complete.")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Super Tutor",
    description="多角色智能教学系统 — 扔给它一本 PDF，它读、它出题、它批改、它排复习计划。",
    version=__version__,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# ---------------------------------------------------------------------------
# Rate Limiter — attach to app state + exception handler
# ---------------------------------------------------------------------------
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Exception handlers — 将业务异常转换为标准 APIResponse
# ---------------------------------------------------------------------------


@app.exception_handler(TutorError)
async def tutor_error_handler(request: Request, exc: TutorError) -> JSONResponse:
    """捕获所有 TutorError 子类，返回标准错误格式。"""
    logger.warning("TutorError handled: %s", exc)
    return JSONResponse(
        status_code=400,
        content={
            "code": 400,
            "message": "请求处理失败",
            "detail": str(exc),
        },
    )


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
    """参数校验错误。"""
    return JSONResponse(
        status_code=422,
        content={
            "code": 422,
            "message": "参数校验失败",
            "detail": str(exc),
        },
    )


@app.exception_handler(Exception)
async def generic_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底异常处理。"""
    logger.exception("Unhandled exception: %s", exc)
    return JSONResponse(
        status_code=500,
        content={
            "code": 500,
            "message": "服务器内部错误",
            "detail": "请稍后重试或联系管理员。",
        },
    )


# ---------------------------------------------------------------------------
# Health check (always available)
# ---------------------------------------------------------------------------


@app.get("/api/v1/health")
async def health_check(request: Request) -> dict:
    """健康检查端点（含 prompt 版本信息）。"""
    prompt_versions: dict = {}
    try:
        roles = getattr(request.app.state, "tutor_role_manager", None)
        if roles is not None and hasattr(roles, "get_all_versions"):
            prompt_versions = roles.get_all_versions()
    except Exception:
        pass
    return {
        "code": 0,
        "message": "ok",
        "data": {
            "version": __version__,
            "prompt_versions": prompt_versions,
        },
    }


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

app.include_router(materials_router)
app.include_router(quizzes_router)
app.include_router(dashboard_router)
app.include_router(tokens_router)


# ---------------------------------------------------------------------------
# CLI launcher
# ---------------------------------------------------------------------------


def main() -> None:
    """命令行启动入口。"""
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(
        description="Super Tutor — 多角色智能教学系统"
    )
    parser.add_argument(
        "--port", type=int, default=8765, help="服务端口（默认 8765）"
    )
    parser.add_argument(
        "--host", type=str, default="127.0.0.1", help="绑定地址（默认 127.0.0.1）"
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        default=False,
        help="启用热重载（开发模式）",
    )
    args = parser.parse_args()

    uvicorn.run(
        "super_tutor.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
