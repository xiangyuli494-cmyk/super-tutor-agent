"""Tests for dashboard / student queries — direct database level."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from tests.conftest import _create_test_material, _insert_test_kp


class TestDashboard:
    """Student dashboard query tests."""

    async def _setup_attempt(self, test_db, student_id="student-1", is_correct=True):
        """Helper: insert a material, KP, question, and attempt."""
        now = datetime.now(timezone.utc).isoformat()
        mat_id = await _create_test_material(test_db, material_id="mat-dash")

        await _insert_test_kp(
            test_db, kp_id="kp-dash", material_id=mat_id,
            title="牛顿第一定律", mastery_level=0.75,
        )

        await test_db.insert_question({
            "question_id": "q-dash",
            "type": "multiple_choice",
            "difficulty": "medium",
            "topic": "牛顿第一定律",
            "stem": "一个冰球在光滑冰面上将做什么运动？",
            "options": json.dumps([
                {"key": "A", "text": "减速"}, {"key": "B", "text": "匀速直线"}
            ]),
            "correct_answer": "B",
            "explanation": "惯性定律",
            "kp_id": "kp-dash",
            "estimated_seconds": 60,
            "points": 1.0,
            "tags": json.dumps(["力学"]),
            "created_at": now,
        })

        await test_db.insert_attempt({
            "attempt_id": f"att-dash-{1 if is_correct else 0}",
            "student_id": student_id,
            "question_id": "q-dash",
            "kp_id": "kp-dash",
            "student_answer": "B",
            "is_correct": 1 if is_correct else 0,
            "score": 1.0,
            "time_spent_seconds": 30,
            "started_at": now,
            "submitted_at": now,
        })

    async def test_list_attempts_empty(self, test_db):
        """list_attempts_by_student should return empty for new student."""
        attempts, total = await test_db.list_attempts_by_student("no-one")
        assert attempts == []
        assert total == 0

    async def test_list_attempts_with_data(self, test_db):
        """list_attempts_by_student should return attempts for a student."""
        await self._setup_attempt(test_db, "student-1", is_correct=True)

        attempts, total = await test_db.list_attempts_by_student("student-1")
        assert total >= 1
        assert len(attempts) >= 1
        assert attempts[0]["student_id"] == "student-1"
        assert attempts[0]["is_correct"] == 1

    async def test_list_attempts_by_kp(self, test_db):
        """list_attempts_by_student with kp_id filter should work."""
        await self._setup_attempt(test_db, "student-2", is_correct=False)

        attempts, total = await test_db.list_attempts_by_student(
            "student-2", kp_id="kp-dash"
        )
        assert total >= 1
        assert all(a["kp_id"] == "kp-dash" for a in attempts)

    async def test_mastery_empty(self, test_db):
        """list_knowledge_points_with_mastery should return empty when no KPs."""
        kps = await test_db.list_knowledge_points_with_mastery()
        assert kps == []

    async def test_mastery_with_data(self, test_db):
        """list_knowledge_points_with_mastery should return KP mastery info."""
        mat_id = await _create_test_material(test_db, material_id="mat-mastery")
        await _insert_test_kp(
            test_db, kp_id="kp-m1", material_id=mat_id,
            title="牛顿第一定律", mastery_level=0.85,
        )
        await _insert_test_kp(
            test_db, kp_id="kp-m2", material_id=mat_id,
            title="牛顿第二定律", mastery_level=0.40,
        )

        kps = await test_db.list_knowledge_points_with_mastery()
        assert len(kps) >= 2
        kp_map = {kp["kp_id"]: kp for kp in kps}
        assert kp_map["kp-m1"]["mastery_level"] == 0.85
        assert kp_map["kp-m2"]["mastery_level"] == 0.40

    async def test_wrong_questions_empty(self, test_db):
        """list_wrong_questions_by_student should return empty for new student."""
        rows, total = await test_db.list_wrong_questions_by_student("no-one")
        assert rows == []
        assert total == 0

    async def test_wrong_questions_with_data(self, test_db):
        """list_wrong_questions_by_student should return wrong-book entries."""
        now = datetime.now(timezone.utc).isoformat()
        await test_db.insert_wrong_question({
            "wrong_id": "wq-test-1",
            "student_id": "student-1",
            "question_id": "q-test-1",
            "kp_id": "kp-test-1",
            "wrong_answer": "错误答案",
            "correct_answer": "正确答案",
            "attempt_count": 2,
            "resolution_status": "unresolved",
            "created_at": now,
            "updated_at": now,
        })

        rows, total = await test_db.list_wrong_questions_by_student("student-1")
        assert total >= 1
        assert rows[0]["wrong_id"] == "wq-test-1"
        assert rows[0]["attempt_count"] == 2
        assert rows[0]["resolution_status"] == "unresolved"

    async def test_wrong_questions_status_filter(self, test_db):
        """list_wrong_questions_by_student with status filter should work."""
        now = datetime.now(timezone.utc).isoformat()
        await test_db.insert_wrong_question({
            "wrong_id": "wq-resolved",
            "student_id": "student-3",
            "question_id": "q-x",
            "kp_id": "kp-x",
            "wrong_answer": "错",
            "correct_answer": "对",
            "attempt_count": 1,
            "resolution_status": "resolved",
            "created_at": now,
            "updated_at": now,
        })
        await test_db.insert_wrong_question({
            "wrong_id": "wq-unresolved",
            "student_id": "student-3",
            "question_id": "q-y",
            "kp_id": "kp-y",
            "wrong_answer": "错",
            "correct_answer": "对",
            "attempt_count": 1,
            "resolution_status": "unresolved",
            "created_at": now,
            "updated_at": now,
        })

        rows, total = await test_db.list_wrong_questions_by_student(
            "student-3", resolution_status="resolved"
        )
        assert total >= 1
        for r in rows:
            assert r["resolution_status"] == "resolved"
