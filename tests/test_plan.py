"""Tests for the PlanEngine — topological sort, priority scoring, and plan generation."""

from __future__ import annotations

import pytest

from super_tutor.engine.plan_engine import PlanEngine
from super_tutor.models.plan import StudyPlan


# ============================================================================
# Unit tests — no LLM/DB needed
# ============================================================================


class TestTopologicalSort:
    """Kahn's algorithm topological sort of KP dependency DAG."""

    @staticmethod
    def _kp(kp_id: str, prereq_ids: list[str]) -> dict:
        """Build a minimal KP-like dict for topological sort tests."""
        return {
            "kp_id": kp_id,
            "title": kp_id.upper(),
            "prerequisite_ids": str(prereq_ids).replace("'", '"'),
        }

    def test_linear_chain(self):
        """kp-001 → kp-002 → kp-003  should sort to [kp-001, kp-002, kp-003]."""
        kps = [
            self._kp("kp-001", []),
            self._kp("kp-002", ["kp-001"]),
            self._kp("kp-003", ["kp-002"]),
        ]
        result = PlanEngine.topological_sort(kps)
        assert result == ["kp-001", "kp-002", "kp-003"]

    def test_no_dependencies(self):
        """Three independent KPs should all appear in any order."""
        kps = [
            self._kp("kp-a", []),
            self._kp("kp-b", []),
            self._kp("kp-c", []),
        ]
        result = PlanEngine.topological_sort(kps)
        assert set(result) == {"kp-a", "kp-b", "kp-c"}
        assert len(result) == 3

    def test_diamond_dependency(self):
        """kp-a → kp-b, kp-c → kp-d.  a must be first, d must be last."""
        kps = [
            self._kp("kp-a", []),
            self._kp("kp-b", ["kp-a"]),
            self._kp("kp-c", ["kp-a"]),
            self._kp("kp-d", ["kp-b", "kp-c"]),
        ]
        result = PlanEngine.topological_sort(kps)
        assert result[0] == "kp-a"
        assert result[-1] == "kp-d"
        assert result.index("kp-b") < result.index("kp-d")
        assert result.index("kp-c") < result.index("kp-d")

    def test_cycle_handled_gracefully(self):
        """A cycle should not crash — remaining nodes appended at end."""
        kps = [
            self._kp("kp-x", ["kp-y"]),
            self._kp("kp-y", ["kp-x"]),
        ]
        result = PlanEngine.topological_sort(kps)
        assert set(result) == {"kp-x", "kp-y"}
        assert len(result) == 2

    def test_empty(self):
        """Empty list → empty list."""
        assert PlanEngine.topological_sort([]) == []

    def test_single_kp(self):
        """Single KP with no dependencies."""
        kps = [self._kp("kp-only", [])]
        assert PlanEngine.topological_sort(kps) == ["kp-only"]

    def test_external_prerequisite_ignored(self):
        """Prerequisites not in the KP set are ignored."""
        kps = [
            self._kp("kp-001", []),
            self._kp("kp-002", ["kp-999"]),  # external, not in set
        ]
        result = PlanEngine.topological_sort(kps)
        # kp-002 has no prerequisite in the set → in-degree 0
        assert set(result) == {"kp-001", "kp-002"}
        assert len(result) == 2


