"""Plan Engine — 学习计划生成引擎。

根据诊断评估结果（知识点掌握度映射）生成个性化学习计划，
按依赖关系拓扑排序后，由优先级公式决定学习顺序与活动类型。

Usage::

    engine = PlanEngine(db)
    plan = await engine.generate(
        kp_ids=["kp-001", "kp-002", "kp-003"],
        mastery_map={"kp-001": 0.2, "kp-002": 0.8, "kp-003": 0.5},
        student_id="student-1",
    )
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from super_tutor.core.database import Database
from super_tutor.engine.knowledge_engine import _parse_json_list
from super_tutor.models.mastery import ReviewItem
from super_tutor.models.plan import StudyPlan

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 活动类型 → 掌握度区间映射
# ---------------------------------------------------------------------------

_ACTIVITY_LEARN_NEW = "learn_new"       # mastery < 0.3
_ACTIVITY_REVIEW = "review"              # 0.3 ≤ mastery < 0.5
_ACTIVITY_PRACTICE = "practice"          # 0.5 ≤ mastery < 0.8
_ACTIVITY_QUIZ = "quiz"                  # mastery ≥ 0.8

# ---------------------------------------------------------------------------
# 默认学习时长（分钟）—— 按难度
# ---------------------------------------------------------------------------

_DIFFICULTY_MINUTES: dict[str, int] = {
    "beginner": 15,
    "easy": 20,
    "medium": 30,
    "hard": 45,
    "expert": 60,
}


class PlanEngine:
    """学习计划生成引擎。

    根据知识点掌握度映射，生成拓扑排序后的个性化学习计划。
    每个知识点按掌握度分配活动类型（learn_new / review / practice / quiz）
    和预估学习时长，以 ``StudyPlan`` 模型输出并持久化到数据库。

    Usage::

        engine = PlanEngine(db)
        plan = await engine.generate(kp_ids, mastery_map, student_id="s1")
    """

    def __init__(self, db: Database) -> None:
        """Initialise the plan engine.

        Args:
            db: An initialised ``Database`` instance.
        """
        self._db = db

    # ==================================================================
    # Generate — 生成学习计划
    # ==================================================================

    async def generate(
        self,
        kp_ids: list[str],
        mastery_map: dict[str, float],
        student_id: str = "",
        plan_title: str = "",
        plan_goal: str = "",
        start_date: str = "",
    ) -> StudyPlan:
        """Generate a personalised study plan.

        Workflow:
        1. Fetch KPs from the database and topological-sort by prerequisites.
        2. Compute a priority score for each KP:
           ``(1 - mastery) × (1 + successor_count / total_kps)``
           — lower mastery and more dependents → higher priority.
        3. Assign ``activity_type`` and ``estimated_minutes`` for each KP.
        4. Build a ``StudyPlan`` with ``ReviewItem`` schedule entries.
        5. Persist the plan to the ``study_plans`` table.

        Args:
            kp_ids: Knowledge point IDs to include in the plan.
            mastery_map: Mapping of ``kp_id`` → mastery level (0.0–1.0).
            student_id: The student identifier.
            plan_title: Optional human-readable title.
            plan_goal: Optional learning goal description.
            start_date: ISO 8601 date string (e.g. ``"2026-06-25"``).
                Defaults to today.

        Returns:
            A ``StudyPlan`` model with the populated schedule.

        Raises:
            ValueError: If *kp_ids* is empty.
        """
        if not kp_ids:
            raise ValueError("kp_ids 不能为空")

        # -- 1. Fetch KPs from DB ----------------------------------------------
        kps: list[dict] = []
        for kid in kp_ids:
            row = await self._db.get_knowledge_point(kid)
            if row is None:
                logger.warning("KP %s 不存在，跳过", kid)
                continue
            kps.append(row)

        if not kps:
            raise ValueError("所有指定的 kp_ids 均不存在于数据库中")

        # -- 2. Topological sort -----------------------------------------------
        ordered_ids = self.topological_sort(kps)
        total_kps = len(ordered_ids)

        # Build lookup
        kp_by_id: dict[str, dict] = {kp["kp_id"]: kp for kp in kps}

        # -- 3. Compute priority & build sequence ------------------------------
        if not start_date:
            start_date = datetime.now(timezone.utc).date().isoformat()

        kp_sequence: list[dict] = []
        schedule: list[ReviewItem] = []

        for idx, kid in enumerate(ordered_ids):
            kp = kp_by_id.get(kid, {})
            mastery = mastery_map.get(kid, 0.0)
            successor_ids = _parse_json_list(
                kp.get("successor_ids", "[]")
            )
            successor_count = len(successor_ids)
            difficulty = kp.get("difficulty", "medium")

            # Priority formula
            priority_score = self._compute_priority(
                mastery=mastery,
                successor_count=successor_count,
                total_kps=total_kps,
            )

            # Activity type based on mastery
            activity_type = self._activity_for_mastery(mastery)

            # Estimated minutes based on difficulty & mastery gap
            estimated_minutes = self._estimate_minutes(
                difficulty=difficulty,
                mastery_gap=1.0 - mastery,
            )

            # Scheduled date: one KP per day from start_date
            scheduled_date = (
                datetime.fromisoformat(start_date).date() + timedelta(days=idx)
            ).isoformat()

            # -- kp_sequence entry (DB format) --
            entry = {
                "kp_id": kid,
                "title": kp.get("title", kid),
                "order": idx,
                "priority_score": round(priority_score, 4),
                "mastery": round(mastery, 4),
                "activity_type": activity_type,
                "estimated_minutes": estimated_minutes,
                "scheduled_date": scheduled_date,
                "completed": False,
                "completed_at": None,
                "notes": "",
            }
            kp_sequence.append(entry)

            # -- ReviewItem (model format) --
            schedule.append(
                ReviewItem(
                    item_id=str(uuid4()),
                    knowledge_node_id=kid,
                    scheduled_date=scheduled_date,
                    activity_type=activity_type,
                    estimated_minutes=estimated_minutes,
                    completed=False,
                    notes=f"掌握度={mastery:.2f} 优先级={priority_score:.2f}",
                )
            )

        # -- 4. Build StudyPlan model ------------------------------------------
        now = datetime.now(timezone.utc).isoformat()
        plan = StudyPlan(
            plan_id=str(uuid4()),
            student_id=student_id,
            title=plan_title or "个性化学习计划",
            status="active",
            kp_sequence=ordered_ids.copy(),
            schedule=schedule,
            created_at=now,
            updated_at=now,
        )

        # -- 5. Persist to DB --------------------------------------------------
        await self._db.create_study_plan({
            "plan_id": plan.plan_id,
            "student_id": plan.student_id,
            "title": plan.title,
            "description": f"基于 {total_kps} 个知识点的诊断评估自动生成",
            "goal": plan_goal or "掌握所有知识点，达到 ≥0.8 掌握度",
            "start_date": start_date,
            "end_date": None,
            "status": plan.status,
            "kp_sequence": kp_sequence,
            "metadata": {
                "source": "plan_engine",
                "kp_count": total_kps,
                "generated_at": now,
            },
            "created_at": plan.created_at,
            "updated_at": plan.updated_at,
        })

        logger.info(
            "学习计划已生成: plan_id=%s kps=%d items=%d",
            plan.plan_id,
            total_kps,
            len(schedule),
        )

        return plan

    # ==================================================================
    # Topological Sort
    # ==================================================================

    @staticmethod
    def topological_sort(kps: list[dict]) -> list[str]:
        """Topological sort of KPs by prerequisite dependencies.

        Uses Kahn's algorithm.  KPs with no prerequisites (or
        prerequisites outside the given set) come first; their
        successors follow in dependency order.  Cycles are handled
        gracefully — remaining nodes are appended at the end.

        Args:
            kps: A list of KP dicts, each containing at least
                ``kp_id`` and ``prerequisite_ids`` (JSON string or list).

        Returns:
            Ordered list of kp_ids.
        """
        if not kps:
            return []

        kp_ids = {kp["kp_id"] for kp in kps}

        # Build adjacency list and in-degree map
        adj: dict[str, list[str]] = {k: [] for k in kp_ids}
        in_degree: dict[str, int] = {k: 0 for k in kp_ids}

        for kp in kps:
            kid = kp["kp_id"]
            prereqs = _parse_json_list(kp.get("prerequisite_ids", "[]"))
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

    # ==================================================================
    # Helpers
    # ==================================================================

    @staticmethod
    def _compute_priority(
        mastery: float,
        successor_count: int,
        total_kps: int,
    ) -> float:
        """Compute priority score for a single KP.

        Formula: ``(1 - mastery) × (1 + successor_count / total_kps)``

        - Lower mastery → higher priority (need to learn).
        - More successors → higher priority (blocking other KPs).

        Args:
            mastery: Current mastery level (0.0–1.0).
            successor_count: Number of direct successors.
            total_kps: Total number of KPs in the plan.

        Returns:
            Priority score (0.0–2.0, higher = more urgent).
        """
        mastery_gap = 1.0 - mastery
        successor_factor = 1.0 + (successor_count / total_kps if total_kps > 0 else 0.0)
        return round(mastery_gap * successor_factor, 4)

    @staticmethod
    def _activity_for_mastery(mastery: float) -> str:
        """Determine activity type from mastery level.

        Args:
            mastery: Current mastery level (0.0–1.0).

        Returns:
            One of: ``learn_new``, ``review``, ``practice``, ``quiz``.
        """
        if mastery < 0.3:
            return _ACTIVITY_LEARN_NEW
        elif mastery < 0.5:
            return _ACTIVITY_REVIEW
        elif mastery < 0.8:
            return _ACTIVITY_PRACTICE
        else:
            return _ACTIVITY_QUIZ

    @staticmethod
    def _estimate_minutes(difficulty: str, mastery_gap: float) -> int:
        """Estimate study minutes based on difficulty and mastery gap.

        Args:
            difficulty: One of ``beginner``, ``easy``, ``medium``,
                ``hard``, ``expert``.
            mastery_gap: ``1.0 - mastery`` — how much the student
                still needs to learn.

        Returns:
            Estimated minutes (clamped to 10–120).
        """
        base = _DIFFICULTY_MINUTES.get(difficulty, 30)
        # Scale by mastery gap: gap=1.0 → 1.5× base, gap=0.0 → 0.5× base
        scaled = int(base * (0.5 + mastery_gap))
        return max(10, min(120, scaled))
