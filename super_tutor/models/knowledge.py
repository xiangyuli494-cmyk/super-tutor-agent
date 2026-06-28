"""Super Tutor — 知识点模型。

定义从教材中提取的独立知识点，包含前后置依赖关系和掌握程度追踪。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from super_tutor.models.enums import DifficultyLevel


# ============================================================================
# KnowledgePoint — 知识点
# ============================================================================


class KnowledgePoint(BaseModel):
    """从教材中提取的独立知识点。

    对应 ``knowledge_points`` 表。每个知识点包含原文内容、标题、
    难度评估、前后置依赖关系和掌握程度追踪。
    """

    kp_id: str = Field(
        ...,
        description="知识点唯一标识",
    )
    material_id: str = Field(
        ...,
        description="所属学习材料 ID",
    )
    title: str = Field(
        ...,
        min_length=1,
        max_length=256,
        description="知识点标题，如'牛顿第二定律'、'矩阵乘法'",
    )
    summary: str = Field(
        default="",
        max_length=256,
        description="一句话摘要（≤256 字符）",
    )
    content: str = Field(
        ...,
        min_length=1,
        description="知识点正文",
    )
    keywords: list[str] = Field(
        default_factory=list,
        description="关键词列表（3–8 个）",
    )
    difficulty: DifficultyLevel = Field(
        default=DifficultyLevel.MEDIUM,
        description="难度等级：beginner | easy | medium | hard | expert",
    )
    course_type: str = Field(
        default="",
        description="课程类型，如'physics'、'mathematics'",
    )
    chapter_index: int = Field(
        default=0,
        ge=0,
        description="章节序号（0-based），用于排序",
    )
    prerequisite_ids: list[str] = Field(
        default_factory=list,
        description="前置知识点 ID 列表",
    )
    successor_ids: list[str] = Field(
        default_factory=list,
        description="后继知识点 ID 列表",
    )
    mastery_level: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="掌握程度（0.0–1.0）",
    )
    assessment_count: int = Field(
        default=0,
        ge=0,
        description="已评估次数",
    )
    created_at: str = Field(
        ...,
        description="创建时间（ISO 8601）",
    )
    updated_at: str = Field(
        ...,
        description="最后更新时间（ISO 8601）",
    )
