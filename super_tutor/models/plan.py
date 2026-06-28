"""Super Tutor — 学习计划模型。

定义基于知识点拓扑排序的个性化学习计划，
包含排期条目和进度追踪。
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, Field

from super_tutor.models.mastery import ReviewItem


# ============================================================================
# StudyPlan — 学习计划
# ============================================================================


class StudyPlan(BaseModel):
    """基于诊断评估生成的学习计划。

    包含拓扑排序后的知识点序列和按日排期的学习/复习条目。
    学生完成条目后更新对应掌握度，系统据此动态调整后续计划。
    """

    plan_id: str = Field(
        default_factory=lambda: str(uuid4()),
        description="计划唯一标识",
    )
    student_id: str = Field(
        ...,
        description="学生 ID",
    )
    title: str = Field(
        ...,
        min_length=1,
        max_length=256,
        description="计划标题，如'高二物理力学复习计划'",
    )
    status: str = Field(
        default="active",
        description="计划状态：draft / active / completed / paused / archived",
    )
    kp_sequence: list[str] = Field(
        default_factory=list,
        description="拓扑排序后的知识点 ID 序列（前驱 → 后继）",
    )
    schedule: list[ReviewItem] = Field(
        default_factory=list,
        description="排期条目列表（按日期升序）",
    )
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="创建时间（ISO 8601）",
    )
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="最后更新时间（ISO 8601）",
    )

    # -- 统计 ---------------------------------------------------------------

    @property
    def item_count(self) -> int:
        """排期条目总数。"""
        return len(self.schedule)

    @property
    def completed_count(self) -> int:
        """已完成条目数。"""
        return sum(1 for it in self.schedule if it.completed)

    @property
    def progress(self) -> float:
        """完成进度（0-1），空计划返回 0.0。"""
        if not self.schedule:
            return 0.0
        return round(self.completed_count / len(self.schedule), 4)
