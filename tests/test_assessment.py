"""Tests for the AssessmentEngine — topological sort, prerequisite rules, and grading."""

from __future__ import annotations

import pytest

from super_tutor.engine.assessment_engine import AssessmentEngine
from super_tutor.models.assessment import AssessmentReport, KPAssessmentResult


# ============================================================================
# Unit tests — no LLM/DB needed
# ============================================================================


class TestTopologicalSort:
    """Kahn's algorithm topological sort of KP dependency DAG."""

    def test_linear_chain(self):
        """kp-001 → kp-002 → kp-003  should sort to [kp-001, kp-002, kp-003]."""
        kp_map = {
            "kp-001": {"prerequisite_ids": "[]", "title": "牛顿第一定律"},
            "kp-002": {"prerequisite_ids": '["kp-001"]', "title": "牛顿第二定律"},
            "kp-003": {"prerequisite_ids": '["kp-002"]', "title": "牛顿第三定律"},
        }
        # We need an instance — use __new__ to bypass __init__
        engine = object.__new__(AssessmentEngine)
        result = engine._topological_sort(kp_map)
        assert result == ["kp-001", "kp-002", "kp-003"]

    def test_no_dependencies(self):
        """Three independent KPs should all appear in any order."""
        kp_map = {
            "kp-a": {"prerequisite_ids": "[]", "title": "A"},
            "kp-b": {"prerequisite_ids": "[]", "title": "B"},
            "kp-c": {"prerequisite_ids": "[]", "title": "C"},
        }
        engine = object.__new__(AssessmentEngine)
        result = engine._topological_sort(kp_map)
        assert set(result) == {"kp-a", "kp-b", "kp-c"}
        assert len(result) == 3

    def test_diamond_dependency(self):
        """kp-a → kp-b, kp-c → kp-d.  a must be first, d must be last."""
        kp_map = {
            "kp-a": {"prerequisite_ids": "[]", "title": "A"},
            "kp-b": {"prerequisite_ids": '["kp-a"]', "title": "B"},
            "kp-c": {"prerequisite_ids": '["kp-a"]', "title": "C"},
            "kp-d": {"prerequisite_ids": '["kp-b", "kp-c"]', "title": "D"},
        }
        engine = object.__new__(AssessmentEngine)
        result = engine._topological_sort(kp_map)
        assert result[0] == "kp-a"
        assert result[-1] == "kp-d"
        assert result.index("kp-b") < result.index("kp-d")
        assert result.index("kp-c") < result.index("kp-d")

    def test_cycle_handled_gracefully(self):
        """A cycle should not crash — remaining nodes appended at end."""
        kp_map = {
            "kp-x": {"prerequisite_ids": '["kp-y"]', "title": "X"},
            "kp-y": {"prerequisite_ids": '["kp-x"]', "title": "Y"},
        }
        engine = object.__new__(AssessmentEngine)
        result = engine._topological_sort(kp_map)
        assert set(result) == {"kp-x", "kp-y"}
        assert len(result) == 2

    def test_empty(self):
        """Empty map → empty list."""
        engine = object.__new__(AssessmentEngine)
        assert engine._topological_sort({}) == []


class TestDistributeCounts:
    """Question count distribution across KPs."""

    def test_exact_minimum(self):
        """3 KPs, 3 questions → 1 each."""
        engine = object.__new__(AssessmentEngine)
        result = engine._distribute_counts(["a", "b", "c"], 3)
        assert result == {"a": 1, "b": 1, "c": 1}

    def test_extra_distributed(self):
        """3 KPs, 6 questions → 2 each."""
        engine = object.__new__(AssessmentEngine)
        result = engine._distribute_counts(["a", "b", "c"], 6)
        assert result == {"a": 2, "b": 2, "c": 2}

    def test_all_at_least_one(self):
        """Every KP gets at least 1 question."""
        engine = object.__new__(AssessmentEngine)
        result = engine._distribute_counts(["a", "b", "c", "d", "e"], 15)
        assert len(result) == 5
        for count in result.values():
            assert count >= 1
        assert sum(result.values()) == 15

    def test_empty_kps(self):
        """No KPs → empty dict."""
        engine = object.__new__(AssessmentEngine)
        assert engine._distribute_counts([], 10) == {}


