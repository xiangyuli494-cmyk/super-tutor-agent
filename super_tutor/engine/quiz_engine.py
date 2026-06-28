"""Quiz Engine — 题目生成、自动批改与错题收录。

将 LLM 出题、程序/LLM 批改和错题本维护封装为高层次的
业务逻辑组件，供 orchestration 层和前端直接使用。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from super_tutor.core.database import Database
from super_tutor.core.exceptions import LLMError, MaterialError
from super_tutor.core.llm_client import LLMClient
from super_tutor.engine.knowledge_engine import KnowledgeEngine, _parse_json_list
from super_tutor.models.enums import DifficultyLevel, QuestionType
from super_tutor.models.quiz import Question, QuizAttempt

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 默认 prompt 路径
# ---------------------------------------------------------------------------
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_DEFAULT_QUIZ_GEN_PROMPT = _PROMPTS_DIR / "quiz_gen.md"
_DEFAULT_GRADE_PROMPT = _PROMPTS_DIR / "grade.md"

# ---------------------------------------------------------------------------
# 程序批改覆盖的题型
# ---------------------------------------------------------------------------
_PROGRAMMATIC_TYPES: set[str] = {"multiple_choice", "true_false"}


class QuizEngine:
    """测验引擎 — 出题、批改和错题收录。

    封装了 LLM 出题、程序/LLM 混合批改和错题本写入逻辑。

    Usage::

        engine = QuizEngine(db, llm_client, knowledge_engine)
        questions = await engine.generate_questions(["kp-001"], count=5)
        attempts = await engine.grade_answers(questions, student_answers)
        for attempt in attempts:
            if not attempt.is_correct:
                await engine.add_to_wrong_book(attempt)
    """

    def __init__(
        self,
        db: Database,
        llm_client: LLMClient,
        knowledge_engine: KnowledgeEngine,
        quiz_gen_prompt_path: str | None = None,
        grade_prompt_path: str | None = None,
    ) -> None:
        """Initialise the quiz engine.

        Args:
            db: An initialised ``Database`` instance.
            llm_client: An ``LLMClient`` instance for LLM calls.
            knowledge_engine: A ``KnowledgeEngine`` for KP lookups.
            quiz_gen_prompt_path: Optional custom quiz-gen prompt path.
            grade_prompt_path: Optional custom grading prompt path.
        """
        self._db = db
        self._llm = llm_client
        self._knowledge = knowledge_engine
        self._quiz_gen_prompt_path = (
            quiz_gen_prompt_path or str(_DEFAULT_QUIZ_GEN_PROMPT)
        )
        self._grade_prompt_path = (
            grade_prompt_path or str(_DEFAULT_GRADE_PROMPT)
        )

    # ==================================================================
    # generate_questions
    # ==================================================================

    async def generate_questions(
        self,
        kp_ids: list[str],
        count: int = 5,
        difficulty: str | None = None,
        types: list[str] | None = None,
    ) -> list[Question]:
        """Generate quiz questions based on knowledge points.

        Fetches each knowledge point and its prerequisites from the
        database, builds a context-rich prompt, and calls the LLM to
        generate questions.  Results are persisted to ``questions``.

        Args:
            kp_ids: Knowledge point IDs to base questions on.
            count: Total number of questions to generate.
            difficulty: Optional difficulty override (e.g. ``"medium"``).
            types: Optional question type filter (e.g.
                ``["multiple_choice", "short_answer"]``).

        Returns:
            The list of newly created ``Question`` objects.
        """
        if not kp_ids:
            raise ValueError("kp_ids must not be empty")
        if count < 1:
            raise ValueError("count must be >= 1")

        # -- 1. Collect knowledge point data ---------------------------------
        kp_infos: list[dict[str, Any]] = []
        for kp_id in kp_ids:
            kp_row = await self._db.get_knowledge_point(kp_id)
            if kp_row is None:
                logger.warning("Knowledge point not found: %s", kp_id)
                continue

            # Fetch prerequisite summaries for context injection
            prereq_ids: list[str] = _parse_json_list(
                kp_row.get("prerequisite_ids", "[]")
            )
            prereq_summaries: list[str] = []
            for pid in prereq_ids:
                pr = await self._db.get_knowledge_point(pid)
                if pr:
                    prereq_summaries.append(
                        f"  [{pr.get('title', pid[:8])}] {pr.get('summary', '')}"
                    )

            kp_infos.append(
                {
                    "kp_id": kp_id,
                    "title": kp_row.get("title", ""),
                    "content": kp_row.get("content", ""),
                    "summary": kp_row.get("summary", ""),
                    "difficulty": kp_row.get("difficulty", "medium"),
                    "keywords": _parse_json_list(kp_row.get("keywords", "[]")),
                    "prerequisites": prereq_summaries,
                }
            )

        if not kp_infos:
            raise ValueError("None of the given kp_ids exist in the database")

        # -- 2. Build prompt context -----------------------------------------
        kp_context_lines: list[str] = []
        for info in kp_infos:
            lines = [
                f"## 知识点: {info['title']}",
                f"- kp_id: {info['kp_id']}",
                f"- difficulty: {info['difficulty']}",
                f"- keywords: {', '.join(info['keywords'])}" if info["keywords"] else "",
                f"- summary: {info['summary']}",
            ]
            if info["prerequisites"]:
                lines.append("- 前置知识点:")
                lines.extend(info["prerequisites"])
            lines.append(f"\n{info['content']}\n")
            kp_context_lines.append("\n".join(l for l in lines if l))

        kp_context = "\n---\n".join(kp_context_lines)

        # Distribute count across KPs
        per_kp = _distribute_counts([i["kp_id"] for i in kp_infos], count)

        constraints: list[str] = [f"请生成 {count} 道题目。"]
        constraints.append(
            f"知识点分布: {', '.join(f'{kid}:{n}道' for kid, n in per_kp.items())}"
        )
        if difficulty:
            constraints.append(f"所有题目难度统一为: {difficulty}")
        if types:
            constraints.append(f"只生成以下题型: {', '.join(types)}")

        user_prompt = (
            f"## 知识点列表\n\n{kp_context}\n\n"
            f"## 出题要求\n\n" + "\n".join(f"- {c}" for c in constraints)
        )

        # -- 3. Load system prompt & call LLM --------------------------------
        try:
            system_prompt = Path(self._quiz_gen_prompt_path).read_text(
                encoding="utf-8"
            )
        except OSError as exc:
            raise MaterialError(
                f"无法加载出题提示词: {self._quiz_gen_prompt_path} ({exc})"
            ) from exc

        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        logger.info(
            "Generating %d questions for %d KPs (difficulty=%s, types=%s)",
            count, len(kp_infos), difficulty or "auto", types or "auto",
        )

        try:
            raw = await self._llm.chat(
                messages=messages,
                temperature=0.7,
                max_tokens=8192,
                timeout=180,
            )
        except LLMError as exc:
            raise MaterialError(f"LLM 出题失败: {exc}") from exc

        # -- 4. Parse JSON response ------------------------------------------
        raw = _strip_markdown_fence(raw)

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.error("LLM 出题返回的 JSON 无法解析: %s", raw[:500])
            raise MaterialError(f"题目生成结果不是有效 JSON: {exc}") from exc

        raw_questions = data.get("questions", [])
        if not raw_questions:
            raise MaterialError("LLM 未生成任何题目。")

        # -- 5. Create Question objects & persist ----------------------------
        created: list[Question] = []
        now = datetime.now(timezone.utc).isoformat()

        # Fallback kp_id: use the first requested KP when the LLM omits it
        _fallback_kp_id = kp_ids[0] if kp_ids else ""

        for item in raw_questions:
            qid = str(uuid4())
            q = Question(
                question_id=qid,
                type=QuestionType(item.get("type", "multiple_choice")),
                difficulty=DifficultyLevel(
                    item.get("difficulty", "medium")
                ),
                subject=item.get("subject", ""),
                topic=item.get("topic", ""),
                stem=item.get("stem", ""),
                options=item.get("options", []),
                correct_answer=item.get("correct_answer", ""),
                explanation=item.get("explanation", ""),
                hints=item.get("hints", []),
                kp_id=item.get("kp_id", "").strip() or _fallback_kp_id,
                estimated_seconds=item.get("estimated_seconds", 120),
                points=item.get("points", 1.0),
                tags=item.get("tags", []),
                created_at=now,
            )

            await self._db.insert_question(
                {
                    "question_id": q.question_id,
                    "type": q.type.value,
                    "difficulty": q.difficulty.value,
                    "subject": q.subject,
                    "topic": q.topic,
                    "stem": q.stem,
                    "options": q.options,
                    "correct_answer": q.correct_answer,
                    "explanation": q.explanation,
                    "kp_id": q.kp_id,
                    "kp_context": json.dumps(
                        item.get("kp_context", {}), ensure_ascii=False
                    ),
                    "estimated_seconds": q.estimated_seconds,
                    "points": q.points,
                    "tags": q.tags,
                    "metadata": {},
                    "created_at": now,
                }
            )
            created.append(q)

        logger.info("Generated %d questions for %d KPs", len(created), len(kp_infos))
        return created

    # ==================================================================
    # grade_answers
    # ==================================================================

    async def grade_answers(
        self,
        questions: list[Question],
        student_answers: list[dict[str, Any]],
        student_id: str = "",
    ) -> list[QuizAttempt]:
        """Grade a batch of student answers.

        Multiple-choice and true/false questions are graded
        programmatically (no LLM cost).  All other types are sent to
        the LLM for semantic grading.

        Args:
            questions: The ``Question`` objects being answered.
            student_answers: A list of dicts, each containing at least
                ``question_id`` and ``student_answer``.  May also include
                ``time_spent_seconds``, ``hints_used``,
                ``attempt_number``, ``confidence``.
            grades: Pre-determined (full credit) statuses for any/all.

        Returns:
            A list of ``QuizAttempt`` objects (one per answer).
        """
        # -- 1. Build question lookup ----------------------------------------
        q_map: dict[str, Question] = {q.question_id: q for q in questions}
        answer_map: dict[str, dict] = {
            a["question_id"]: a for a in student_answers
        }

        # -- 2. Split: programmatic vs LLM -----------------------------------
        programmatic_items: list[tuple[Question, dict]] = []
        llm_items: list[tuple[Question, dict]] = []

        for a in student_answers:
            qid = a["question_id"]
            q = q_map.get(qid)
            if q is None:
                logger.warning("Answer references unknown question: %s", qid)
                continue
            if q.type.value in _PROGRAMMATIC_TYPES:
                programmatic_items.append((q, a))
            else:
                llm_items.append((q, a))

        # -- 3. Programmatic grading -----------------------------------------
        now = datetime.now(timezone.utc).isoformat()
        attempts: list[QuizAttempt] = []

        for q, ans in programmatic_items:
            is_correct, score, max_score = _grade_programmatic(
                q, str(ans.get("student_answer", ""))
            )
            attempt = await _persist_attempt(
                self._db, q, ans, student_id, is_correct, score, max_score, now
            )
            attempts.append(attempt)

        # -- 4. LLM grading --------------------------------------------------
        if llm_items:
            llm_attempts = await _grade_via_llm(
                self._llm,
                self._grade_prompt_path,
                llm_items,
                student_id,
                now,
            )
            # Persist each LLM-graded attempt
            for i, (q, ans) in enumerate(llm_items):
                result = (
                    llm_attempts[i]
                    if i < len(llm_attempts)
                    else {"is_correct": False, "score": 0.0, "max_score": 1.0}
                )
                attempt = await _persist_attempt(
                    self._db,
                    q,
                    ans,
                    student_id,
                    result.get("is_correct", False),
                    result.get("score", 0.0),
                    result.get("max_score", q.points),
                    now,
                    result.get("misconceptions"),
                    result.get("analysis", ""),
                )
                attempts.append(attempt)

        logger.info(
            "Graded %d answers: %d programmatic + %d LLM",
            len(attempts),
            len(programmatic_items),
            len(llm_items),
        )
        return attempts

    # ==================================================================
    # add_to_wrong_book
    # ==================================================================

    async def add_to_wrong_book(
        self,
        attempt: QuizAttempt,
        question: Question | None = None,
    ) -> dict[str, Any]:
        """Record an incorrect attempt in the wrong-answer notebook.

        Only records if ``attempt.is_correct`` is ``False``.  If the
        same question already has a wrong-book entry for this student,
        ``attempt_count`` is incremented.

        Args:
            attempt: The graded ``QuizAttempt``.
            question: The corresponding ``Question`` (used for
                ``correct_answer`` when not already available).

        Returns:
            The wrong-question record dict as inserted/updated in
            ``wrong_questions``.
        """
        if attempt.is_correct is not False:
            logger.debug(
                "Skipping wrong-book for correct attempt %s", attempt.attempt_id
            )
            return {}

        student_id = getattr(attempt, "student_id", "") or ""

        # Determine the correct answer for reference
        correct_answer: str = ""
        if question is not None:
            correct_answer = _serialize_answer(question.correct_answer)

        now = datetime.now(timezone.utc).isoformat()
        wrong_id = str(uuid4())

        # Check for existing entry (same student + same question)
        existing = await self._db.get_wrong_question_by_student_and_question(
            student_id, attempt.question_id
        )

        if existing is not None:
            # Increment attempt_count & refresh mutable fields
            new_count = existing.get("attempt_count", 1) + 1
            updates: dict[str, Any] = {
                "wrong_answer": _serialize_answer(attempt.student_answer or ""),
                "attempt_count": new_count,
                "updated_at": now,
            }
            # Refresh kp_id in case it was previously missing
            new_kp_id = getattr(attempt, "kp_id", "") or ""
            if new_kp_id and new_kp_id != existing.get("kp_id", ""):
                updates["kp_id"] = new_kp_id
            # Refresh correct_answer when available
            if correct_answer and correct_answer != existing.get("correct_answer", ""):
                updates["correct_answer"] = correct_answer
            await self._db.update_wrong_question(
                existing["wrong_id"],
                updates,
            )
            logger.debug(
                "Updated wrong-book entry %s (attempt #%d)",
                existing["wrong_id"],
                new_count,
            )
            existing["attempt_count"] = new_count
            existing["updated_at"] = now
            return existing

        # New entry
        record: dict[str, Any] = {
            "wrong_id": wrong_id,
            "student_id": student_id,
            "question_id": attempt.question_id,
            "kp_id": getattr(attempt, "kp_id", "") or "",
            "wrong_answer": _serialize_answer(attempt.student_answer or ""),
            "correct_answer": correct_answer,
            "attempt_count": 1,
            "resolution_status": "unresolved",
            "note": "",
            "created_at": now,
            "updated_at": now,
        }
        await self._db.insert_wrong_question(record)
        logger.info("Created wrong-book entry %s for question %s", wrong_id, attempt.question_id)
        return record


# ==================================================================
# Internal helpers — grading
# ==================================================================


def _grade_programmatic(
    question: Question, student_answer: str
) -> tuple[bool, float, float]:
    """Grade a multiple-choice or true/false answer programmatically.

    Returns:
        (is_correct, score, max_score)
    """
    if question.type == QuestionType.MULTIPLE_CHOICE:
        student = student_answer.strip().upper()
        correct = str(question.correct_answer).strip().upper()
        is_correct = student == correct
        return is_correct, 1.0 if is_correct else 0.0, 1.0

    if question.type == QuestionType.TRUE_FALSE:
        def _to_bool(val: str) -> bool | None:
            v = val.strip().lower()
            if v in ("true", "1", "yes", "对", "正确"):
                return True
            if v in ("false", "0", "no", "错", "错误"):
                return False
            return None

        student_bool = _to_bool(student_answer)
        correct_raw = question.correct_answer
        if isinstance(correct_raw, bool):
            correct_bool = correct_raw
        else:
            correct_bool = _to_bool(str(correct_raw))

        if student_bool is None or correct_bool is None:
            is_correct = False
        else:
            is_correct = student_bool == correct_bool
        return is_correct, 1.0 if is_correct else 0.0, 1.0

    # Fallback — should not reach here
    return False, 0.0, question.points


async def _grade_via_llm(
    llm_client: LLMClient,
    grade_prompt_path: str,
    items: list[tuple[Question, dict]],
    student_id: str,
    now: str,
) -> list[dict[str, Any]]:
    """Send questions + student answers to the LLM for grading.

    Returns a list of result dicts (aligned with ``items``).
    """
    try:
        system_prompt = Path(grade_prompt_path).read_text(encoding="utf-8")
    except OSError as exc:
        raise MaterialError(f"无法加载批改提示词: {grade_prompt_path} ({exc})") from exc

    # Build grading context
    lines: list[str] = []
    for idx, (q, ans) in enumerate(items):
        lines.append(f"## 题目 {idx + 1}")
        lines.append(f"- question_id: {q.question_id}")
        lines.append(f"- type: {q.type.value}")
        lines.append(f"- stem: {q.stem}")
        if q.options:
            lines.append(f"- options: {json.dumps(q.options, ensure_ascii=False)}")
        correct_repr = q.correct_answer
        if isinstance(correct_repr, (dict, list)):
            correct_repr = json.dumps(correct_repr, ensure_ascii=False)
        lines.append(f"- correct_answer (参考答案): {correct_repr}")
        lines.append(f"- max_score (满分): {q.points}")
        lines.append(f"- 学生作答: {ans.get('student_answer', '')}")
        lines.append("")

    user_prompt = "\n".join(lines)

    messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    logger.info("Grading %d items via LLM...", len(items))

    try:
        raw = await llm_client.chat(
            messages=messages,
            temperature=0.1,
            max_tokens=4096,
            timeout=120,
        )
    except LLMError as exc:
        raise MaterialError(f"LLM 批改失败: {exc}") from exc

    raw = _strip_markdown_fence(raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("LLM 批改返回的 JSON 无法解析: %s", raw[:500])
        raise MaterialError(f"批改结果不是有效 JSON: {exc}") from exc

    return data.get("results", [])


def _serialize_answer(answer: Any) -> str:
    """Convert a question's correct_answer to a JSON string for storage."""
    if isinstance(answer, (dict, list)):
        return json.dumps(answer, ensure_ascii=False)
    return str(answer)


