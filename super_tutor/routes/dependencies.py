"""Super Tutor — FastAPI 依赖注入。

通过 ``Depends()`` 向路由函数注入共享资源（Database、LLMClient 等）。
所有重型对象在 FastAPI ``lifespan`` 中初始化一次，挂载到 ``app.state`` 上，
路由通过本模块的 ``use_*`` 函数按需获取。

使用方式::

    from fastapi import Depends
    from super_tutor.routes.dependencies import use_db

    @router.post("/upload")
    async def upload(req: Request, db = Depends(use_db)):
        ...
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import Request
from starlette.datastructures import State as AppState

from super_tutor.config import TutorConfig
from super_tutor.core.database import Database
from super_tutor.core.llm_client import LLMClient
from super_tutor.core.orchestrator import Orchestrator
from super_tutor.core.role_manager import RoleManager
from super_tutor.core.token_tracker import TokenTracker

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Orchestrator Registry — 线程安全的会话注册表
# ---------------------------------------------------------------------------


class OrchestratorRegistry:
    """异步安全的 Orchestrator 会话注册表。

    使用 ``asyncio.Lock`` 保护写操作，读操作（``get``）利用
    CPython GIL 下的 ``dict.get`` 原子性，无需加锁。
    """

    def __init__(self) -> None:
        self._data: dict[str, "Orchestrator"] = {}
        self._lock: "asyncio.Lock" = None  # type: ignore[assignment]

    def _ensure_lock(self) -> "asyncio.Lock":
        """懒初始化锁（避免在非事件循环上下文中创建）。"""
        import asyncio

        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def get(self, session_id: str) -> "Orchestrator | None":
        """无锁读取（dict.get 在 CPython 中是原子操作）。"""
        return self._data.get(session_id)

    def get_sync(self, session_id: str) -> "Orchestrator | None":
        """``get()`` 的别名，向后兼容。"""
        return self.get(session_id)

    async def set(self, session_id: str, orch: "Orchestrator") -> None:
        """加锁写入。"""
        lock = self._ensure_lock()
        async with lock:
            self._data[session_id] = orch

    async def remove(self, session_id: str) -> None:
        """加锁删除。"""
        lock = self._ensure_lock()
        async with lock:
            self._data.pop(session_id, None)

    def __setitem__(self, session_id: str, orch: "Orchestrator") -> None:
        """直接赋值（非异步，用于简化创建流程）。

        注意：此方法不加锁。在 FastAPI 的请求处理中，
        每个请求在独立的协程中运行，但协程是协作式调度的，
        因此简单的赋值在无 await 点时是安全的。
        """
        self._data[session_id] = orch

    def __contains__(self, session_id: str) -> bool:
        return session_id in self._data


# ---------------------------------------------------------------------------
# State keys — the attribute names stored on ``app.state`` at startup.
# ---------------------------------------------------------------------------
_S_CONFIG: str = "tutor_config"
_S_DB: str = "tutor_database"
_S_LLM: str = "tutor_llm_client"
_S_ROLES: str = "tutor_role_manager"
_S_ORCH_REGISTRY: str = "tutor_orchestrator_registry"
_S_TOKEN_TRACKER: str = "tutor_token_tracker"


# ===================================================================
# Lifespan helpers — called from main.py lifespan
# ===================================================================


async def init_app_state(app_state: AppState) -> None:
    """Initialize all shared resources and attach them to ``app.state``.

    Called once during FastAPI startup (inside the ``lifespan`` context
    manager).  After this returns, every route can ``Depends(use_db)`` etc.
    """
    # -- Config ---------------------------------------------------------
    config = TutorConfig.get_instance()
    setattr(app_state, _S_CONFIG, config)

    # -- Database -------------------------------------------------------
    db_path = _resolve_db_path()
    database = Database(db_path=db_path, config=config)
    await database.initialize()
    setattr(app_state, _S_DB, database)
    logger.info("Database ready: %s", db_path)

    # -- LLM Client -----------------------------------------------------
    try:
        llm_client = LLMClient(config=config, project_root=None, cli_mode=False)
        setattr(app_state, _S_LLM, llm_client)
        logger.info("LLM client ready.")
    except Exception as exc:
        logger.warning(
            "LLM client unavailable (API key not configured?): %s. "
            "LLM-dependent endpoints will return 503.",
            exc,
        )
        setattr(app_state, _S_LLM, None)

    # -- Role Manager ---------------------------------------------------
    prompts_dir = _resolve_prompts_dir()
    role_manager = RoleManager(prompts_dir=str(prompts_dir))
    setattr(app_state, _S_ROLES, role_manager)

    # -- Token Tracker ---------------------------------------------------
    token_tracker = TokenTracker(
        database=database,
        budget=config.token_budget_default,
    )
    setattr(app_state, _S_TOKEN_TRACKER, token_tracker)
    logger.info("Token tracker ready (budget=%d).", config.token_budget_default)

    # -- Orchestrator Registry ------------------------------------------
    setattr(app_state, _S_ORCH_REGISTRY, OrchestratorRegistry())


async def shutdown_app_state(app_state: AppState) -> None:
    """Clean up shared resources on shutdown."""
    db: Optional[Database] = getattr(app_state, _S_DB, None)
    if db is not None:
        await db.close()
        logger.info("Database connection closed.")


# ===================================================================
# Dependency providers — use with ``fastapi.Depends(use_xxx)``
# ===================================================================


def use_config(request: Request) -> TutorConfig:
    """回传单例 TutorConfig。"""
    return getattr(request.app.state, _S_CONFIG)


def use_db(request: Request) -> Database:
    """回传已初始化的 Database 实例。"""
    return getattr(request.app.state, _S_DB)


def use_llm_client(request: Request) -> LLMClient:
    """回传 LLMClient 实例。

    Raises:
        HTTPException(503): 若 API Key 未配置导致 LLM 不可用。
    """
    from fastapi import HTTPException

    client = getattr(request.app.state, _S_LLM, None)
    if client is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "LLM 服务不可用：API Key 未配置。"
                "请在 ~/.super-tutor/settings.json 中设置 deepseek_api_key，"
                "或设置环境变量 TUTOR_API_KEY。"
            ),
        )
    return client


def use_role_manager(request: Request) -> RoleManager:
    """回传 RoleManager 实例。"""
    return getattr(request.app.state, _S_ROLES)


def use_orchestrator_registry(request: Request) -> OrchestratorRegistry:
    """回传 Orchestrator 会话注册表。"""
    return getattr(request.app.state, _S_ORCH_REGISTRY)


def use_token_tracker(request: Request) -> TokenTracker:
    """回传 TokenTracker 实例。"""
    return getattr(request.app.state, _S_TOKEN_TRACKER)


def build_orchestrator(
    db: Database,
    llm_client: LLMClient,
    role_manager: RoleManager,
    token_tracker: TokenTracker | None = None,
) -> Orchestrator:
    """工厂函数：构造一个新的 Orchestrator 实例。

    每个测验会话需要一个独立的 Orchestrator。
    可选注入 TokenTracker 用于预算管控和用量统计。
    """
    orch = Orchestrator(
        database=db,
        llm_client=llm_client,
        role_manager=role_manager,
    )
    if token_tracker is not None:
        orch.inject_token_tracker(token_tracker)
    return orch


# ===================================================================
# Internal helpers
# ===================================================================


def _resolve_db_path() -> str:
    """Determine the default SQLite database path.

    Priority:
    1. ``TUTOR_DB_PATH`` environment variable.
    2. ``~/.super-tutor/super_tutor.db`` in the user's home directory.
    """
    env_path = os.getenv("TUTOR_DB_PATH")
    if env_path:
        return str(Path(env_path).expanduser().resolve())

    path = Path.home() / ".super-tutor" / "super_tutor.db"
    # Ensure parent directory exists (Database.__init__ requires it).
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def _resolve_prompts_dir() -> Path:
    """Resolve the role system prompts directory.

    Returns the ``super_tutor/prompts/`` directory relative to this package.
    """
    # Resolve from this file's location: routes/dependencies.py → ../../super_tutor/prompts
    this_dir = Path(__file__).resolve().parent  # routes/
    package_dir = this_dir.parent                # super_tutor/
    prompts_dir = package_dir / "prompts"
    if not prompts_dir.is_dir():
        raise FileNotFoundError(
            f"角色提示词目录不存在: {prompts_dir}\n"
            "Expected layout: super_tutor/prompts/{tutor,assistant,evaluator}.md"
        )
    return prompts_dir
