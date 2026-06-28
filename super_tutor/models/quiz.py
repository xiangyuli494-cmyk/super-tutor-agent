"""Super Tutor — 测验模型。

定义题目、作答记录和错题本，覆盖出题→作答→错题追踪完整链路。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from super_tutor.models.enums import DifficultyLevel, QuestionType


# ============================================================================
# Question — 单道题目
# ============================================================================


class Question(BaseModel):
    """题库中的一道题目，支持多种题型。

    ``correct_answer`` 的类型因题型而异：
    - 选择题 → 选项 key 字符串（如 ``"A"``）
    - 判断题 → ``true`` / ``false``
    - 填空题 → 字符串或字符串列表（多空）
    - 简答/论述 → 参考答案文本
    - 编程题 → ``{"language": "python", "test_cases": [...], "reference_solution": "..."}``
    """

    question_id: str = Field(
        default_factory=lambda: str(uuid4()),
        description="题目唯一标识",
    )
    type: QuestionType = Field(
        ...,
        description="题目类型",
    )
    difficulty: DifficultyLevel = Field(
        default=DifficultyLevel.MEDIUM,
        description="难度等级",
    )
    subject: str = Field(
        default="",
        description="所属学科",
    )
    topic: str = Field(
        default="",
        description="主题标签，如'牛顿定律'、'矩阵运算'",
    )
    stem: str = Field(
        ...,
        min_length=1,
        description="题干（支持 Markdown）",
    )
    options: list[dict[str, Any]] = Field(
        default_factory=list,
        description="选项列表。选择题：[{'key': 'A', 'text': '...'}, ...]",
    )
    correct_answer: Any = Field(
        ...,
        description="正确答案，格式依题型而定（详见类文档）",
    )
    explanation: str = Field(
        default="",
        description="答案解析 / 解题思路（支持 Markdown）",
    )
    hints: list[str] = Field(
        default_factory=list,
        description="渐进式提示，从笼统到具体排列",
    )
    kp_id: str = Field(
        default="",
        description="直接关联的知识点 ID",
    )
    kp_context: str = Field(
        default="",
        description="出题时注入的上下文 JSON",
    )
    estimated_seconds: int = Field(
        default=120,
        ge=0,
        description="预计作答耗时（秒）",
    )
    points: float = Field(
        default=1.0,
        ge=0.0,
        description="分值",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="分类标签，便于检索与组卷",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="扩展元数据（出题人、审核状态、使用次数等）",
    )
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="创建时间（ISO 8601）",
    )



# ============================================================================
# QuizAttempt — 单题作答记录
# ============================================================================


class QuizAttempt(BaseModel):
    """学生单题作答记录 — 精简为批改核心字段。

    记录学生答案、批改结果和作答耗时，直接关联到知识点。
    """

    attempt_id: str = Field(
        default_factory=lambda: str(uuid4()),
        description="作答记录唯一标识",
    )
    student_id: str = Field(
        default="",
        description="学生标识",
    )
    question_id: str = Field(
        ...,
        description="所答 Question ID",
    )
    kp_id: str = Field(
        default="",
        description="关联的知识点 ID",
    )
    student_answer: Any = Field(
        default=None,
        description="学生提交的答案（格式与 Question.correct_answer 对齐）",
    )
    is_correct: Optional[bool] = Field(
        default=None,
        description="是否批改为正确（None 表示尚未批改）",
    )
    time_spent_seconds: int = Field(
        default=0,
        ge=0,
        description="本题作答耗时（秒）",
    )
    started_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="开始作答时间（ISO 8601）",
    )
    submitted_at: Optional[str] = Field(
        default=None,
        description="提交时间（ISO 8601）",
    )