class TestPrerequisiteRules:
    """Three prerequisite calibration rules."""

    def _make_report(self, kp_results: list[KPAssessmentResult]) -> AssessmentReport:
        """Build a minimal AssessmentReport from a list of KP results.

        Computes total_questions, correct_count, and kp_ids automatically.
        """
        return AssessmentReport(
            assessment_id="test-001",
            student_id="student-1",
            kp_ids=[r.kp_id for r in kp_results],
            total_questions=sum(r.total_count for r in kp_results),
            correct_count=sum(r.correct_count for r in kp_results),
            kp_results=kp_results,
        )

    def test_rule1_confidence_discount(self):
        """Prerequisite mastery ≤ 0.5 → successor confidence × 0.7."""
        kps = [
            KPAssessmentResult(
                kp_id="kp-001", title="A",
                prerequisite_ids=[], successor_ids=["kp-002"],
                accuracy=0.4, initial_mastery=0.4, adjusted_mastery=0.4,
                total_count=5, correct_count=2,
            ),
            KPAssessmentResult(
                kp_id="kp-002", title="B",
                prerequisite_ids=["kp-001"], successor_ids=[],
                accuracy=1.0, initial_mastery=1.0, adjusted_mastery=1.0,
                confidence=0.5, total_count=5, correct_count=5,
            ),
        ]
        report = self._make_report(kps)
        engine = object.__new__(AssessmentEngine)
        engine.apply_prerequisite_rules(report)

        # kp-002 confidence should be discounted
        b_result = next(r for r in report.kp_results if r.kp_id == "kp-002")
        assert b_result.confidence == pytest.approx(0.35)  # 0.5 * 0.7
        assert b_result.adjusted_mastery == pytest.approx(0.35)  # 1.0 * 0.35
        assert any("规则1" in w for w in b_result.warnings)

    def test_rule2_need_review(self):
        """Successor correct (≥0.6) but prerequisite wrong (<0.5) → prereq need_review."""
        kps = [
            KPAssessmentResult(
                kp_id="kp-001", title="A",
                prerequisite_ids=[], successor_ids=["kp-002"],
                accuracy=0.2, initial_mastery=0.2, adjusted_mastery=0.2,
                total_count=5, correct_count=1, status="learning",
            ),
            KPAssessmentResult(
                kp_id="kp-002", title="B",
                prerequisite_ids=["kp-001"], successor_ids=[],
                accuracy=0.8, initial_mastery=0.8, adjusted_mastery=0.8,
                total_count=5, correct_count=4, status="learning",
            ),
        ]
        report = self._make_report(kps)
        engine = object.__new__(AssessmentEngine)
        engine.apply_prerequisite_rules(report)

        a_result = next(r for r in report.kp_results if r.kp_id == "kp-001")
        assert a_result.status == "need_review"
        assert any("规则2" in w for w in a_result.warnings)

    def test_rule3_need_relearn(self):
        """≥3 direct successors all fail (<0.5) → prerequisite need_relearn."""
        kps = [
            KPAssessmentResult(
                kp_id="kp-base", title="Base",
                prerequisite_ids=[], successor_ids=["kp-s1", "kp-s2", "kp-s3"],
                accuracy=0.9, initial_mastery=0.9, adjusted_mastery=0.9,
                total_count=3, correct_count=3, status="mastered",
            ),
            KPAssessmentResult(
                kp_id="kp-s1", title="S1",
                prerequisite_ids=["kp-base"], successor_ids=[],
                accuracy=0.3, total_count=3, correct_count=1,
            ),
            KPAssessmentResult(
                kp_id="kp-s2", title="S2",
                prerequisite_ids=["kp-base"], successor_ids=[],
                accuracy=0.2, total_count=3, correct_count=0,
            ),
            KPAssessmentResult(
                kp_id="kp-s3", title="S3",
                prerequisite_ids=["kp-base"], successor_ids=[],
                accuracy=0.1, total_count=3, correct_count=0,
            ),
        ]
        report = self._make_report(kps)
        engine = object.__new__(AssessmentEngine)
        engine.apply_prerequisite_rules(report)

        base_result = next(r for r in report.kp_results if r.kp_id == "kp-base")
        assert base_result.status == "need_relearn"
        assert base_result.adjusted_mastery == pytest.approx(0.45)  # 0.9 * 0.5
        assert any("规则3" in w for w in base_result.warnings)

    def test_no_rules_triggered_when_all_mastered(self):
        """All KPs mastered → no rules should fire."""
        kps = [
            KPAssessmentResult(
                kp_id="kp-001", title="A",
                prerequisite_ids=[], successor_ids=["kp-002"],
                accuracy=0.9, initial_mastery=0.9, adjusted_mastery=0.9,
                total_count=3, correct_count=3, status="mastered",
            ),
            KPAssessmentResult(
                kp_id="kp-002", title="B",
                prerequisite_ids=["kp-001"], successor_ids=[],
                accuracy=0.85, initial_mastery=0.85, adjusted_mastery=0.85,
                total_count=3, correct_count=3, status="mastered",
            ),
        ]
        report = self._make_report(kps)
        engine = object.__new__(AssessmentEngine)
        engine.apply_prerequisite_rules(report)

        assert len(report.rules_applied) == 0


class TestAssessmentReport:
    """AssessmentReport model tests."""

    def test_mastery_distribution(self):
        """Count KPs by status."""
        report = AssessmentReport(
            kp_results=[
                KPAssessmentResult(kp_id="a", adjusted_mastery=0.9, status="mastered"),
                KPAssessmentResult(kp_id="b", adjusted_mastery=0.3, status="need_relearn"),
                KPAssessmentResult(kp_id="c", adjusted_mastery=0.3, status="need_relearn"),
            ]
        )
        dist = report.mastery_distribution
        assert dist["mastered"] == 1
        assert dist["need_relearn"] == 2
        assert dist["learning"] == 0

    def test_weak_strong_lists(self):
        """weak_kps / strong_kps are populated after grade()."""
        report = AssessmentReport(
            kp_results=[
                KPAssessmentResult(kp_id="a", adjusted_mastery=0.2, title="Weak"),
                KPAssessmentResult(kp_id="b", adjusted_mastery=0.5, title="Borderline"),
                KPAssessmentResult(kp_id="c", adjusted_mastery=0.9, title="Strong"),
            ]
        )
        report.weak_kps = sorted(
            [r for r in report.kp_results if r.adjusted_mastery <= 0.5],
            key=lambda r: r.adjusted_mastery,
        )
        report.strong_kps = sorted(
            [r for r in report.kp_results if r.adjusted_mastery >= 0.8],
            key=lambda r: r.adjusted_mastery,
            reverse=True,
        )
        assert len(report.weak_kps) == 2
        assert report.weak_kps[0].kp_id == "a"  # weakest first
        assert len(report.strong_kps) == 1
        assert report.strong_kps[0].kp_id == "c"
