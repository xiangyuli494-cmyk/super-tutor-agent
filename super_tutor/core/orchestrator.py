"""Workflow orchestrator — Super Tutor 教学流水线引擎。

实现状态机驱动的三 AI 角色协作流水线：

    IDLE → PARSING → QUIZ_GEN → EVALUATING → PLANNING → DONE

三个 AI 角色各司其职：

* **Tutor**（主导师）— 解析 PDF 资料 + 制定学习计划
* **Assistant**（助教）— 根据知识库生成题目 + 组卷
* **Evaluator**（评估者）— 批改作答 + 迷思概念诊断
"""

from __future__ import annotations

import json as _json
import logging
import re as _re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

from super_tutor.core.database import Database
from super_tutor.core.exceptions import LLMClientError, TutorError, VALID_ROLES
from super_tutor.core.llm_client import LLMClient
from super_tutor.core.role_manager import RoleManager
from super_tutor.models.enums import AIRole, PipelinePhase
from super_tutor.models.knowledge import KnowledgeChunk
from super_tutor.models.mastery import ReviewItem
from super_tutor.models.quiz import MisconceptionTag, Question, QuizAttempt

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

_MAX_STEP_RETRIES: int = 3
"""单步最大连续重试次数，超过后停留 ERROR 等待人工介入。"""

_VALID_PHASE_VALUES: set[str] = {p.value for p in PipelinePhase}
"""所有合法 PipelinePhase 枚举值，用于 DB 恢复时的校验。"""

# 各阶段使用的 LLM 算力档位
_PARSING_MODEL_TIER: str = "heavy"       # PDF 解析需要强理解能力
_QUIZ_GEN_MODEL_TIER: str = "heavy"      # 出题需要高质量输出
_EVALUATING_MODEL_TIER: str = "medium"    # 批改中等算力即可
_PLANNING_MODEL_TIER: str = "medium"      # 排期计算中等算力即可

# AIRole → RoleManager 文件名 的映射
_ROLE_TO_PROMPT_FILE: dict[AIRole, str] = {
    AIRole.TUTOR: "tutor",
    AIRole.ASSISTANT: "assistant",
    AIRole.EVALUATOR: "evaluator",
}


# ---------------------------------------------------------------------------
# 自定义异常
# ---------------------------------------------------------------------------


