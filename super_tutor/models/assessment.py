"""Super Tutor — 诊断性评估模型。

定义评估报告结构和单知识点评估结果，
用于 AssessmentEngine 的输出。
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, Field


# ============================================================================
# KPAssessmentResult — 单个知识点的评估结果
# ============================================================================


class KPAssessmentResult(BaseModel):
    """单个知识点在一次诊断性评估中的结果。

    包含题目作答统计、掌握度初始计算值和经前置规则
    调整后的最终掌握度。

    状态标签说明：
    - ``mastered`` — 掌握度 >= 0.8，无需复习
    - ``learning`` — 0.5 < 掌握度 < 0.8，正常学习进度
    - ``need_review`` — 后继正确但此前驱有误，疑似理解不扎实
    - ``need_relearn`` — 掌握度 <= 0.3 or 连续多个后继出错，需重新学习
    """

    kp_id: str = Field(
        ...,
        description="知识点 ID",
    )
    title: str = Field(
        default="",
        description="知识点标题",
    )
    prerequisite_ids: list[str] = Field(
        default_factory=list,
        description="前置知识点 ID 列表",
    )
    successor_ids: list[str] = Field(
        default_factory=list,
        description="后继知识点 ID 列表",
    )
    question_ids: list[str] = Field(
        default_factory=list,
        description="本次评估中该 KP 对应的题目 ID 列表",
    )
    correct_count: int = Field(
        default=0,
        ge=0,
        description="正确作答数",
    )
    total_count: int = Field(
        default=0,
        ge=0,
        description="该 KP 的总题目数",
    )
    accuracy: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="本 KP 的正确率",
    )
    initial_mastery: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="初始掌握度（基于准确率），未经前置规则调整",
    )
    adjusted_mastery: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="经前置规则调整后的掌握度",
    )
    confidence: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="掌握度评估的置信度",
    )
    status: str = Field(
        default="learning",
        description="掌握状态：mastered / learning / need_review / need_relearn",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="前置规则触发的警告消息列表",
    )
    note: str = Field(
        default="",
        description="诊断备注",
    )


# ============================================================================
# AssessmentReport — 一次完整的诊断性评估报告
# ============================================================================


class AssessmentReport(BaseModel):
    """一次完整的诊断性评估报告。

    聚合所有 KPA assessment 结果，提供整体统计和
    按掌握度排序的薄弱点/强项列表。
    """

    assessment_id: str = Field(
        default_factory=lambda: str(uuid4()),
        description="评估报告唯一标识",
    )
    student_id: str = Field(
        default="default",
        description="学生 ID",
    )
    kp_ids: list[str] = Field(
        default_factory=list,
        description="本次评估涉及的知识点 ID 列表（拓扑序）",
    )
    total_questions: int = Field(
        default=0,
        ge=0,
        description="总题目数",
    )
    correct_count: int = Field(
        default=0,
        ge=0,
        description="总正确数",
    )
    accuracy: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="整体正确率",
    )
    kp_results: list[KPAssessmentResult] = Field(
        default_factory=list,
        description="每个知识点的评估结果",
    )
    rules_applied: list[str] = Field(
        default_factory=list,
        description="本次评估中触发的前置规则描述列表",
    )
    weak_kps: list[KPAssessmentResult] = Field(
        default_factory=list,
        description="薄弱知识点（adjusted_mastery <= 0.5），按掌握度升序",
    )
    strong_kps: list[KPAssessmentResult] = Field(
        default_factory=list,
        description="强项知识点（adjusted_mastery >= 0.8），按掌握度降序",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="评估过程中的警告信息（如错题本写入失败等）",
    )
    generated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="评估生成时间（ISO 8601）",
    )

    # -- 计算属性 ------------------------------------------------------------

    @property
    def mastery_distribution(self) -> dict[str, int]:
        """掌握度分布统计。"""
        dist: dict[str, int] = {
            "mastered": 0,
            "learning": 0,
            "need_review": 0,
            "need_relearn": 0,
        }
        for r in self.kp_results:
            dist[r.status] = dist.get(r.status, 0) + 1
        return dist