class TestComputePriority:
    """Priority score formula: (1 - mastery) × (1 + successor_count / total)."""

    def test_low_mastery_high_priority(self):
        """Unmastered KP with many successors → high priority."""
        score = PlanEngine._compute_priority(
            mastery=0.0, successor_count=5, total_kps=10
        )
        # (1-0) × (1 + 5/10) = 1.0 × 1.5 = 1.5
        assert score == pytest.approx(1.5)

    def test_high_mastery_low_priority(self):
        """Mastered KP with no successors → low priority."""
        score = PlanEngine._compute_priority(
            mastery=1.0, successor_count=0, total_kps=10
        )
        # (1-1) × (1 + 0/10) = 0.0 × 1.0 = 0.0
        assert score == pytest.approx(0.0)

    def test_mid_mastery(self):
        """50% mastery, 2 successors out of 6 → moderate priority."""
        score = PlanEngine._compute_priority(
            mastery=0.5, successor_count=2, total_kps=6
        )
        # (1-0.5) × (1 + 2/6) = 0.5 × 1.333... = 0.6667
        assert score == pytest.approx(0.6667, abs=0.001)

    def test_total_zero(self):
        """Zero total KPs → successor factor defaults to 1.0."""
        score = PlanEngine._compute_priority(
            mastery=0.3, successor_count=3, total_kps=0
        )
        # (1-0.3) × 1.0 = 0.7
        assert score == pytest.approx(0.7)

    def test_many_successors(self):
        """Many successors → priority boosted significantly."""
        score = PlanEngine._compute_priority(
            mastery=0.4, successor_count=9, total_kps=10
        )
        # (1-0.4) × (1 + 9/10) = 0.6 × 1.9 = 1.14
        assert score == pytest.approx(1.14)


class TestActivityForMastery:
    """Activity type assignment based on mastery level."""

    def test_learn_new(self):
        """mastery < 0.3 → learn_new."""
        assert PlanEngine._activity_for_mastery(0.0) == "learn_new"
        assert PlanEngine._activity_for_mastery(0.29) == "learn_new"

    def test_review(self):
        """0.3 ≤ mastery < 0.5 → review."""
        assert PlanEngine._activity_for_mastery(0.3) == "review"
        assert PlanEngine._activity_for_mastery(0.49) == "review"

    def test_practice(self):
        """0.5 ≤ mastery < 0.8 → practice."""
        assert PlanEngine._activity_for_mastery(0.5) == "practice"
        assert PlanEngine._activity_for_mastery(0.79) == "practice"

    def test_quiz(self):
        """mastery ≥ 0.8 → quiz."""
        assert PlanEngine._activity_for_mastery(0.8) == "quiz"
        assert PlanEngine._activity_for_mastery(1.0) == "quiz"


class TestEstimateMinutes:
    """Study time estimation based on difficulty and mastery gap."""

    def test_easy_high_gap(self):
        """Easy KP, large mastery gap → moderate minutes."""
        mins = PlanEngine._estimate_minutes("easy", 1.0)
        # base=20, scaled = 20 * (0.5 + 1.0) = 30
        assert mins == 30

    def test_hard_low_gap(self):
        """Hard KP, small mastery gap → lower minutes."""
        mins = PlanEngine._estimate_minutes("hard", 0.1)
        # base=45, scaled = 45 * (0.5 + 0.1) = 27
        assert mins == 27

    def test_beginner_full_gap(self):
        """Beginner KP, full gap → minimum effort."""
        mins = PlanEngine._estimate_minutes("beginner", 1.0)
        # base=15, scaled = 15 * 1.5 = 22.5 → 22
        assert mins == 22

    def test_expert_full_gap(self):
        """Expert KP, full gap → maximum effort."""
        mins = PlanEngine._estimate_minutes("expert", 1.0)
        # base=60, scaled = 60 * 1.5 = 90
        assert mins == 90

    def test_clamped_to_max(self):
        """Minutes are clamped to 120 max."""
        mins = PlanEngine._estimate_minutes("expert", 1.0)
        # base=60, scaled = 60 * (0.5 + 1.0) = 90 → under 120
        assert mins <= 120

    def test_clamped_to_min(self):
        """Minutes are clamped to 10 min."""
        mins = PlanEngine._estimate_minutes("beginner", 0.0)
        # base=15, scaled = 15 * 0.5 = 7.5 → clamped to 10
        assert mins == 10

    def test_unknown_difficulty(self):
        """Unknown difficulty → default 30 min base."""
        mins = PlanEngine._estimate_minutes("unknown_str", 0.5)
        # base=30, scaled = 30 * (0.5 + 0.5) = 30
        assert mins == 30


