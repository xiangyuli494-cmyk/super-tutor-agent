"""Super Tutor — 学生仪表盘路由。

提供学习概览、掌握度明细、错题本和今日复习清单。
部分端点依赖 mastery_records 和 study_plans 表（P1 待建），
当前 MVP 版本从已有数据聚合。
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from super_tutor.core.database import Database
from super_tutor.routes.dependencies import use_db, use_orchestrator_registry
from super_tutor.routes.schemas import (
    APIResponse,
    DashboardResponse,
    MasteryItem,
    PlanTodayResponse,
    WrongQuestionItem,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/students", tags=["dashboard"])


# ===================================================================
# Dashboard — 学习概览
# ===================================================================


@router.get("/{student_id}/dashboard", response_model=APIResponse)
async def get_dashboard(
    student_id: str,
    db: Database = Depends(use_db),
) -> APIResponse:
    """获取学生学习仪表盘概览。

    按 student_id 聚合全部 quiz_attempts，计算总体正确率、
    统计薄弱/优势知识点。
    """
    # 获取所有作答（不限条数，用于统计）
    all_attempts, _total = await db.list_attempts_by_student(
        student_id, limit=10000, offset=0
    )

    if not all_attempts:
        return APIResponse(
            data=DashboardResponse(
                student_id=student_id,
            ).model_dump(),
            message="暂无作答记录。完成一次测验后即可查看仪表盘。",
        )

    total_questions = len(all_attempts)
    correct_count = sum(
        1 for a in all_attempts if a.get("is_correct")
    )
    overall_accuracy = correct_count / total_questions if total_questions > 0 else 0.0

    # 提取最近 10 条
    recent = all_attempts[:10]

    # 批量查询所有题目（一次 JOIN 替代 N 次 get_question）避免 N+1
    question_ids = list({
        a.get("question_id", "") for a in all_attempts if a.get("question_id")
    })
    question_map = await db.get_questions_batch(question_ids)

    topic_stats: dict[str, dict[str, int]] = {}  # topic → {total, correct}
    for a in all_attempts:
        q = question_map.get(a.get("question_id", ""))
        topic = q.get("topic", "未分类") if q else "未分类"
        if topic not in topic_stats:
            topic_stats[topic] = {"total": 0, "correct": 0}
        topic_stats[topic]["total"] += 1
        if a.get("is_correct"):
            topic_stats[topic]["correct"] += 1

    weak_topics: list[str] = []
    strong_topics: list[str] = []
    for topic, stats in topic_stats.items():
        acc = stats["correct"] / stats["total"] if stats["total"] > 0 else 0
        if acc < 0.6:
            weak_topics.append(topic)
        elif acc >= 0.85 and stats["total"] >= 2:
            strong_topics.append(topic)

    return APIResponse(
        data=DashboardResponse(
            student_id=student_id,
            total_questions_attempted=total_questions,
            correct_count=correct_count,
            overall_accuracy=round(overall_accuracy, 3),
            weak_topics=weak_topics,
            strong_topics=strong_topics,
            recent_attempts=recent,
        ).model_dump(),
    )


# ===================================================================
# Mastery — 掌握度明细
# ===================================================================


@router.get("/{student_id}/mastery", response_model=APIResponse)
async def get_mastery(
    student_id: str,
    db: Database = Depends(use_db),
) -> APIResponse:
    """获取学生各知识点的掌握度明细。

    返回 mastery_records 表中的完整记录，含 SM-2 参数和状态。
    """
    records = await db.list_mastery_records(student_id)

    items: list[dict] = []
    for r in records:
        items.append(
            MasteryItem(
                knowledge_node_id=r.get("knowledge_node_id", ""),
                total_attempts=r.get("total_attempts", 0),
                correct_attempts=r.get("correct_attempts", 0),
                accuracy=round(
                    r.get("correct_attempts", 0) / r.get("total_attempts", 1)
                    if r.get("total_attempts", 0) > 0
                    else 0.0,
                    3,
                ),
                last_attempt_at=r.get("last_attempt_at"),
            ).model_dump()
        )
        # 附加 SM-2 详情（MasteryItem schema 之外）
        items[-1]["mastery_level"] = r.get("mastery_level", 0.0)
        items[-1]["state"] = r.get("state", "new")
        items[-1]["sm2_next_review"] = r.get("sm2_next_review")
        items[-1]["sm2_interval_days"] = r.get("sm2_interval_days", 0)

    if not items:
        return APIResponse(
            data={
                "student_id": student_id,
                "items": [],
            },
            message="暂无掌握度数据。完成一次测验后即可查看。",
        )

    return APIResponse(
        data={
            "student_id": student_id,
            "items": items,
        }
    )


# ===================================================================
# Wrong Questions — 错题本
# ===================================================================


@router.get("/{student_id}/wrong-questions", response_model=APIResponse)
async def get_wrong_questions(
    student_id: str,
    limit: int = Query(default=20, ge=1, le=100, description="返回条数上限"),
    offset: int = Query(default=0, ge=0, description="分页偏移"),
    db: Database = Depends(use_db),
) -> APIResponse:
    """获取学生错题本。

    从 ``quiz_attempts`` 表中查询 ``student_id`` 匹配且 ``is_correct=0``
    的记录，按提交时间倒序排列。
    """
    rows, total = await db.list_attempts_by_student(
        student_id, is_correct=False, limit=limit, offset=offset
    )

    items: list[dict] = []
    for row in rows:
        items.append(
            WrongQuestionItem(
                attempt_id=row["attempt_id"],
                question_id=row["question_id"],
                student_answer=row["student_answer"],
                is_correct=False,
                score=row["score"],
                submitted_at=row["submitted_at"],
                note=row["note"],
            ).model_dump()
        )

    return APIResponse(
        data={
            "student_id": student_id,
            "total": total,
            "limit": limit,
            "offset": offset,
            "items": items,
        }
    )


# ===================================================================
# Today's Plan — 今日复习清单
# ===================================================================


@router.get("/{student_id}/plan/today", response_model=APIResponse)
async def get_today_plan(
    student_id: str,
    db: Database = Depends(use_db),
) -> APIResponse:
    """获取学生今日复习清单。

    查询 review_items 表，返回今天应完成的所有条目。
    """
    today = date.today().isoformat()
    items = await db.get_today_items(student_id, today)

    if not items:
        return APIResponse(
            data=PlanTodayResponse(
                date=today,
                items=[],
            ).model_dump(),
            message="今日暂无排期。完成测验并生成学习计划后即可查看。",
        )

    return APIResponse(
        data=PlanTodayResponse(
            date=today,
            items=[
                {
                    "item_id": it["item_id"],
                    "knowledge_node_id": it["knowledge_node_id"],
                    "activity_type": it["activity_type"],
                    "scheduled_date": it["scheduled_date"],
                    "estimated_minutes": it["estimated_minutes"],
                    "completed": bool(it.get("completed", False)),
                    "notes": it.get("notes", ""),
                }
                for it in items
            ],
        ).model_dump(),
    )