async def _persist_attempt(
    db: Database,
    question: Question,
    answer: dict[str, Any],
    student_id: str,
    is_correct: bool,
    score: float,
    max_score: float,
    now: str,
    misconceptions: list[dict] | None = None,
    analysis: str = "",
) -> QuizAttempt:
    """Persist a graded answer as a ``QuizAttempt`` in the database."""
    attempt_id = str(uuid4())

    kp_id = question.kp_id

    record: dict[str, Any] = {
        "attempt_id": attempt_id,
        "student_id": student_id,
        "question_id": question.question_id,
        "kp_id": kp_id,
        "student_answer": answer.get("student_answer"),
        "is_correct": 1 if is_correct else 0,
        "score": score,
        "time_spent_seconds": answer.get("time_spent_seconds", 0),
        "hints_used": answer.get("hints_used", 0),
        "attempt_number": answer.get("attempt_number", 1),
        "confidence": answer.get("confidence"),
        "misconception_ids": json.dumps(
            [m.get("label", "") for m in (misconceptions or [])],
            ensure_ascii=False,
        ),
        "note": analysis or "",
        "started_at": now,
        "submitted_at": now,
        "metadata": json.dumps(
            {
                "max_score": max_score,
                "misconceptions": misconceptions or [],
            },
            ensure_ascii=False,
        ),
    }

    await db.insert_attempt(record)

    return QuizAttempt(
        attempt_id=attempt_id,
        student_id=student_id,
        question_id=question.question_id,
        kp_id=kp_id,
        student_answer=answer.get("student_answer"),
        is_correct=is_correct,
        time_spent_seconds=answer.get("time_spent_seconds", 0),
        started_at=now,
        submitted_at=now,
    )


# ==================================================================
# General helpers
# ==================================================================


def _strip_markdown_fence(raw: str) -> str:
    """Remove Markdown code fences from an LLM response if present."""
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines)
    return raw


def _distribute_counts(kp_ids: list[str], count: int) -> dict[str, int]:
    """Distribute ``count`` items evenly across ``kp_ids``.

    Remainder items are assigned to the first KPs (round-robin).
    """
    n = len(kp_ids)
    if n == 0:
        return {}
    base = count // n
    remainder = count % n
    result: dict[str, int] = {}
    for i, kp_id in enumerate(kp_ids):
        result[kp_id] = base + (1 if i < remainder else 0)
    return result