class OrchestratorError(TutorError):
    """Orchestrator 层错误。"""


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class Orchestrator:
    """教学流水线状态机编排器。

    协调三个 AI 角色按序推进：

    1. **PARSING** — Tutor 解析 PDF，生成知识片段和摘要。
    2. **QUIZ_GEN** — Assistant 基于知识库生成测验题目。
    3. **EVALUATING** — Evaluator 批改学生作答，诊断迷思概念。
    4. **PLANNING** — Tutor 综合掌握数据，生成 SM-2 排期计划。

    使用方式::

        orch = Orchestrator(database=db, llm_client=llm, role_manager=rm)
        await orch.initialize(session_context={"material_id": "xxx"})

        # Phase 1: PDF 解析
        await orch.start()          # IDLE → PARSING
        await orch.proceed()        # PARSING → QUIZ_GEN

        # 学生作答（断点：等待真实学生提交）
        await orch.submit_answers([
            {"question_id": "q1", "student_answer": "B"},
        ])

        # Phase 2-4: 批改 → 排期 → 完成
        await orch.proceed()        # QUIZ_GEN → EVALUATING
        await orch.proceed()        # EVALUATING → PLANNING
        await orch.proceed()        # PLANNING → DONE

        status = await orch.get_status()

    Attributes:
        state: 当前工作流状态（只读属性）。
        session_id: 当前教学会话标识。
    """

    def __init__(
        self,
        database: Database,
        llm_client: LLMClient,
        role_manager: RoleManager,
    ) -> None:
        """初始化编排器。

        Args:
            database: 数据库管理器（必须已调用 ``initialize()``）。
            llm_client: 已配置的 LLM 客户端。
            role_manager: AI 角色系统提示词管理器。
        """
        self._db: Database = database
        self._llm: LLMClient = llm_client
        self._roles: RoleManager = role_manager

        # ------------------------------------------------------------------
        # 会话上下文 — 贯穿整个流水线的运行时数据
        # ------------------------------------------------------------------
        self._session_context: dict[str, Any] = {}

        # ------------------------------------------------------------------
        # 流水线阶段
        # ------------------------------------------------------------------
        self._phase: PipelinePhase = PipelinePhase.IDLE
        self._previous_phase: PipelinePhase = PipelinePhase.IDLE
        self._paused: bool = False
        self._error_message: Optional[str] = None
        self._step_retry_count: int = 0
        self._in_progress: bool = False

        # ------------------------------------------------------------------
        # 阶段产出物 — 上游阶段写入，下游阶段读取
        # ------------------------------------------------------------------
        self._artifacts: dict[str, Any] = {}

        # ------------------------------------------------------------------
        # AI 角色状态追踪
        # ------------------------------------------------------------------
        self._role_statuses: dict[str, str] = {
            role.value: "idle" for role in AIRole
        }
        self._role_tasks: dict[str, Optional[str]] = {
            role.value: None for role in AIRole
        }

        # ------------------------------------------------------------------
        # Token 追踪器 — 可选注入，不注入时走 DB 直写（回退路径）
        # ------------------------------------------------------------------
        self._token_tracker: Optional[Any] = None

    # ==================================================================
    # Properties
    # ==================================================================

    @property
    def state(self) -> PipelinePhase:
        """返回当前流水线阶段。"""
        return self._phase

    @property
    def is_done(self) -> bool:
        """流水线是否已完成全部阶段。"""
        return (
            self._phase == PipelinePhase.PLANNING
            and not self._paused
            and self._error_message is None
        )

    @property
    def session_id(self) -> Optional[str]:
        """返回当前会话 ID。"""
        return self._session_context.get("session_id")

    def inject_token_tracker(self, tracker: Any) -> None:
        """注入 TokenTracker 用于预算管控和用量统计。

        注入后在 ``_invoke_role`` 中自动使用 tracker 记录用量
        并在每次 LLM 调用前检查预算。不注入则回退到 DB 直写。

        Args:
            tracker: :class:`TokenTracker` 实例。
        """
        self._token_tracker = tracker
        logger.debug("TokenTracker injected into Orchestrator.")

    # ==================================================================
    # 初始化
    # ==================================================================

    async def initialize(self, session_context: dict[str, Any]) -> None:
        """设置流水线的运行时上下文。

        必须在 ``start()`` 之前调用。

        Args:
            session_context: 会话上下文，至少包含：
                - ``material_id``: 学习材料 ID
                - ``session_id``: 可选会话标识
                - ``student_id``: 可选学生标识
        """
        self._session_context = session_context
        logger.info(
            "Orchestrator session context set: material=%s",
            session_context.get("material_id"),
        )

    # ==================================================================
    # 状态机控制（公开 API）
    # ==================================================================

    async def start(self) -> None:
        """启动流水线：IDLE → PARSING。

        由 Parser 角色（Tutor）解析 PDF 材料，生成知识片段。

        状态机采用 **DB 作为唯一真相源** 模式：
        ① 读取 DB 确认当前状态
        ② 修改内存状态
        ③ 写入 DB（崩溃恢复边界）
        ④ 执行 Agent

        Raises:
            OrchestratorError: 若不是从 IDLE 阶段调用。
        """
        if self._phase != PipelinePhase.IDLE:
            raise OrchestratorError(
                f"无法从 '{self._phase.value}' 启动，"
                f"需要 '{PipelinePhase.IDLE.value}' 阶段。"
            )
        if self._paused:
            raise OrchestratorError("流水线已暂停，请先调用 resume()。")

        # ① 以 DB 为唯一真相源，确认当前状态
        if self.session_id:
            db_state = await self._db.load_session(self.session_id)
            if db_state is not None:
                db_phase_raw = db_state.get("state", db_state.get("phase", "idle"))
                if db_phase_raw != PipelinePhase.IDLE.value:
                    self._phase = PipelinePhase(db_phase_raw)
                    raise OrchestratorError(
                        f"会话已在 '{db_phase_raw}' 阶段，无法从 IDLE 重新启动。"
                        f"请通过 GET /sessions/{self.session_id}/questions 自动恢复。"
                    )

        # ② 修改状态（内存）
        self._previous_phase = self._phase
        self._phase = PipelinePhase.PARSING
        self._in_progress = True
        logger.info("阶段推进: %s → %s", self._previous_phase.value, self._phase.value)

        # ③ 状态先写入 DB（崩溃恢复边界：DB 先落地，再执行 Agent）
        await self.save()

        # ④ 执行 Agent
        await self._parsing_phase()

    async def submit_answers(
        self,
        answers: list[dict[str, Any]],
        *,
        quiz_session_id: Optional[str] = None,
    ) -> int:
        """提交学生作答，作为 EVALUATING 阶段的输入。

        必须在 QUIZ_GEN 阶段完成之后调用。该方法将作答数据存入
        会话上下文供 ``_evaluating_phase`` 消费，并同时创建
        ``QuizAttempt`` Pydantic 模型实例用于后续持久化。

        Args:
            answers: 学生作答列表，每项可包含：
                - ``question_id``: 题目 ID（必填）
                - ``student_answer``: 学生提交的答案（必填）
                - ``time_spent_seconds``: 本题耗时（可选，默认 0）
                - ``hints_used``: 查看提示次数（可选，默认 0）
                - ``attempt_number``: 第几次尝试（可选，默认 1）
                - ``confidence``: 自评置信度 0-1（可选）
            quiz_session_id: 关联的 QuizSession ID（可选）。

        Returns:
            成功提交的作答条数。

        Raises:
            OrchestratorError: 若当前阶段不允许提交作答。
        """
        if self._phase != PipelinePhase.QUIZ_GEN:
            raise OrchestratorError(
                f"无法在 '{self._phase.value}' 阶段提交作答，"
                f"请先完成 QUIZ_GEN 阶段（当前需要 '{PipelinePhase.QUIZ_GEN.value}'）。"
            )

        # 存入上下文供 _evaluating_phase 使用
        self._session_context["student_answers"] = answers
        self._session_context["quiz_session_id"] = quiz_session_id

        # 反序列化为 Pydantic 模型（P1：模型脱节修复）
        questions_index: dict[str, dict[str, Any]] = {}
        for q in self._artifacts.get("questions", []):
            qid = q.get("question_id", "")
            if qid:
                questions_index[qid] = q

        attempts: list[QuizAttempt] = []
        for i, ans in enumerate(answers):
            qid = ans.get("question_id", f"unknown_{i}")
            try:
                attempt = QuizAttempt(
                    session_id=quiz_session_id or self.session_id or "unknown",
                    question_id=qid,
                    student_answer=ans.get("student_answer"),
                    time_spent_seconds=ans.get("time_spent_seconds", 0),
                    hints_used=ans.get("hints_used", 0),
                    attempt_number=ans.get("attempt_number", 1),
                    confidence=ans.get("confidence"),
                )
                attempts.append(attempt)
            except Exception as exc:
                logger.warning(
                    "QuizAttempt 构造失败 (question_id=%s): %s", qid, exc
                )

        self._artifacts["submitted_attempts"] = attempts
        logger.info(
            "提交 %d 条学生作答（%d 条成功反序列化为 QuizAttempt）。",
            len(answers),
            len(attempts),
        )
        return len(attempts)

    async def proceed(self) -> None:
        """推进流水线一步。

        状态机采用 **DB 作为唯一真相源** 模式：
        ① 读取 DB 同步当前状态（修复内存/DB 不一致）
        ② 计算下一阶段并修改内存状态
        ③ 写入 DB（崩溃恢复边界）
        ④ 执行 Agent

        Raises:
            OrchestratorError: 若从 IDLE / 暂停 / 错误 / 已完成 状态调用。
        """
        if self._paused:
            raise OrchestratorError("流水线已暂停，请先调用 resume()。")
        if self._error_message is not None:
            raise OrchestratorError(
                f"流水线处于错误状态: {self._error_message}。请先调用 retry_step()。"
            )

        # ① 以 DB 为唯一真相源，同步当前状态
        if self.session_id:
            db_state = await self._db.load_session(self.session_id)
            if db_state is not None:
                db_phase_raw = db_state.get("state", db_state.get("phase", "idle"))
                if db_phase_raw not in _VALID_PHASE_VALUES:
                    db_phase_raw = PipelinePhase.IDLE.value
                db_phase = PipelinePhase(db_phase_raw)
                if db_phase != self._phase:
                    logger.warning(
                        "内存状态 %s 与 DB 状态 %s 不一致，以 DB 为准。",
                        self._phase.value, db_phase.value,
                    )
                    self._phase = db_phase

        if self._phase == PipelinePhase.IDLE:
            raise OrchestratorError("请先调用 start() 启动流水线。")
        if self._phase == PipelinePhase.PLANNING:
            raise OrchestratorError("流水线已完成全部阶段，无需再推进。")

        # ② 计算下一阶段并修改内存状态
        _NEXT_PHASE: dict[PipelinePhase, PipelinePhase] = {
            PipelinePhase.PARSING: PipelinePhase.QUIZ_GEN,
            PipelinePhase.QUIZ_GEN: PipelinePhase.EVALUATING,
            PipelinePhase.EVALUATING: PipelinePhase.PLANNING,
        }
        next_phase = _NEXT_PHASE[self._phase]
        self._previous_phase = self._phase
        self._phase = next_phase
        self._in_progress = True
        logger.info("阶段推进: %s → %s", self._previous_phase.value, next_phase.value)

        # ③ 状态先写入 DB（崩溃恢复边界：DB 先落地，再执行 Agent）
        await self.save()

        # ④ 执行 Agent
        _PHASE_HANDLER = {
            PipelinePhase.PARSING: self._parsing_phase,
            PipelinePhase.QUIZ_GEN: self._quiz_gen_phase,
            PipelinePhase.EVALUATING: self._evaluating_phase,
            PipelinePhase.PLANNING: self._planning_phase,
        }
        handler = _PHASE_HANDLER.get(next_phase)
        if handler:
            await handler()

        # 成功后重置重试计数器
        self._step_retry_count = 0

    async def pause(self) -> None:
        """暂停流水线。

        保存当前阶段以便 ``resume()`` 恢复。已暂停时调用为幂等操作。
        """
        if self._paused:
            return
        if self._phase == PipelinePhase.IDLE:
            raise OrchestratorError("流水线尚未启动，无法暂停。")

        self._paused = True
        await self.save()
        logger.info("流水线已暂停（当前阶段: %s）。", self._phase.value)

    async def resume(self) -> None:
        """从暂停恢复。

        Raises:
            OrchestratorError: 若当前未暂停。
        """
        if not self._paused:
            raise OrchestratorError("流水线未处于暂停状态。")
        self._paused = False
        logger.info("流水线已恢复（当前阶段: %s）。", self._phase.value)

    async def save(self) -> None:
        """持久化当前编排器状态到 ``sessions`` 表。

        每个阶段完成后自动调用。仅保存原始 dict 类型的 artifacts，
        Pydantic 模型列表在 restore 时通过 ``_hydrate_models()`` 重建。
        """
        session_id = self.session_id
        if not session_id:
            logger.warning("无法保存会话：未设置 session_id")
            return

        # 仅保留原始 dict list（跳过 Pydantic model list 和纯字符串/非列表值）
        raw_artifacts: dict[str, Any] = {}
        for key, value in self._artifacts.items():
            if isinstance(value, list):
                if not value:
                    raw_artifacts[key] = value
                elif isinstance(value[0], dict):
                    raw_artifacts[key] = value
                # else: Pydantic model list — 跳过，restore 时重建

        now_iso = datetime.now(timezone.utc).isoformat()
        await self._db.save_session({
            "session_id": session_id,
            "user_id": self._session_context.get("student_id", ""),
            "state": self._phase.value,
            "previous_state": self._previous_phase.value,
            "in_progress": 1 if self._in_progress else 0,
            "error_message": self._error_message,
            "step_retry_count": self._step_retry_count,
            "session_context": self._session_context,
            "artifacts": raw_artifacts,
            "role_statuses": self._role_statuses,
            "role_tasks": {k: v for k, v in self._role_tasks.items()},
            "created_at": now_iso,
            "updated_at": now_iso,
        })
        logger.debug("会话 %s 已持久化（phase=%s）。", session_id, self._phase.value)

    # ==================================================================
    # 阶段生命周期钩子
    # ==================================================================

    async def _start_phase(
        self, phase: PipelinePhase, role: AIRole, task: str,
    ) -> None:
        """阶段开始前的角色状态更新。

        注意：状态转换（phase + in_progress）已由调用方
        ``start()`` / ``proceed()`` / ``retry_step()`` 在调用本方法前
        写入 DB。本方法仅更新运行时角色追踪状态（内存）。

        Args:
            phase: 即将执行的流水线阶段。
            role: 负责该阶段的 AI 角色。
            task: 角色当前任务的简短描述。
        """
        self._set_role_status(role, "active", task)
        logger.debug("阶段 %s 已开始（role=%s, task=%s）", phase.value, role.value, task)

    async def _end_phase(
        self, phase: PipelinePhase, role: AIRole,
    ) -> None:
        """阶段完成后的保存点。

        标记 ``_in_progress=False``，将角色状态设为空闲，
        并立即持久化。

        Args:
            phase: 刚刚完成的流水线阶段。
            role: 负责该阶段的 AI 角色。
        """
        self._in_progress = False
        self._set_role_status(role, "idle", None)
        await self.save()
        logger.debug("阶段 %s 已完成（in_progress=False）", phase.value)

    @classmethod
    async def restore(
        cls,
        session_id: str,
        *,
        database: Database,
        llm_client: LLMClient,
        role_manager: RoleManager,
    ) -> Optional["Orchestrator"]:
        """从数据库恢复一个已持久化的编排器会话。

        Args:
            session_id: 要恢复的会话 ID。
            database: 数据库实例（已初始化）。
            llm_client: LLM 客户端。
            role_manager: AI 角色管理器。

        Returns:
            恢复的 Orchestrator 实例，若 session_id 不存在则返回 ``None``。
        """
        data = await database.load_session(session_id)
        if data is None:
            return None

        orch = cls(
            database=database,
            llm_client=llm_client,
            role_manager=role_manager,
        )

        # 还原流水线阶段（向后兼容旧字段名 state/previous_state）
        raw_phase = data.get("phase", data.get("state", "idle"))
        raw_prev = data.get("previous_phase", data.get("previous_state", "idle"))
        # 旧数据中可能有 "done"/"paused"/"error" 等已移除的枚举值，回退到 idle
        if raw_phase not in _VALID_PHASE_VALUES:
            raw_phase = PipelinePhase.IDLE.value
        if raw_prev not in _VALID_PHASE_VALUES:
            raw_prev = PipelinePhase.IDLE.value
        orch._phase = PipelinePhase(raw_phase)
        orch._previous_phase = PipelinePhase(raw_prev)
        orch._paused = data.get("paused", False)
        orch._error_message = data.get("error_message")
        orch._step_retry_count = data.get("step_retry_count", 0)
        orch._in_progress = bool(data.get("in_progress", 0))
        orch._session_context = data.get("session_context", {})

        # 还原 AI 角色状态
        default_statuses = {role.value: "idle" for role in AIRole}
        orch._role_statuses = data.get("role_statuses", default_statuses)
        default_tasks = {role.value: None for role in AIRole}
        orch._role_tasks = data.get("role_tasks", default_tasks)

        # 还原原始 dict artifacts
        raw_artifacts = data.get("artifacts", {})
        orch._artifacts = dict(raw_artifacts)

        # 重新水合 Pydantic 模型列表
        material_id = orch._session_context.get("material_id", "")
        session_id_ctx = orch._session_context.get("session_id", "")
        student_id = orch._session_context.get("student_id", "")

        if "chunks" in raw_artifacts:
            orch._artifacts["chunk_models"] = _hydrate_models(
                raw_artifacts["chunks"], KnowledgeChunk,
                defaults={"material_id": material_id},
            )
        if "questions" in raw_artifacts:
            orch._artifacts["question_models"] = _hydrate_models(
                raw_artifacts["questions"], Question,
            )
        if "attempts" in raw_artifacts:
            orch._artifacts["attempt_models"] = _hydrate_models(
                raw_artifacts["attempts"], QuizAttempt,
                defaults={"session_id": session_id_ctx, "student_id": student_id},
            )
        if "misconceptions" in raw_artifacts:
            orch._artifacts["misconception_models"] = _hydrate_models(
                raw_artifacts["misconceptions"], MisconceptionTag,
            )
        if "plan_items" in raw_artifacts:
            orch._artifacts["plan_item_models"] = _hydrate_models(
                raw_artifacts["plan_items"], ReviewItem,
            )

        # -- 崩溃恢复：若上次运行时在阶段中途崩溃，回退到上一阶段 ----
        if orch._in_progress:
            logger.warning(
                "会话 %s 上次运行在 %s 阶段中途崩溃（in_progress=1），"
                "回退到 %s 等待重试。",
                session_id, orch._phase.value, orch._previous_phase.value,
            )
            orch._error_message = (
                f"上次 {orch._phase.value} 阶段执行中断（服务器重启或崩溃）。"
                f"将在下次推进时自动重试。"
            )
            orch._phase = orch._previous_phase  # 回退到上一阶段
            orch._in_progress = False  # 重置标记，让重试正常进行

        logger.info(
            "会话 %s 已恢复（phase=%s, artifacts=%d keys）。",
            session_id, orch._phase.value, len(raw_artifacts),
        )
        return orch

    async def retry_step(self) -> None:
        """重试当前失败步骤。

        状态机采用 **DB 作为唯一真相源** 模式：
        ① 清除错误、标记 in_progress、写入 DB
        ② 执行 Agent

        限制连续重试 ``_MAX_STEP_RETRIES`` 次，超过后保持错误。

        Raises:
            OrchestratorError: 若当前不在错误状态。
        """
        if self._error_message is None:
            raise OrchestratorError("流水线未处于错误状态，无需重试。")

        self._step_retry_count += 1
        if self._step_retry_count > _MAX_STEP_RETRIES:
            self._error_message = (
                f"步骤重试已达上限（{_MAX_STEP_RETRIES} 次），请人工介入。"
            )
            await self.save()  # 持久化最终失败状态
            logger.error(self._error_message)
            return

        failed_phase = self._phase
        logger.info(
            "重试步骤（%d/%d），目标阶段: %s。",
            self._step_retry_count,
            _MAX_STEP_RETRIES,
            failed_phase.value,
        )
        self._error_message = None
        self._in_progress = True

        # ① 状态先写入 DB（崩溃恢复边界：DB 先落地，再执行 Agent）
        await self.save()

        # ② 执行 Agent
        _PHASE_HANDLER = {
            PipelinePhase.PARSING: self._parsing_phase,
            PipelinePhase.QUIZ_GEN: self._quiz_gen_phase,
            PipelinePhase.EVALUATING: self._evaluating_phase,
            PipelinePhase.PLANNING: self._planning_phase,
        }
        handler = _PHASE_HANDLER.get(failed_phase)
        if handler:
            await handler()

    async def get_status(self) -> dict[str, Any]:
        """返回当前流水线阶段快照。

        Returns:
            包含阶段、暂停状态、产物摘要及各 AI 角色状态的字典。
        """
        return {
            "phase": self._phase.value,
            "paused": self._paused,
            "is_done": self.is_done,
            "session_id": self.session_id,
            "error_message": self._error_message,
            "artifacts": {
                "chunk_count": len(self._artifacts.get("chunks", [])),
                "question_count": len(self._artifacts.get("questions", [])),
                "attempt_count": len(self._artifacts.get("attempts", [])),
                "plan_item_count": len(self._artifacts.get("plan_items", [])),
            },
            "roles": {
                role.value: {
                    "status": self._role_statuses.get(role.value, "idle"),
                    "current_task": self._role_tasks.get(role.value),
                }
                for role in AIRole
            },
        }

    # ==================================================================
    # 阶段实现
    # ==================================================================

    async def _parsing_phase(self) -> None:
        """PARSING 阶段：Tutor 解析 PDF 材料。

        输入：session_context 中的 material_id → 从 DB 读取全文
        产出：KnowledgeChunk 列表（摘要形式），写入 _artifacts["chunks"]
        下一状态：QUIZ_GEN
        """
        role = AIRole.TUTOR
        await self._start_phase(PipelinePhase.PARSING, role, "解析 PDF 材料，生成知识片段")

        material_id = self._session_context.get("material_id", "unknown")

        # -- 从数据库读取材料全文 ------------------------------------------
        material_content = ""
        try:
            material = await self._db.get_material(material_id)
            if material is not None:
                material_content = material.get("content", "")
                logger.info(
                    "PARSING: 已从 DB 读取材料全文 (%d 字符)。",
                    len(material_content),
                )
            else:
                logger.warning(
                    "PARSING: material_id=%s 在数据库中未找到，"
                    "LLM 将收到空文档。",
                    material_id,
                )
        except Exception as exc:
            logger.warning(
                "PARSING: 无法从 DB 读取材料全文: %s。将使用空文档继续。",
                exc,
            )

        if not material_content.strip():
            logger.error(
                "PARSING: 材料全文为空！material_id=%s。"
                "请确认材料上传时内容已正确保存。",
                material_id,
            )

        try:
            prompt = _build_parsing_prompt(material_id, material_content)

            system_prompt = self._roles.build_context(
                role=role.value,
                project_path="",  # 教学场景无需项目路径
                extra_context={
                    "phase": "parsing",
                    "material_id": material_id,
                    "action": "解析 PDF 内容，输出知识片段列表",
                },
            )

            response = await self._invoke_role(
                role=role.value,
                user_message=prompt,
                system_prompt=system_prompt,
                tier=_PARSING_MODEL_TIER,
            )

            # 防御解析：从 LLM 响应中提取 chunks 列表
            chunks_raw = _safe_parse_json_list(response, "chunks")
            chunks = _hydrate_models(
                chunks_raw,
                KnowledgeChunk,
                defaults={"material_id": material_id},
            )
            self._artifacts["chunks"] = chunks_raw       # 保留原始 dict 供 LLM prompt
            self._artifacts["chunk_models"] = chunks      # Pydantic 模型供内部消费
            self._artifacts["parsing_output"] = response

            await self._on_step_complete(
                role=role.value,
                artifact={
                    "type": "parsing_result",
                    "title": "PDF 解析结果",
                    "summary_256": f"生成 {len(chunks)} 个知识片段",
                    "full_text": response[:2000],
                },
            )

            await self._end_phase(PipelinePhase.PARSING, role)
            logger.info("PARSING 阶段完成：%d 个知识片段。", len(chunks))

        except Exception as exc:
            self._set_role_status(role, "error", str(exc))
            await self._handle_phase_error(exc)
            raise

    async def _quiz_gen_phase(self) -> None:
        """QUIZ_GEN 阶段：Assistant 基于知识库生成测验题目。

        输入：_artifacts["chunks"]
        产出：Question 列表 + QuizSession，写入 _artifacts["questions"]
        下一状态：EVALUATING
        """
        role = AIRole.ASSISTANT
        await self._start_phase(PipelinePhase.QUIZ_GEN, role, "基于知识库生成测验题目")

        chunks = self._artifacts.get("chunks", [])

        try:
            prompt = _build_quiz_gen_prompt(chunks)

            system_prompt = self._roles.build_context(
                role=role.value,
                project_path="",
                extra_context={
                    "phase": "quiz_gen",
                    "chunk_count": str(len(chunks)),
                    "action": "根据知识片段生成测验题目",
                },
            )

            response = await self._invoke_role(
                role=role.value,
                user_message=prompt,
                system_prompt=system_prompt,
                tier=_QUIZ_GEN_MODEL_TIER,
            )

            questions_raw = _safe_parse_json_list(response, "questions")
            # 关联 chunk_ids 作为默认字段注入
            chunk_id_list = [
                c.get("chunk_id", "") for c in self._artifacts.get("chunks", [])
            ]
            questions = _hydrate_models(
                questions_raw,
                Question,
                defaults={"chunk_ids": chunk_id_list} if chunk_id_list else None,
            )
            self._artifacts["questions"] = questions_raw     # 保留原始 dict 供 LLM prompt
            self._artifacts["question_models"] = questions    # Pydantic 模型供内部消费
            self._artifacts["quiz_gen_output"] = response

            # -- 持久化题目到 questions 表（避免丢失后重新消耗 Token）--
            session_id = self.session_id or "unknown"
            now_iso = datetime.now(timezone.utc).isoformat()
            persisted_q = 0
            for i, q_raw in enumerate(questions_raw):
                try:
                    qid = q_raw.get("question_id", str(uuid4()))
                    await self._db.insert_question({
                        "question_id": qid,
                        "session_id": session_id,
                        "type": q_raw.get("type", "multiple_choice"),
                        "difficulty": q_raw.get("difficulty", "medium"),
                        "subject": q_raw.get("subject", ""),
                        "topic": q_raw.get("topic", ""),
                        "stem": q_raw.get("stem", ""),
                        "options": q_raw.get("options", []),
                        "correct_answer": q_raw.get("correct_answer", ""),
                        "explanation": q_raw.get("explanation", ""),
                        "chunk_ids": q_raw.get("chunk_ids", chunk_id_list),
                        "knowledge_node_ids": q_raw.get("knowledge_node_ids", []),
                        "estimated_seconds": q_raw.get("estimated_seconds", 120),
                        "points": q_raw.get("points", 1.0),
                        "tags": q_raw.get("tags", []),
                        "metadata": q_raw.get("metadata", {}),
                        "created_at": now_iso,
                    })
                    persisted_q += 1
                except Exception as exc:
                    logger.warning(
                        "题目 %s 持久化失败: %s",
                        q_raw.get("question_id", f"#{i}"), exc,
                    )
            logger.info(
                "QUIZ_GEN: %d/%d 道题目已持久化到 questions 表。",
                persisted_q, len(questions_raw),
            )

            await self._on_step_complete(
                role=role.value,
                artifact={
                    "type": "quiz_generation",
                    "title": "题目生成结果",
                    "summary_256": f"生成 {len(questions)} 道题目",
                    "full_text": response[:2000],
                },
            )

            await self._end_phase(PipelinePhase.QUIZ_GEN, role)
            logger.info("QUIZ_GEN 阶段完成：%d 道题目。", len(questions))

        except Exception as exc:
            self._set_role_status(role, "error", str(exc))
            await self._handle_phase_error(exc)
            raise

    async def _evaluating_phase(self) -> None:
        """EVALUATING 阶段：Evaluator 批改学生作答。

        输入：_artifacts["questions"] + 学生作答数据
        产出：批改结果 + MisconceptionTag 列表，写入 _artifacts["attempts"]
        下一状态：PLANNING
        """
        role = AIRole.EVALUATOR
        await self._start_phase(PipelinePhase.EVALUATING, role, "批改作答，诊断迷思概念")

        questions = self._artifacts.get("questions", [])
        student_answers = self._session_context.get("student_answers", [])

        try:
            prompt = _build_evaluating_prompt(questions, student_answers)

            system_prompt = self._roles.build_context(
                role=role.value,
                project_path="",
                extra_context={
                    "phase": "evaluating",
                    "question_count": str(len(questions)),
                    "action": "批改学生作答并诊断迷思概念",
                },
            )

            response = await self._invoke_role(
                role=role.value,
                user_message=prompt,
                system_prompt=system_prompt,
                tier=_EVALUATING_MODEL_TIER,
            )

            attempts_raw = _safe_parse_json_list(response, "attempts")
            misconceptions_raw = _safe_parse_json_list(response, "misconceptions")

            session_id = self.session_id or "unknown"
            student_id = self._session_context.get("student_id", "")
            attempts = _hydrate_models(
                attempts_raw,
                QuizAttempt,
                defaults={"session_id": session_id, "student_id": student_id},
            )
            misconceptions = _hydrate_models(
                misconceptions_raw,
                MisconceptionTag,
            )
            self._artifacts["attempts"] = attempts_raw
            self._artifacts["misconceptions"] = misconceptions_raw
            self._artifacts["attempt_models"] = attempts
            self._artifacts["misconception_models"] = misconceptions
            self._artifacts["evaluating_output"] = response

            # -- 持久化作答记录到 quiz_attempts 表 -------------------------
            now_iso = datetime.now(timezone.utc).isoformat()
            persisted = 0
            for raw_attempt in attempts_raw:
                try:
                    await self._db.insert_attempt(
                        {
                            "attempt_id": raw_attempt.get("attempt_id", str(uuid4())),
                            "session_id": session_id,
                            "student_id": student_id,
                            "question_id": raw_attempt.get("question_id", ""),
                            "student_answer": raw_attempt.get("student_answer"),
                            "is_correct": raw_attempt.get("is_correct"),
                            "score": raw_attempt.get("score"),
                            "time_spent_seconds": raw_attempt.get("time_spent_seconds", 0),
                            "hints_used": raw_attempt.get("hints_used", 0),
                            "attempt_number": raw_attempt.get("attempt_number", 1),
                            "confidence": raw_attempt.get("confidence"),
                            "misconception_ids": raw_attempt.get("misconception_ids", []),
                            "note": raw_attempt.get("note", ""),
                            "started_at": raw_attempt.get("started_at", now_iso),
                            "submitted_at": raw_attempt.get("submitted_at", now_iso),
                            "metadata": raw_attempt.get("metadata", {}),
                        }
                    )
                    persisted += 1
                except Exception as exc:
                    logger.warning(
                        "Failed to persist attempt %s: %s",
                        raw_attempt.get("attempt_id", "?"),
                        exc,
                    )
            logger.info(
                "EVALUATING: %d/%d attempts persisted to DB (student_id=%r).",
                persisted,
                len(attempts_raw),
                student_id,
            )

            # -- 更新掌握度记录 (mastery_records) ---------------------------
            if student_id:
                await self._persist_mastery_records(
                    student_id=student_id,
                    attempts_raw=attempts_raw,
                    session_id=session_id,
                )

            await self._on_step_complete(
                role=role.value,
                artifact={
                    "type": "evaluation_result",
                    "title": "批改与诊断结果",
                    "summary_256": (
                        f"批改 {len(attempts)} 题，"
                        f"诊断 {len(misconceptions)} 个迷思概念"
                    ),
                    "full_text": response[:2000],
                },
            )

            await self._end_phase(PipelinePhase.EVALUATING, role)
            logger.info(
                "EVALUATING 阶段完成：%d 题已批改，%d 个迷思概念。",
                len(attempts),
                len(misconceptions),
            )

        except Exception as exc:
            self._set_role_status(role, "error", str(exc))
            await self._handle_phase_error(exc)
            raise

    async def _planning_phase(self) -> None:
        """PLANNING 阶段：Tutor 生成 SM-2 排期学习计划。

        输入：_artifacts 中的批改结果 + 迷思概念
        产出：StudyPlan（ReviewItem 列表），写入 _artifacts["plan_items"]
        下一状态：DONE
        """
        role = AIRole.TUTOR
        await self._start_phase(PipelinePhase.PLANNING, role, "生成 SM-2 排期学习计划")

        attempts = self._artifacts.get("attempts", [])
        misconceptions = self._artifacts.get("misconceptions", [])

        try:
            prompt = _build_planning_prompt(attempts, misconceptions)

            system_prompt = self._roles.build_context(
                role=role.value,
                project_path="",
                extra_context={
                    "phase": "planning",
                    "attempt_count": str(len(attempts)),
                    "misconception_count": str(len(misconceptions)),
                    "action": "综合评估数据生成 SM-2 间隔重复排期计划",
                },
            )

            response = await self._invoke_role(
                role=role.value,
                user_message=prompt,
                system_prompt=system_prompt,
                tier=_PLANNING_MODEL_TIER,
            )

            plan_items_raw = _safe_parse_json_list(response, "plan_items")
            plan_items = _hydrate_models(
                plan_items_raw,
                ReviewItem,
                defaults={},  # mastery_record_id 由后续 DB 关联决定，不在此硬编码
            )
            self._artifacts["plan_items"] = plan_items_raw
            self._artifacts["plan_item_models"] = plan_items
            self._artifacts["planning_output"] = response

            # -- 持久化排期计划到 study_plans + review_items 表 -----------
            student_id = self._session_context.get("student_id", "")
            if student_id and plan_items_raw:
                try:
                    plan_id = str(uuid4())
                    today_str = date.today().isoformat()
                    now_iso = datetime.now(timezone.utc).isoformat()

                    # 查询学生已有的 mastery_records，用于关联
                    mastery_records = await self._db.list_mastery_records(student_id)
                    node_to_record: dict[str, str] = {
                        r["knowledge_node_id"]: r["record_id"]
                        for r in mastery_records
                    }

                    plan_dict = {
                        "plan_id": plan_id,
                        "student_id": student_id,
                        "title": self._session_context.get("title", "学习计划"),
                        "description": "由 Tutor 角色基于测验批改结果自动生成",
                        "subject": "",
                        "goal": "",
                        "start_date": today_str,
                        "end_date": None,
                        "status": "active",
                        "created_at": now_iso,
                        "updated_at": now_iso,
                    }

                    items_to_persist: list[dict[str, Any]] = []
                    for i, item in enumerate(plan_items_raw):
                        node_id = item.get("knowledge_node_id", "")
                        # Override LLM-generated dates: distribute items from today onward
                        raw_date = item.get("scheduled_date", today_str)
                        try:
                            # Try to parse as date; if it looks like a real date, use it
                            parsed = date.fromisoformat(raw_date[:10])
                            if parsed < date.today():
                                # Past date — offset from today by item index
                                actual_date = (date.today() + timedelta(days=i // 2)).isoformat()
                            else:
                                actual_date = raw_date[:10]
                        except Exception:
                            actual_date = (date.today() + timedelta(days=i // 2)).isoformat()

                        items_to_persist.append({
                            "item_id": item.get("item_id", str(uuid4())),
                            "plan_id": plan_id,
                            "student_id": student_id,
                            "knowledge_node_id": node_id,
                            "mastery_record_id": node_to_record.get(node_id),
                            "scheduled_date": actual_date,
                            "activity_type": item.get("activity_type", "review"),
                            "estimated_minutes": item.get("estimated_minutes", 15),
                            "completed": False,
                            "completed_at": None,
                            "notes": item.get("notes", ""),
                            "metadata": item.get("metadata", {}),
                        })

                    await self._db.create_study_plan(plan_dict, items_to_persist)
                    logger.info(
                        "PLANNING: study plan %s with %d items persisted (student_id=%r).",
                        plan_id,
                        len(items_to_persist),
                        student_id,
                    )
                except Exception as exc:
                    logger.warning("Failed to persist study plan: %s", exc)

            await self._on_step_complete(
                role=role.value,
                artifact={
                    "type": "study_plan",
                    "title": "SM-2 排期学习计划",
                    "summary_256": f"生成 {len(plan_items)} 个复习条目",
                    "full_text": response[:2000],
                },
            )

            await self._end_phase(PipelinePhase.PLANNING, role)
            logger.info("PLANNING 阶段完成：%d 个排期条目。", len(plan_items))

        except Exception as exc:
            self._set_role_status(role, "error", str(exc))
            await self._handle_phase_error(exc)
            raise

    # ==================================================================
    # LLM 调用封装
    # ==================================================================

    async def _invoke_role(
        self,
        role: str,
        user_message: str,
        system_prompt: Optional[str] = None,
        tier: str = "medium",
    ) -> str:
        """调用 AI 角色并记录 token 用量。

        封装 ``LLMClient.chat_with_file_context``，增加角色校验、
        token 日志和错误传播。

        Args:
            role: 角色标识（``"tutor"`` / ``"assistant"`` / ``"evaluator"``）。
            user_message: 给 AI 角色的指令或上下文。
            system_prompt: 系统提示词。None 时由 RoleManager 自动加载。
            tier: 算力档位（``"heavy"`` / ``"medium"`` / ``"light"``）。

        Returns:
            AI 角色的完整响应文本。

        Raises:
            LLMClientError: API 调用失败（重试耗尽后）。
        """
        if role not in VALID_ROLES:
            raise OrchestratorError(
                f"未知角色 '{role}'。有效角色: {sorted(VALID_ROLES)}"
            )

        # Token 预算检查（仅当 TokenTracker 注入时）
        if self._token_tracker is not None:
            budget_status = await self._token_tracker.check_budget(
                self.session_id or "unknown"
            )
            if budget_status["exhausted"]:
                raise OrchestratorError(
                    f"Token 预算已耗尽（budget={budget_status.get('budget', '?')}）。"
                    f"请联系管理员增加预算或等待下个周期。"
                )
            if budget_status["warning"]:
                logger.warning(
                    "Token 预算使用率 >= 80%%（remaining=%d）",
                    budget_status.get("remaining", 0),
                )

        try:
            response = await self._llm.chat_with_file_context(
                role=role,
                user_message=user_message,
                files=[],
                tier=tier,
                system_prompt=system_prompt,
            )

            prompt_tokens = _estimate_tokens(user_message)
            completion_tokens = _estimate_tokens(response)

            # 优先走 TokenTracker（含预算管控 + 内存聚合），
            # 否则回退到 DB 直写。
            if self._token_tracker is not None:
                await self._token_tracker.record(
                    project_id=self.session_id or "unknown",
                    role=role,
                    task_id="",
                    model_tier=tier,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                )
            else:
                await self._db.log_token_usage(
                    {
                        "project_id": self.session_id or "unknown",
                        "role": role,
                        "tier": tier,
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": prompt_tokens + completion_tokens,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                )

            return response

        except LLMClientError:
            raise
        except Exception as exc:
            logger.error("调用角色 '%s' 时发生异常: %s", role, exc)
            raise LLMClientError(
                f"调用角色 '{role}' 失败: {exc}"
            ) from exc

    # ==================================================================
    # 状态机辅助方法
    # ==================================================================

    async def _persist_mastery_records(
        self,
        *,
        student_id: str,
        attempts_raw: list[dict[str, Any]],
        session_id: str,
    ) -> None:
        """从批改结果更新学生的知识点掌握度记录（含 SM-2 参数）。

        对每个 attempt 涉及的 knowledge_node，汇总该学生在此次
        会话中的表现，与数据库中已有记录合并后 upsert。
        """
        # -- 汇总本次会话的节点级统计 ---------------------------------------
        node_stats: dict[str, dict[str, Any]] = {}
        for attempt in attempts_raw:
            question_id = attempt.get("question_id", "")
            is_correct = attempt.get("is_correct", False)
            score = attempt.get("score") or (1.0 if is_correct else 0.0)
            time_spent = attempt.get("time_spent_seconds", 0)
            hints_used = attempt.get("hints_used", 0)
            mis_ids = attempt.get("misconception_ids", [])

            # 查找题目对应的知识点
            question_rows = []
            try:
                q = await self._db.get_question(question_id)
                if q:
                    question_rows.append(q)
            except Exception:
                pass

            # 如果找不到题目，使用 question_id 本身作为 node
            if not question_rows:
                node_ids = [f"node:{question_id}"]
            else:
                raw_ids = question_rows[0].get("knowledge_node_ids", "[]")
                try:
                    parsed = _json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
                    node_ids = parsed if parsed else [f"node:{question_id}"]
                except Exception:
                    node_ids = [f"node:{question_id}"]

            for node_id in node_ids:
                if node_id not in node_stats:
                    node_stats[node_id] = {
                        "total": 0,
                        "correct": 0,
                        "total_score": 0.0,
                        "total_time": 0,
                        "total_hints": 0,
                        "misconception_ids": set(),
                        "last_attempt_at": "",
                    }
                s = node_stats[node_id]
                s["total"] += 1
                if is_correct:
                    s["correct"] += 1
                s["total_score"] += score
                s["total_time"] += time_spent
                s["total_hints"] += hints_used
                s["misconception_ids"].update(mis_ids if isinstance(mis_ids, list) else [])
                s["last_attempt_at"] = attempt.get("submitted_at", "")

        # -- 查询已有记录，合并后 upsert -----------------------------------
        existing_records = await self._db.list_mastery_records(student_id)
        existing_map: dict[str, dict[str, Any]] = {
            r["knowledge_node_id"]: r for r in existing_records
        }

        now_iso = datetime.now(timezone.utc).isoformat()
        for node_id, stats in node_stats.items():
            existing = existing_map.get(node_id)

            total = stats["total"] + (existing.get("total_attempts", 0) if existing else 0)
            correct = stats["correct"] + (existing.get("correct_attempts", 0) if existing else 0)
            mastery_level = correct / total if total > 0 else 0.0
            avg_score = stats["total_score"] / stats["total"] if stats["total"] > 0 else 0.0

            # SM-2 quality: 0-5 scale based on score
            if avg_score >= 0.9:
                quality = 5
            elif avg_score >= 0.7:
                quality = 4
            elif avg_score >= 0.5:
                quality = 3
            elif avg_score >= 0.3:
                quality = 2
            elif avg_score >= 0.1:
                quality = 1
            else:
                quality = 0

            # SM-2 algorithm
            if existing:
                sm2_reps = existing.get("sm2_repetitions", 0)
                sm2_ef = existing.get("sm2_ease_factor", 2.5)
                sm2_interval = existing.get("sm2_interval_days", 0)
            else:
                sm2_reps = 0
                sm2_ef = 2.5
                sm2_interval = 0

            if quality >= 3:
                if sm2_reps == 0:
                    sm2_interval = 1
                elif sm2_reps == 1:
                    sm2_interval = 6
                else:
                    sm2_interval = int(round(sm2_interval * sm2_ef))
                sm2_reps += 1
            else:
                sm2_reps = 0
                sm2_interval = 1

            # Ease factor update
            sm2_ef = sm2_ef + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
            sm2_ef = max(1.3, sm2_ef)

            # Next review date
            next_review = (date.today() + timedelta(days=sm2_interval)).isoformat()

            # Determine state
            if mastery_level >= 0.85:
                state = "mastered"
            elif mastery_level >= 0.6:
                state = "reviewing"
            elif total > 0:
                state = "learning"
            else:
                state = "new"

            record_id = existing["record_id"] if existing else str(uuid4())
            existing_mis = set()
            if existing and existing.get("misconception_ids"):
                try:
                    existing_mis = set(_json.loads(existing["misconception_ids"])
                        if isinstance(existing["misconception_ids"], str)
                        else existing["misconception_ids"])
                except Exception:
                    pass

            all_mis = list(stats["misconception_ids"] | existing_mis)

            await self._db.upsert_mastery_record({
                "record_id": record_id,
                "student_id": student_id,
                "knowledge_node_id": node_id,
                "mastery_level": round(mastery_level, 3),
                "confidence": min(0.9, 0.3 + total * 0.05),
                "total_attempts": total,
                "correct_attempts": correct,
                "last_attempt_at": stats["last_attempt_at"] or now_iso,
                "last_score": round(avg_score, 3),
                "streak": stats["correct"] if stats["correct"] == stats["total"] else 0,
                "time_spent_total_seconds": stats["total_time"],
                "hints_used_total": stats["total_hints"],
                "misconception_ids": all_mis,
                "state": state,
                "sm2_repetitions": sm2_reps,
                "sm2_ease_factor": round(sm2_ef, 3),
                "sm2_interval_days": sm2_interval,
                "sm2_next_review": next_review,
                "sm2_last_quality": quality,
                "created_at": existing.get("created_at", now_iso) if existing else now_iso,
                "updated_at": now_iso,
            })

        logger.info(
            "EVALUATING: mastery_records updated for %d knowledge nodes (student_id=%r).",
            len(node_stats),
            student_id,
        )

    async def _handle_phase_error(self, exc: Exception) -> None:
        """记录阶段异常并持久化错误状态。

        注意：不重置 ``_in_progress``，保留崩溃标记以便恢复时回退。
        """
        self._error_message = str(exc)
        self._previous_phase = self._phase
        await self.save()
        logger.error("阶段执行失败: %s", exc)

    # ==================================================================
    # 持久化 & 角色状态辅助
    # ==================================================================

    async def _on_step_complete(
        self, role: str, artifact: dict[str, Any]
    ) -> None:
        """阶段完成回调：将产出物写入数据库。"""
        try:
            await self._db.insert_artifact(
                {
                    "project_id": self.session_id or "unknown",
                    "role": role,
                    "type": artifact.get("type", "generic"),
                    "title": artifact.get("title", ""),
                    "summary_256": artifact.get("summary_256", ""),
                    "full_text": artifact.get("full_text", ""),
                    "file_path": artifact.get("file_path"),
                    "version": artifact.get("version", 1),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        except Exception as exc:
            logger.warning(
                "产出物持久化失败 (role=%s): %s", role, exc
            )

        # 自动持久化会话状态
        await self.save()

    def _set_role_status(
        self, role: AIRole, status: str, task: Optional[str] = None
    ) -> None:
        """更新 AI 角色的追踪状态。"""
        self._role_statuses[role.value] = status
        self._role_tasks[role.value] = task


# ======================================================================
# 阶段 Prompt 构建函数（模块级辅助函数）
# ======================================================================


def _build_parsing_prompt(material_id: str, content: str = "") -> str:
    """构建 PDF 解析阶段的用户提示词。

    Args:
        material_id: 材料唯一标识。
        content: 材料的全文内容（由 PyMuPDF 提取或文本上传）。"""
    # 截断过长的文本以适配 LLM 上下文窗口
    # 中文约 1.5 字符/token，50K 字符 ≈ 33K tokens，留足输出空间
    _MAX_CONTENT_CHARS = 50_000
    if len(content) > _MAX_CONTENT_CHARS:
        content = content[:_MAX_CONTENT_CHARS]
        truncation_note = (
            f"\n\n（注：原文共超过 {_MAX_CONTENT_CHARS} 字符，"
            f"已截断至前 {_MAX_CONTENT_CHARS} 字符。请分析已有内容。）"
        )
    else:
        truncation_note = ""

    return (
        "你是一位教学资料分析专家。请分析以下学习材料，将其拆分为独立的知识片段。\n\n"
        f"材料 ID: {material_id}\n"
        f"材料长度: {len(content)} 字符{truncation_note}\n\n"
        "────────────────────────────────────────\n"
        "## 学习材料内容\n\n"
        f"{content}\n\n"
        "────────────────────────────────────────\n\n"
        "## 输出要求\n"
        "将材料内容按知识点边界切分为多个 chunk，每个 chunk 包含：\n"
        "1. **content**: 原文片段（保持完整语义，200-2000 字）\n"
        "2. **summary**: 一句话摘要（≤256 字符）\n"
        "3. **topic**: 主题标签（如'牛顿定律'、'矩阵运算'）\n"
        "4. **difficulty**: 难度评估（beginner / easy / medium / hard / expert）\n"
        "5. **keywords**: 3-5 个关键词\n\n"
        "请以 JSON 数组格式输出，格式为：\n"
        '```json\n{"chunks": [{"content": "...", "summary": "...", '
        '"topic": "...", "difficulty": "medium", "keywords": ["..."]}]}\n```\n\n'
        "注意：\n"
        "- 保持原文语义完整，不要截断句子\n"
        "- 数学公式/代码块保持原样\n"
        "- 按原文顺序排列 chunks"
    )


def _build_quiz_gen_prompt(chunks: list[dict[str, Any]]) -> str:
    """构建题目生成阶段的用户提示词。"""
    # 限制 chunks 数量以避免 prompt 过长
    chunks_preview = chunks[:15] if len(chunks) > 15 else chunks
    chunks_json = _safe_truncate_json(chunks_preview, max_items=15)

    return (
        "你是一位资深教学出题专家。请根据以下知识片段生成一套测验题。\n\n"
        f"## 知识片段（共 {len(chunks)} 个）\n"
        f"```json\n{chunks_json}\n```\n\n"
        "## 出题要求\n"
        "1. 每个知识片段至少出 1 道题\n"
        "2. 题型以选择题（multiple_choice）为主，可含少量简答题（short_answer）\n"
        "3. 难度分布：记忆 30% / 理解 40% / 应用 20% / 分析 10%\n"
        "4. 每道题包含：\n"
        "   - **stem**: 题干\n"
        "   - **type**: 题目类型\n"
        "   - **options**: 选项列表 [{'key': 'A', 'text': '...'}, ...]\n"
        "   - **correct_answer**: 正确答案\n"
        "   - **explanation**: 详细解析\n"
        "   - **difficulty**: 难度评估\n"
        "   - **knowledge_node_ids**: 考查的知识点\n\n"
        "请以 JSON 数组格式输出：\n"
        '```json\n{"questions": [{"stem": "...", "type": "multiple_choice", '
        '"options": [...], "correct_answer": "A", "explanation": "...", '
        '"difficulty": "easy", "knowledge_node_ids": ["..."]}]}\n```'
    )


def _build_evaluating_prompt(
    questions: list[dict[str, Any]],
    student_answers: list[dict[str, Any]],
) -> str:
    """构建批改诊断阶段的用户提示词。"""
    questions_json = _safe_truncate_json(questions, max_items=20)
    answers_json = _safe_truncate_json(student_answers, max_items=20)

    return (
        "你是一位严谨的教学评估专家。请批改学生的作答并诊断其迷思概念。\n\n"
        f"## 题目\n```json\n{questions_json}\n```\n\n"
        f"## 学生作答\n```json\n{answers_json}\n```\n\n"
        "## 评估要求\n"
        "1. 逐题判定对错（is_correct）\n"
        "2. 为错题诊断迷思概念（misconception）：\n"
        "   - **label**: 错误标签（如'动量与动能混淆'）\n"
        "   - **category**: 错误类别（conceptual / calculation / careless / "
        "application / logic / notation / incomplete）\n"
        "   - **description**: 错误详细描述\n"
        "   - **remediation_hint**: 补救建议\n"
        "3. 为每道错题提供一条苏格拉底式引导提示（不直接给答案，引导学生自己发现）\n\n"
        "请以 JSON 格式输出：\n"
        '```json\n{\n'
        '  "attempts": [{"question_id": "...", "is_correct": false, '
        '"score": 0.0, "misconception_ids": ["..."]}],\n'
        '  "misconceptions": [{"label": "...", "category": "conceptual", '
        '"description": "...", "remediation_hint": "..."}]\n'
        '}\n```'
    )


def _build_planning_prompt(
    attempts: list[dict[str, Any]],
    misconceptions: list[dict[str, Any]],
) -> str:
    """构建排期计划阶段的用户提示词。"""
    attempts_json = _safe_truncate_json(attempts, max_items=20)
    misconceptions_json = _safe_truncate_json(misconceptions, max_items=10)

    return (
        "你是一位学习规划专家。请根据学生的作答表现和迷思概念诊断，"
        "制定一份基于 SM-2 算法的间隔重复复习计划。\n\n"
        f"## 作答记录\n```json\n{attempts_json}\n```\n\n"
        f"## 迷思概念\n```json\n{misconceptions_json}\n```\n\n"
        "## 排期要求\n"
        "1. 对每个未掌握的知识点，安排复习条目（review item）\n"
        "2. 使用 SM-2 算法计算复习间隔：\n"
        "   - 首次学习: 1 天后复习\n"
        "   - 第二次: 6 天后复习\n"
        "   - 之后: interval = previous_interval × EF\n"
        "3. 优先级规则：薄弱知识点 × 逾期天数（弱且紧急的排前面）\n"
        "4. 每天学习量不超过 2 小时\n"
        "5. 每个复习条目包含：\n"
        "   - **scheduled_date**: 计划复习日期\n"
        "   - **activity_type**: review / practice / quiz\n"
        "   - **estimated_minutes**: 预计耗时\n"
        "   - **knowledge_node_id**: 对应知识点\n\n"
        "请以 JSON 数组格式输出：\n"
        '```json\n{"plan_items": [{"scheduled_date": "2025-01-01", '
        '"activity_type": "review", "estimated_minutes": 15, '
        '"knowledge_node_id": "..."}]}\n```'
    )


# ======================================================================
# 通用辅助函数
# ======================================================================


def _safe_parse_json_list(
    response: str, key: str
) -> list[dict[str, Any]]:
    """从 LLM 响应中安全提取 JSON 列表。

    使用 4 层防御策略：
    1. 直接 ``json.loads`` 整个响应
    2. 正则提取 ```json ... ``` 围栏代码块
    3. 正则提取第一个 ``{...}`` 或 ``[...]``
    4. 返回空列表

    Args:
        response: LLM 原始响应文本。
        key: 期望的 JSON 对象键名（如 ``"chunks"``、``"questions"``）。

    Returns:
        解析出的字典列表，失败时返回空列表。
    """
    # 第 1 层：尝试直接解析整个响应
    try:
        data = _json.loads(response)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and key in data:
            items = data[key]
            return items if isinstance(items, list) else [items]
    except (_json.JSONDecodeError, TypeError):
        pass

    # 第 2 层：提取 ```json ... ``` 围栏代码块
    fence_match = _re.search(
        r"```(?:json)?\s*\n?([\s\S]*?)\n?```", response
    )
    if fence_match:
        try:
            data = _json.loads(fence_match.group(1).strip())
            if isinstance(data, list):
                return data
            if isinstance(data, dict) and key in data:
                items = data[key]
                return items if isinstance(items, list) else [items]
        except (_json.JSONDecodeError, TypeError):
            pass

    # 第 3 层：查找第一个 JSON 对象或数组
    json_match = _re.search(r"(\[.*\]|\{.*\})", response, _re.DOTALL)
    if json_match:
        try:
            data = _json.loads(json_match.group(1))
            if isinstance(data, list):
                return data
            if isinstance(data, dict) and key in data:
                items = data[key]
                return items if isinstance(items, list) else [items]
        except (_json.JSONDecodeError, TypeError):
            pass

    # 第 4 层：放弃，返回空列表
    logger.warning(
        "_safe_parse_json_list: 无法解析 LLM 响应为 JSON，"
        "key=%s，响应前 200 字符: %s",
        key,
        response[:200],
    )
    return []


def _hydrate_models(
    raw_items: list[dict[str, Any]],
    model_cls: type,
    *,
    defaults: Optional[dict[str, Any]] = None,
) -> list[Any]:
    """将 LLM 输出的原始字典列表反序列化为 Pydantic 模型实例。

    逐条尝试 ``model_cls(**defaults, **item)``，验证失败的条目
    记录警告后跳过，确保单个脏数据不影响整批产出物。

    Args:
        raw_items: LLM 输出的原始字典列表。
        model_cls: 目标 Pydantic 模型类（如 ``KnowledgeChunk``）。
        defaults: 注入到每条记录的默认字段值
                  （如 ``material_id``、``session_id`` 等运行时上下文）。

    Returns:
        成功反序列化的模型实例列表（可能短于输入）。
    """
    models: list[Any] = []
    merged_defaults = defaults or {}
    for i, item in enumerate(raw_items):
        try:
            merged = {**merged_defaults, **item}
            models.append(model_cls(**merged))
        except Exception as exc:
            logger.warning(
                "_hydrate_models: 第 %d 条 %s 反序列化失败: %s",
                i,
                model_cls.__name__,
                exc,
            )
    if models:
        logger.debug(
            "_hydrate_models: %d/%d 条成功反序列化为 %s。",
            len(models),
            len(raw_items),
            model_cls.__name__,
        )
    return models


def _safe_truncate_json(
    items: list[dict[str, Any]], max_items: int = 15
) -> str:
    """将列表截断并序列化为 JSON 字符串。

    超过 ``max_items`` 时自动截断并附加占位提示。

    Args:
        items: 待序列化的字典列表。
        max_items: 最大保留条数。

    Returns:
        JSON 字符串。
    """
    truncated = items[:max_items]
    result = _json.dumps(truncated, ensure_ascii=False, indent=2)
    if len(items) > max_items:
        result += f"\n// ... 共 {len(items)} 条，已截断显示前 {max_items} 条"
    return result


def _estimate_tokens(text: str) -> int:
    """粗略估算 token 数（按词数 / 0.75）。"""
    return max(1, int(len(text.split()) / 0.75))
