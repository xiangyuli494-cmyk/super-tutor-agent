"""Assessment Engine — 诊断性评估引擎。

基于知识点依赖链生成诊断性题目，逐题批改，计算每 KP 掌握度，
并应用前置规则调整置信度与状态标签。

Usage::

    engine = AssessmentEngine(db, llm_client)
    questions = await engine.generate(["kp-001", "kp-002", "kp-003"])
    report = await engine.grade(questions, student_answers)
"""

from __future__ import annotations

import json
import logging
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from super_tutor.core.database import Database
from super_tutor.core.llm_client import LLMClient
from super_tutor.engine.knowledge_engine import KnowledgeEngine, _parse_json_list
from super_tutor.engine.quiz_engine import QuizEngine
from super_tutor.models.assessment import AssessmentReport, KPAssessmentResult
from super_tutor.models.enums import DifficultyLevel, QuestionType
from super_tutor.models.quiz import Question

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 默认 prompt 路径
# ---------------------------------------------------------------------------
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_DEFAULT_ASSESSMENT_PROMPT = _PROMPTS_DIR / "assessment.md"


class AssessmentEngine:
    """诊断性评估引擎。

    按知识点依赖链（前驱 → 后继）生成诊断性题目，
    批改后计算每个 KP 的掌握度，并应用三条前置规则
    对评估结果进行校准。

    Usage::

        engine = AssessmentEngine(db, llm_client)
        questions = await engine.generate(["kp-001", "kp-002", "kp-003"])
        report = await engine.grade(questions, student_answers)
    """

    def __init__(
        self,
        db: Database,
        llm_client: LLMClient,
        knowledge_engine: KnowledgeEngine | None = None,
        quiz_engine: QuizEngine | None = None,
        assessment_prompt_path: str | None = None,
    ) -> None:
        """Initialise the assessment engine.

        Args:
            db: An initialised ``Database`` instance.
            llm_client: An ``LLMClient`` instance for LLM calls.
            knowledge_engine: Optional pre-built ``KnowledgeEngine``.
                If omitted, one is created from *db* + *llm_client*.
            quiz_engine: Optional pre-built ``QuizEngine``.
                If omitted, one is created from *db* + *llm_client* +
                *knowledge_engine*.
            assessment_prompt_path: Optional path to a custom assessment
                prompt template.  Defaults to ``prompts/assessment.md``.
        """
        self._db = db
        self._llm = llm_client
        self._knowledge_engine = knowledge_engine or KnowledgeEngine(
            db=db, llm_client=llm_client
        )
        self._quiz_engine = quiz_engine or QuizEngine(
            db=db,
            llm_client=llm_client,
            knowledge_engine=self._knowledge_engine,
        )
        self._prompt_path = assessment_prompt_path or str(
            _DEFAULT_ASSESSMENT_PROMPT
        )

    # ==================================================================
    # Generate — 生成诊断性评估题目
    # ==================================================================

    async def generate(
        self,
        kp_ids: list[str],
        student_id: str = "",
        question_count: int = 15,
    ) -> list[Question]:
        """Generate diagnostic assessment questions for a KP chain.

        Each KP receives at least 1 question.  Questions are ordered
        from prerequisites to successors, forming a progressive
        assessment that can detect prerequisite gaps.

        Args:
            kp_ids: Knowledge point IDs to assess.  Must be non-empty.
            student_id: Student identifier (unused in generation, but
                stored in metadata).
            question_count: Total number of questions.  Must be
                >= len(*kp_ids*).

        Returns:
            A list of ``Question`` objects in topological (dependency) order.

        Raises:
            ValueError: If *kp_ids* is empty or *question_count* is
                less than the number of KPs.
        """
        if not kp_ids:
            raise ValueError("kp_ids 不能为空")
        if question_count < len(kp_ids):
            raise ValueError(
                f"question_count ({question_count}) 不能少于 "
                f"知识点数量 ({len(kp_ids)})"
            )

        # -- 1. Fetch KPs & topological sort ---------------------------------
        kp_map: dict[str, dict] = {}
        for kid in kp_ids:
            row = await self._db.get_knowledge_point(kid)
            if row is None:
                logger.warning("KP %s 不存在，跳过", kid)
                continue
            kp_map[kid] = row

        if not kp_map:
            raise ValueError("所有指定的 kp_ids 均不存在于数据库中")

        ordered_ids = self._topological_sort(kp_map)

        # -- 2. Distribute question counts ------------------------------------
        per_kp_count = self._distribute_counts(ordered_ids, question_count)

        # -- 3. Build KP context for LLM --------------------------------------
        kp_context_parts: list[str] = []
        for i, kid in enumerate(ordered_ids):
            kp = kp_map[kid]
            prereqs = _parse_json_list(kp.get("prerequisite_ids", "[]"))
            succs = _parse_json_list(kp.get("successor_ids", "[]"))
            prereqs_in_scope = [p for p in prereqs if p in kp_map]
            succs_in_scope = [s for s in succs if s in kp_map]

            prereq_titles = []
            for pid in prereqs_in_scope:
                pkp = kp_map.get(pid, {})
                prereq_titles.append(pkp.get("title", pid))

            kp_context_parts.append(
                f"### KP {i + 1}: {kp.get('title', kid)}\n"
                f"- kp_id: {kid}\n"
                f"- 难度: {kp.get('difficulty', 'medium')}\n"
                f"- 内容: {kp.get('content', '')}\n"
                f"- 摘要: {kp.get('summary', '')}\n"
                f"- 前置知识点: {', '.join(prereq_titles) if prereq_titles else '无（链首）'}\n"
                f"- 后继知识点数量: {len(succs_in_scope)}\n"
                f"- 需要出题数量: {per_kp_count.get(kid, 1)} 道\n"
            )

        # -- 4. Load system prompt & call LLM ---------------------------------
        try:
            system_prompt = Path(self._prompt_path).read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(
                f"无法加载评估提示词: {self._prompt_path} ({exc})"
            ) from exc

        user_prompt = (
            "# 知识点链（前驱 → 后继，按依赖关系排列）\n\n"
            + "\n".join(kp_context_parts)
            + f"\n\n请生成 {question_count} 道诊断性评估题目，"
            f"确保每个知识点至少有 {min(per_kp_count.values()) if per_kp_count else 1} 道题，"
            f"按知识点依赖关系从基础到高级递进。"
        )

        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        logger.info(
            "开始生成诊断性评估题目 (kp_count=%d, question_count=%d)",
            len(ordered_ids),
            question_count,
        )

        raw = await self._llm.chat(
            messages=messages,
            temperature=0.7,
            max_tokens=8192,
            timeout=180,
        )

        # -- 5. Parse LLM response --------------------------------------------
        raw = self._strip_markdown_fence(raw)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.error("LLM 返回的评估题目 JSON 无法解析: %s", raw[:500])
            raise RuntimeError(
                f"评估题目生成失败 — 无法解析 JSON: {exc}"
            ) from exc

        question_dicts = data.get("assessment_questions", data.get("questions", []))
        if not question_dicts:
            raise RuntimeError("LLM 未返回任何评估题目")

        # -- 6. Build Question models & persist -------------------------------
        questions: list[Question] = []
        now = datetime.now(timezone.utc).isoformat()
        for qd in question_dicts:
            qid = qd.get("question_id") or str(uuid4())
            q = Question(
                question_id=qid,
                type=QuestionType(qd.get("type", "multiple_choice")),
                difficulty=DifficultyLevel(qd.get("difficulty", "medium")),
                subject="",
                topic=qd.get("topic", ""),
                stem=qd.get("stem", ""),
                options=qd.get("options", []),
                correct_answer=qd.get("correct_answer", ""),
                explanation=qd.get("explanation", ""),
                hints=qd.get("hints", []),
                kp_id=qd.get("kp_id", ""),
                kp_context=json.dumps({
                    "diagnostic_tags": qd.get("diagnostic_tags", []),
                    "assessment_generated": True,
                    "student_id": student_id,
                }, ensure_ascii=False),
                estimated_seconds=qd.get("estimated_seconds", 120),
                points=float(qd.get("points", 1.0)),
                tags=qd.get("tags", []),
                metadata={
                    "source": "assessment_engine",
                    "generated_at": now,
                },
                created_at=now,
            )
            await self._db.insert_question(q.model_dump())
            questions.append(q)

        logger.info("已生成 %d 道评估题目", len(questions))
        return questions

    # ==================================================================
    # Grade — 批改 + 掌握度计算 + 前置规则
    # ==================================================================

    async def grade(
        self,
        questions: list[Question],
        student_answers: list[dict],
        student_id: str = "",
    ) -> AssessmentReport:
        """Grade assessment answers and produce a mastery report.

        Each answer is graded via the QuizEngine, then results are
        aggregated per knowledge point.  Mastery levels are computed
        and calibrated through prerequisite rules.

        Args:
            questions: The ``Question`` objects that were presented.
            student_answers: A list of ``{"question_id": str,
                "student_answer": Any, "time_spent_seconds": int}``
                dicts, one per answer submitted.
            student_id: The student being assessed.

        Returns:
            An ``AssessmentReport`` with per-KP results, warnings,
            and overall statistics.
        """
        if not questions:
            raise ValueError("questions 不能为空")
        if not student_answers:
            raise ValueError("student_answers 不能为空")

        # -- 1. Delegate per-question grading to QuizEngine -------------------
        attempts = await self._quiz_engine.grade_answers(
            questions=questions,
            student_answers=student_answers,
            student_id=student_id,
        )

        # -- 1a. Persist wrong answers to wrong book --------------------------
        q_map = {q.question_id: q for q in questions}
        wrong_book_failures: list[str] = []
        for attempt in attempts:
            if attempt.is_correct is False:
                try:
                    await self._quiz_engine.add_to_wrong_book(
                        attempt, q_map.get(attempt.question_id)
                    )
                except Exception:
                    logger.warning(
                        "Failed to add assessment wrong-book entry for %s",
                        attempt.question_id,
                        exc_info=True,
                    )
                    wrong_book_failures.append(attempt.question_id)

        # -- 2. Group attempts by kp_id ---------------------------------------
        kp_attempts: dict[str, list] = {}
        for attempt in attempts:
            kp_id = attempt.kp_id or q_map.get(attempt.question_id, Question()).kp_id
            if not kp_id:
                kp_id = "__unknown__"
            kp_attempts.setdefault(kp_id, []).append(attempt)

        # -- 3. Build KP assessment results -----------------------------------
        kp_results: list[KPAssessmentResult] = []
        # Determine KP order from the questions' kp_ids (topological)
        seen_kps: dict[str, None] = {}
        ordered_kp_ids: list[str] = []
        for q in questions:
            kp_id = q.kp_id or "__unknown__"
            if kp_id not in seen_kps:
                seen_kps[kp_id] = None
                ordered_kp_ids.append(kp_id)

        for kp_id in ordered_kp_ids:
            kp_att = kp_attempts.get(kp_id, [])
            correct = sum(1 for a in kp_att if a.is_correct)
            total = len(kp_att)
            accuracy = round(correct / total, 4) if total > 0 else 0.0

            # Fetch KP info for title and prerequisite/successor IDs
            kp_row = await self._db.get_knowledge_point(kp_id) if kp_id != "__unknown__" else None
            title = kp_row.get("title", kp_id) if kp_row else kp_id
            prereq_ids = _parse_json_list(
                kp_row.get("prerequisite_ids", "[]")
            ) if kp_row else []
            succ_ids = _parse_json_list(
                kp_row.get("successor_ids", "[]")
            ) if kp_row else []

            # Initial mastery = accuracy (simple model for assessment)
            initial_mastery = accuracy

            kp_results.append(
                KPAssessmentResult(
                    kp_id=kp_id,
                    title=title,
                    prerequisite_ids=prereq_ids,
                    successor_ids=succ_ids,
                    question_ids=[a.question_id for a in kp_att],
                    correct_count=correct,
                    total_count=total,
                    accuracy=accuracy,
                    initial_mastery=initial_mastery,
                    adjusted_mastery=initial_mastery,  # calibrated below
                )
            )

        # -- 4. Build preliminary report --------------------------------------
        correct_total = sum(r.correct_count for r in kp_results)
        question_total = sum(r.total_count for r in kp_results)

        warnings: list[str] = []
        if wrong_book_failures:
            warnings.append(
                f"⚠️ {len(wrong_book_failures)} 道错题未能录入错题本: "
                + ", ".join(fid[:8] for fid in wrong_book_failures)
            )

        report = AssessmentReport(
            assessment_id=str(uuid4()),
            student_id=student_id,
            kp_ids=ordered_kp_ids,
            total_questions=question_total,
            correct_count=correct_total,
            accuracy=round(correct_total / question_total, 4) if question_total > 0 else 0.0,
            kp_results=kp_results,
            warnings=warnings,
        )

        # -- 5. Apply prerequisite rules --------------------------------------
        self.apply_prerequisite_rules(report)

        # -- 6. Populate weak/strong KP lists ---------------------------------
        report.weak_kps = sorted(
            [r for r in report.kp_results if r.adjusted_mastery <= 0.5],
            key=lambda r: r.adjusted_mastery,
        )
        report.strong_kps = sorted(
            [r for r in report.kp_results if r.adjusted_mastery >= 0.8],
            key=lambda r: r.adjusted_mastery,
            reverse=True,
        )

        logger.info(
            "评估完成: %d KPs, 整体正确率 %.1f%%, weak=%d strong=%d rules=%d",
            len(kp_results),
            report.accuracy * 100,
            len(report.weak_kps),
            len(report.strong_kps),
            len(report.rules_applied),
        )

        return report

    # ==================================================================
    # Prerequisite Rules
    # ==================================================================

    def apply_prerequisite_rules(self, report: AssessmentReport) -> None:
        """Apply three prerequisite calibration rules to the report.

        Modifies *report* in-place:

        **Rule 1 — Confidence Discount**
        如果前驱知识点掌握度 ≤ 0.5，其后继知识点的置信度乘以 0.7。
        这反映了一个事实：后继答对可能是猜测，因为前驱基础尚未牢固。

        **Rule 2 — Need Review**
        如果后继知识点答对了但前驱知识点答错了，
        将前驱标记为 ``need_review``。
        这识别了"看似理解但不扎实"的情况。

        **Rule 3 — Need Relearn**
        如果某个知识点的 ≥3 个直接后继都答错了，
        将该前驱标记为 ``need_relearn``。
        这说明前驱知识的教学可能存在问题。

        Args:
            report: The ``AssessmentReport`` to calibrate in-place.
        """
        if not report.kp_results:
            return

        # Build lookup by kp_id
        kp_by_id: dict[str, KPAssessmentResult] = {
            r.kp_id: r for r in report.kp_results
        }

        rules_applied: list[str] = []

        # ---- Rule 1: Confidence Discount -----------------------------------
        for r in report.kp_results:
            for prereq_id in r.prerequisite_ids:
                prereq = kp_by_id.get(prereq_id)
                if prereq is None:
                    continue
                if prereq.adjusted_mastery <= 0.5:
                    old_confidence = r.confidence
                    r.confidence = round(r.confidence * 0.7, 4)
                    r.adjusted_mastery = round(
                        r.initial_mastery * r.confidence, 4
                    )
                    msg = (
                        f"规则1: [{r.kp_id}] {r.title} 的前驱 "
                        f"[{prereq_id}] {prereq.title} 掌握度={prereq.adjusted_mastery:.2f}≤0.5，"
                        f"置信度 {old_confidence}→{r.confidence}，"
                        f"调整后掌握度={r.adjusted_mastery:.2f}"
                    )
                    r.warnings.append(msg)
                    rules_applied.append(msg)
                    logger.info(msg)

        # ---- Rule 2: Need Review — successor correct but prerequisite wrong
        for r in report.kp_results:
            if r.accuracy >= 0.6 and r.status not in ("need_review", "need_relearn"):
                # This KP did well; check its prerequisites
                for prereq_id in r.prerequisite_ids:
                    prereq = kp_by_id.get(prereq_id)
                    if prereq is None:
                        continue
                    if prereq.accuracy < 0.5 and prereq.status not in (
                        "need_review",
                        "need_relearn",
                    ):
                        prereq.status = "need_review"
                        msg = (
                            f"规则2: [{prereq_id}] {prereq.title} "
                            f"准确率={prereq.accuracy:.2f}<0.5 但后继 "
                            f"[{r.kp_id}] {r.title} 准确率={r.accuracy:.2f}≥0.6，"
                            f"标记前驱为 need_review"
                        )
                        prereq.warnings.append(msg)
                        rules_applied.append(msg)
                        logger.info(msg)

        # ---- Rule 3: Need Relearn — ≥3 direct successors all wrong ----------
        for r in report.kp_results:
            failed_successors: list[KPAssessmentResult] = []
            for succ_id in r.successor_ids:
                succ = kp_by_id.get(succ_id)
                if succ is not None and succ.accuracy < 0.5:
                    failed_successors.append(succ)

            if len(failed_successors) >= 3:
                r.status = "need_relearn"
                r.adjusted_mastery = round(r.adjusted_mastery * 0.5, 4)
                succ_labels = ", ".join(
                    f"[{s.kp_id}] {s.title} (准确率={s.accuracy:.2f})"
                    for s in failed_successors
                )
                msg = (
                    f"规则3: [{r.kp_id}] {r.title} 的 "
                    f"{len(failed_successors)} 个后继均答错 → "
                    f"标记为 need_relearn，掌握度折半至 {r.adjusted_mastery:.2f}。"
                    f"失败后继: {succ_labels}"
                )
                r.warnings.append(msg)
                rules_applied.append(msg)
                logger.info(msg)

        # ---- Final status assignment for unlabelled KPs ---------------------
        for r in report.kp_results:
            if r.status not in (
                "need_review",
                "need_relearn",
                "mastered",
                "learning",
            ):
                if r.adjusted_mastery >= 0.8:
                    r.status = "mastered"
                elif r.adjusted_mastery >= 0.5:
                    r.status = "learning"
                else:
                    r.status = "need_relearn"

        report.rules_applied = rules_applied

    # ==================================================================
    # Helpers
    # ==================================================================

    def _topological_sort(self, kp_map: dict[str, dict]) -> list[str]:
        """Kahn's algorithm topological sort of KPs by prerequisites.

        KPs with no prerequisites (or prerequisites outside the set)
        come first; their successors follow in dependency order.

        Args:
            kp_map: Mapping of kp_id → DB row dict (must contain
                ``prerequisite_ids`` as JSON string or list).

        Returns:
            Ordered list of kp_ids.
        """
        kp_ids = set(kp_map.keys())

        # Build adjacency list and in-degree map
        adj: dict[str, list[str]] = {k: [] for k in kp_ids}
        in_degree: dict[str, int] = {k: 0 for k in kp_ids}

        for kid in kp_ids:
            prereqs = _parse_json_list(kp_map[kid].get("prerequisite_ids", "[]"))
            for pid in prereqs:
                if pid in kp_ids and pid != kid:
                    adj.setdefault(pid, []).append(kid)
                    in_degree[kid] = in_degree.get(kid, 0) + 1

        # Kahn's algorithm
        queue: deque[str] = deque(
            k for k in kp_ids if in_degree.get(k, 0) == 0
        )
        result: list[str] = []

        while queue:
            node = queue.popleft()
            result.append(node)
            for neighbor in adj.get(node, []):
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        # Append remaining nodes (cycle or orphan references)
        for k in kp_ids:
            if k not in result:
                result.append(k)

        return result

    @staticmethod
    def _distribute_counts(
        ordered_ids: list[str],
        total: int,
    ) -> dict[str, int]:
        """Distribute *total* questions across KPs, min 1 each.

        Args:
            ordered_ids: KPs in topological order.
            total: Total number of questions.

        Returns:
            Mapping of kp_id → question count.
        """
        n = len(ordered_ids)
        if n == 0:
            return {}

        per_kp: dict[str, int] = {k: 1 for k in ordered_ids}
        remaining = total - n

        # Distribute remaining questions evenly, weighted slightly toward
        # later KPs in the chain (which need more diagnostic depth)
        for i in range(remaining):
            # Round-robin starting from the end (successors get extra)
            idx = i % n
            per_kp[ordered_ids[idx]] += 1

        return per_kp

    @staticmethod
    def _strip_markdown_fence(raw: str) -> str:
        """Remove ``` fences from an LLM JSON response."""
        raw = raw.strip()
        if raw.startswith("```"):
            lines = raw.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            raw = "\n".join(lines)
        return raw.strip()