# ============================================================================
# Integration tests — require DB
# ============================================================================


class TestGenerate:
    """Full plan generation flow with database persistence."""

    @pytest.mark.asyncio
    async def test_generate_creates_plan(self, test_db):
        """Generate a plan and verify it is persisted to the DB."""
        # Arrange — insert KPs into the database
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        kps_data = [
            {
                "kp_id": "kp-001",
                "material_id": "mat-1",
                "title": "牛顿第一定律",
                "summary": "惯性定律",
                "content": "物体在不受外力作用时...",
                "keywords": '["牛顿", "惯性"]',
                "difficulty": "medium",
                "prerequisite_ids": "[]",
                "successor_ids": '["kp-002"]',
                "created_at": now,
                "updated_at": now,
            },
            {
                "kp_id": "kp-002",
                "material_id": "mat-1",
                "title": "牛顿第二定律",
                "summary": "F=ma",
                "content": "物体的加速度与合外力成正比...",
                "keywords": '["牛顿", "力", "加速度"]',
                "difficulty": "medium",
                "prerequisite_ids": '["kp-001"]',
                "successor_ids": '["kp-003"]',
                "created_at": now,
                "updated_at": now,
            },
            {
                "kp_id": "kp-003",
                "material_id": "mat-1",
                "title": "牛顿第三定律",
                "summary": "作用力与反作用力",
                "content": "作用力与反作用力大小相等...",
                "keywords": '["牛顿", "作用力", "反作用力"]',
                "difficulty": "easy",
                "prerequisite_ids": '["kp-002"]',
                "successor_ids": "[]",
                "created_at": now,
                "updated_at": now,
            },
        ]
        for kp in kps_data:
            await test_db._conn.execute(
                """INSERT INTO knowledge_points
                   (kp_id, material_id, title, summary, content, keywords,
                    difficulty, prerequisite_ids, successor_ids,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    kp["kp_id"], kp["material_id"], kp["title"],
                    kp["summary"], kp["content"], kp["keywords"],
                    kp["difficulty"], kp["prerequisite_ids"],
                    kp["successor_ids"], kp["created_at"], kp["updated_at"],
                ),
            )
        await test_db._conn.commit()

        engine = PlanEngine(test_db)

        # Act
        plan = await engine.generate(
            kp_ids=["kp-001", "kp-002", "kp-003"],
            mastery_map={"kp-001": 0.2, "kp-002": 0.8, "kp-003": 0.5},
            student_id="student-1",
            plan_title="力学复习计划",
            plan_goal="掌握牛顿三定律",
            start_date="2026-06-25",
        )

        # Assert — model
        assert isinstance(plan, StudyPlan)
        assert plan.student_id == "student-1"
        assert plan.title == "力学复习计划"
        assert plan.status == "active"
        assert len(plan.schedule) == 3
        assert plan.kp_sequence == ["kp-001", "kp-002", "kp-003"]

        # Schedule items should be in topological order
        schedule_ids = [item.knowledge_node_id for item in plan.schedule]
        assert schedule_ids[0] == "kp-001"  # prerequisite first
        assert schedule_ids.index("kp-002") < schedule_ids.index("kp-003")

        # Activity types based on mastery
        kp001_item = plan.schedule[0]
        assert kp001_item.activity_type == "learn_new"  # mastery=0.2 < 0.3
        kp002_item = plan.schedule[1]
        assert kp002_item.activity_type == "quiz"       # mastery=0.8 ≥ 0.8
        kp003_item = plan.schedule[2]
        assert kp003_item.activity_type == "practice"   # mastery=0.5 in [0.5, 0.8)

        # Assert — DB persistence
        db_plan = await test_db.get_study_plan(plan.plan_id)
        assert db_plan is not None
        assert db_plan["student_id"] == "student-1"
        assert db_plan["title"] == "力学复习计划"
        assert len(db_plan["kp_sequence"]) == 3

        # kp_sequence entries
        seq = db_plan["kp_sequence"]
        assert seq[0]["kp_id"] == "kp-001"
        assert seq[0]["order"] == 0
        assert seq[0]["activity_type"] == "learn_new"
        assert seq[0]["priority_score"] > 0
        assert seq[1]["kp_id"] == "kp-002"
        assert seq[2]["kp_id"] == "kp-003"

    @pytest.mark.asyncio
    async def test_generate_empty_kp_ids_raises(self, test_db):
        """Empty kp_ids should raise ValueError."""
        engine = PlanEngine(test_db)
        with pytest.raises(ValueError, match="不能为空"):
            await engine.generate([], {}, student_id="s1")

    @pytest.mark.asyncio
    async def test_generate_all_kps_missing_raises(self, test_db):
        """All kp_ids missing from DB should raise ValueError."""
        engine = PlanEngine(test_db)
        with pytest.raises(ValueError, match="均不存在"):
            await engine.generate(
                ["kp-nonexistent-1", "kp-nonexistent-2"],
                {},
                student_id="s1",
            )

    @pytest.mark.asyncio
    async def test_generate_skips_missing_kps(self, test_db):
        """Missing KPs are skipped, valid ones are included."""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        await test_db._conn.execute(
            """INSERT INTO knowledge_points
               (kp_id, material_id, title, summary, content, keywords,
                difficulty, prerequisite_ids, successor_ids,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "kp-real", "mat-1", "Real KP", "summary", "content",
                "[]", "medium", "[]", "[]", now, now,
            ),
        )
        await test_db._conn.commit()

        engine = PlanEngine(test_db)
        plan = await engine.generate(
            ["kp-real", "kp-missing"],
            {"kp-real": 0.6, "kp-missing": 0.0},
            student_id="s1",
        )

        assert len(plan.schedule) == 1
        assert plan.schedule[0].knowledge_node_id == "kp-real"

    @pytest.mark.asyncio
    async def test_generate_default_dates(self, test_db):
        """When no start_date is given, today is used."""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        await test_db._conn.execute(
            """INSERT INTO knowledge_points
               (kp_id, material_id, title, summary, content, keywords,
                difficulty, prerequisite_ids, successor_ids,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "kp-1", "mat-1", "KP 1", "s", "c",
                "[]", "medium", "[]", "[]", now, now,
            ),
        )
        await test_db._conn.commit()

        engine = PlanEngine(test_db)
        plan = await engine.generate(
            ["kp-1"], {"kp-1": 0.5}, student_id="s1",
        )

        today = datetime.now(timezone.utc).date().isoformat()
        assert plan.schedule[0].scheduled_date == today

    @pytest.mark.asyncio
    async def test_generate_priority_order(self, test_db):
        """Low-mastery KPs get higher priority scores."""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        for i in range(3):
            await test_db._conn.execute(
                """INSERT INTO knowledge_points
                   (kp_id, material_id, title, summary, content, keywords,
                    difficulty, prerequisite_ids, successor_ids,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    f"kp-{i}", "mat-1", f"KP {i}", "s", "c",
                    "[]", "medium", "[]", "[]", now, now,
                ),
            )
        await test_db._conn.commit()

        engine = PlanEngine(test_db)
        plan = await engine.generate(
            ["kp-0", "kp-1", "kp-2"],
            {"kp-0": 0.1, "kp-1": 0.5, "kp-2": 0.9},
            student_id="s1",
        )

        # Retrieve from DB to check priority scores
        db_plan = await test_db.get_study_plan(plan.plan_id)
        seq = db_plan["kp_sequence"]
        scores = {e["kp_id"]: e["priority_score"] for e in seq}

        # kp-0 (mastery=0.1) should have highest priority
        assert scores["kp-0"] > scores["kp-1"]
        assert scores["kp-0"] > scores["kp-2"]
        # kp-2 (mastery=0.9) should have lowest priority
        assert scores["kp-2"] < scores["kp-1"]
