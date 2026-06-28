"""Super Tutor — 学习计划排期模型。

定义排期中的复习/学习条目，供 PlanEngine 使用。
"""

from __future__ import annotations

from typing import Optional
from uuid import uuid4

from pydantic import BaseModel, Field


# ============================================================================
# ReviewItem — 排期中的单个复习项
# ============================================================================


class ReviewItem(BaseModel):
    """学习计划中的单个复习/学习条目。

    由 SM-2 算法根据 MasteryRecord 自动生成，也可手动添加。
    """

    item_id: str = Field(
        default_factory=lambda: str(uuid4()),
        description="条目唯一标识",
    )
    knowledge_node_id: str = Field(
        ...,
        description="对应的 KnowledgeNode ID",
    )
    scheduled_date: str = Field(
        ...,
        description="计划日期（ISO 8601 date）",
    )
    activity_type: str = Field(
        default="review",
        description="活动类型：review / learn_new / practice / quiz / rest",
    )
    estimated_minutes: int = Field(
        default=15,
        ge=0,
        description="预计耗时（分钟）",
    )
    completed: bool = Field(
        default=False,
        description="是否已完成",
    )
    completed_at: Optional[str] = Field(
        default=None,
        description="完成时间（ISO 8601）",
    )
    notes: str = Field(
        default="",
        description="备注",
    )

