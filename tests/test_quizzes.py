"""Tests for QuizEngine — direct engine-level tests."""

from __future__ import annotations

import json
from typing import Any

import pytest

from super_tutor.core.database import Database
from super_tutor.engine.knowledge_engine import KnowledgeEngine
from super_tutor.engine.quiz_engine import QuizEngine
from super_tutor.models.enums import DifficultyLevel, QuestionType
from super_tutor.models.quiz import Question
from tests.conftest import _create_test_material, _insert_test_kp


# ======================================================================
# Minimal fake LLM client
# ======================================================================


class FakeLLMClient:
    """Lightweight test double returning canned JSON responses."""

    def __init__(self) -> None:
        """Initialize the fake LLM with an empty call log."""
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 4096,
        timeout: int = 120,
    ) -> str:
        """Return canned JSON based on user message content.

        Routes: quiz generation keyword → questions JSON,
        grading keyword → results JSON, default → knowledge_points JSON.
        """
        user_msg = ""
        for m in messages:
            if m.get("role") == "user":
                user_msg = m.get("content", "")
                break
        self.calls.append({"user_message": user_msg[:200]})

        # Quiz generation
        if "出题" in user_msg or "请生成" in user_msg:
            return json.dumps({
                "questions": [
                    {
                        "type": "multiple_choice",
                        "difficulty": "medium",
                        "topic": "牛顿第一定律",
                        "stem": "一个物体在光滑水平面上以恒定速度运动，这说明什么？",
                        "options": [
                            {"key": "A", "text": "物体受到平衡力"},
                            {"key": "B", "text": "物体不受任何外力"},
                            {"key": "C", "text": "物体受到恒定的外力"},
                            {"key": "D", "text": "无法判断"},
                        ],
                        "correct_answer": "B",
                        "explanation": "根据牛顿第一定律（惯性定律），光滑水平面意味着没有摩擦力。",
                        "hints": ["想想牛顿第一定律的内容"],
                        "kp_id": "kp-001",
                        "estimated_seconds": 60,
                        "points": 1.0,
                        "tags": ["力学"],
                    },
                ]
            }, ensure_ascii=False)

        # Grading
        if "批改" in user_msg or "学生作答" in user_msg:
            return json.dumps({
                "results": [
                    {
                        "is_correct": True,
                        "score": 1.0,
                        "max_score": 1.0,
                        "misconceptions": [],
                        "analysis": "回答正确。",
                    },
                ]
            }, ensure_ascii=False)

        # Default — knowledge parsing
        return json.dumps({
            "knowledge_points": [
                {
                    "index": 0,
                    "title": "牛顿第一定律",
                    "content": "物体在不受外力作用时，保持静止或匀速直线运动状态。",
                    "summary": "惯性定律",
                    "difficulty": "medium",
                    "keywords": ["牛顿第一定律", "惯性"],
                    "prerequisite_indices": [],
                },
            ]
        }, ensure_ascii=False)


# ======================================================================
# Helper
# ======================================================================


def _make_quiz_engine(db: Database, llm: FakeLLMClient) -> QuizEngine:
    """Factory: create a QuizEngine wired to a test database and fake LLM.

    Also creates the required KnowledgeEngine dependency internally.
    """
    knowledge = KnowledgeEngine(db=db, llm_client=llm)
    return QuizEngine(db=db, llm_client=llm, knowledge_engine=knowledge)


# ======================================================================
# Tests
# ======================================================================


