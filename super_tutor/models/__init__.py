"""Super Tutor — 数据模型层。

提供全项目共用的枚举、Pydantic 模型和类型定义。
"""

from super_tutor.models.enums import (
    DifficultyLevel,
    QuestionType,
)

from super_tutor.models.knowledge import (
    KnowledgePoint,
)

from super_tutor.models.quiz import (
    Question,
    QuizAttempt,
)

from super_tutor.models.assessment import (
    AssessmentReport,
    KPAssessmentResult,
)

from super_tutor.models.mastery import ReviewItem

from super_tutor.models.plan import StudyPlan

from super_tutor.models.socratic import SocraticTurn

__all__ = [
    # Enums
    "DifficultyLevel",
    "QuestionType",
    # Knowledge
    "KnowledgePoint",
    # Quiz
    "Question",
    "QuizAttempt",
    # Assessment
    "AssessmentReport",
    "KPAssessmentResult",
    # Mastery
    "ReviewItem",
    "StudyPlan",
    # Socratic
    "SocraticTurn",
]