class TestQuizEngine:
    """QuizEngine direct tests."""

    async def test_generate_questions(self, test_db):
        """generate_questions should create and persist questions."""
        mat_id = await _create_test_material(test_db, material_id="mat-qg")
        await _insert_test_kp(
            test_db, kp_id="kp-001", material_id=mat_id,
            title="牛顿第一定律",
        )

        engine = _make_quiz_engine(test_db, FakeLLMClient())
        questions = await engine.generate_questions(
            kp_ids=["kp-001"], count=2, difficulty="medium",
        )

        assert len(questions) >= 1
        q = questions[0]
        assert q.question_id
        assert q.stem
        assert q.type in (QuestionType.MULTIPLE_CHOICE, QuestionType.TRUE_FALSE)

        # Verify persistence
        row = await test_db.get_question(q.question_id)
        assert row is not None
        assert row["stem"] == q.stem

    async def test_generate_questions_empty_kps(self, test_db):
        """generate_questions with empty kp_ids should raise ValueError."""
        engine = _make_quiz_engine(test_db, FakeLLMClient())
        with pytest.raises(ValueError):
            await engine.generate_questions(kp_ids=[], count=1)

    async def test_generate_questions_nonexistent_kps(self, test_db):
        """generate_questions with nonexistent kp_ids should raise ValueError."""
        engine = _make_quiz_engine(test_db, FakeLLMClient())
        with pytest.raises(ValueError, match="None of the given kp_ids exist"):
            await engine.generate_questions(kp_ids=["nonexistent"], count=1)

    async def test_generate_questions_zero_count(self, test_db):
        """generate_questions with count < 1 should raise ValueError."""
        mat_id = await _create_test_material(test_db, material_id="mat-qz")
        await _insert_test_kp(test_db, kp_id="kp-qz", material_id=mat_id)

        engine = _make_quiz_engine(test_db, FakeLLMClient())
        with pytest.raises(ValueError, match="count must be >= 1"):
            await engine.generate_questions(kp_ids=["kp-qz"], count=0)

    async def test_grade_answers_programmatic_mc_correct(self, test_db):
        """grade_answers should correctly grade multiple-choice answers."""
        q = Question(
            question_id="q-mc-1",
            type=QuestionType.MULTIPLE_CHOICE,
            difficulty=DifficultyLevel.MEDIUM,
            stem="测试题",
            options=[
                {"key": "A", "text": "错"},
                {"key": "B", "text": "对"},
            ],
            correct_answer="B",
            kp_id="kp-001",
        )

        engine = _make_quiz_engine(test_db, FakeLLMClient())
        attempts = await engine.grade_answers(
            questions=[q],
            student_answers=[
                {"question_id": "q-mc-1", "student_answer": "B", "time_spent_seconds": 15},
            ],
            student_id="student-1",
        )

        assert len(attempts) == 1
        assert attempts[0].is_correct is True

    async def test_grade_answers_programmatic_mc_wrong(self, test_db):
        """grade_answers should mark wrong multiple-choice answers as incorrect."""
        q = Question(
            question_id="q-mc-2",
            type=QuestionType.MULTIPLE_CHOICE,
            difficulty=DifficultyLevel.EASY,
            stem="测试题2",
            options=[{"key": "A", "text": "错"}, {"key": "B", "text": "对"}],
            correct_answer="B",
            kp_id="kp-001",
        )

        engine = _make_quiz_engine(test_db, FakeLLMClient())
        attempts = await engine.grade_answers(
            questions=[q],
            student_answers=[
                {"question_id": "q-mc-2", "student_answer": "A", "time_spent_seconds": 10},
            ],
            student_id="student-1",
        )

        assert len(attempts) == 1
        assert attempts[0].is_correct is False

    async def test_grade_answers_programmatic_tf_correct(self, test_db):
        """grade_answers should correctly grade true/false answers."""
        q = Question(
            question_id="q-tf-1",
            type=QuestionType.TRUE_FALSE,
            difficulty=DifficultyLevel.EASY,
            stem="判断题",
            correct_answer=False,
            kp_id="kp-001",
        )

        engine = _make_quiz_engine(test_db, FakeLLMClient())
        attempts = await engine.grade_answers(
            questions=[q],
            student_answers=[
                {"question_id": "q-tf-1", "student_answer": "false", "time_spent_seconds": 5},
            ],
        )

        assert len(attempts) == 1
        assert attempts[0].is_correct is True

    async def test_grade_answers_persisted(self, test_db):
        """grade_answers should persist attempts to the database."""
        q = Question(
            question_id="q-persist",
            type=QuestionType.MULTIPLE_CHOICE,
            difficulty=DifficultyLevel.MEDIUM,
            stem="持久化测试",
            options=[{"key": "A", "text": "错"}, {"key": "B", "text": "对"}],
            correct_answer="B",
            kp_id="kp-001",
        )

        engine = _make_quiz_engine(test_db, FakeLLMClient())
        await engine.grade_answers(
            questions=[q],
            student_answers=[
                {"question_id": "q-persist", "student_answer": "B", "time_spent_seconds": 20},
            ],
            student_id="student-persist",
        )

        attempts, _ = await test_db.list_attempts_by_student("student-persist")
        assert len(attempts) >= 1
        assert attempts[0]["is_correct"] == 1
        assert attempts[0]["question_id"] == "q-persist"

    async def test_add_to_wrong_book_new(self, test_db):
        """add_to_wrong_book should create a new wrong-book entry for incorrect answer."""
        from super_tutor.models.quiz import QuizAttempt

        attempt = QuizAttempt(
            attempt_id="att-wb-1",
            question_id="q-wb-1",
            kp_id="kp-wb",
            student_answer="A",
            is_correct=False,
        )
        q = Question(
            question_id="q-wb-1",
            type=QuestionType.MULTIPLE_CHOICE,
            difficulty=DifficultyLevel.MEDIUM,
            stem="错题测试",
            correct_answer="B",
            kp_id="kp-wb",
        )

        engine = _make_quiz_engine(test_db, FakeLLMClient())
        record = await engine.add_to_wrong_book(attempt, q)

        assert record
        assert record["question_id"] == "q-wb-1"
        assert record["attempt_count"] == 1
        assert record["resolution_status"] == "unresolved"

    async def test_add_to_wrong_book_skips_correct(self, test_db):
        """add_to_wrong_book should skip correct attempts."""
        from super_tutor.models.quiz import QuizAttempt

        attempt = QuizAttempt(
            attempt_id="att-correct",
            question_id="q-correct",
            kp_id="kp-x",
            student_answer="B",
            is_correct=True,
        )

        engine = _make_quiz_engine(test_db, FakeLLMClient())
        record = await engine.add_to_wrong_book(attempt)

        assert record == {}
        rows, _ = await test_db.list_wrong_questions_by_student("")
        # No entry should exist for q-correct
        q_ids = [r["question_id"] for r in rows]
        assert "q-correct" not in q_ids

    async def test_add_to_wrong_book_duplicate_increments(self, test_db):
        """add_to_wrong_book should increment attempt_count for repeat wrong answers."""
        from super_tutor.models.quiz import QuizAttempt

        q = Question(
            question_id="q-dup",
            type=QuestionType.MULTIPLE_CHOICE,
            difficulty=DifficultyLevel.MEDIUM,
            stem="重复错题",
            correct_answer="B",
            kp_id="kp-dup",
        )

        engine = _make_quiz_engine(test_db, FakeLLMClient())

        # First wrong attempt
        a1 = QuizAttempt(
            attempt_id="att-dup-1", question_id="q-dup", kp_id="kp-dup",
            student_answer="A", is_correct=False, student_id="student-dup",
        )
        await engine.add_to_wrong_book(a1, q)

        # Second wrong attempt (same question, same student)
        a2 = QuizAttempt(
            attempt_id="att-dup-2", question_id="q-dup", kp_id="kp-dup",
            student_answer="C", is_correct=False, student_id="student-dup",
        )
        record = await engine.add_to_wrong_book(a2, q)

        assert record["attempt_count"] == 2
        # wrong_answer updated to latest (student_answer from second attempt)
