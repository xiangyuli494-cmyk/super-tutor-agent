"""SQLite persistence layer for Super Tutor.

Provides structured storage for materials, knowledge points, questions,
quiz attempts, wrong-answer tracking, and study plans.  Uses aiosqlite
for async I/O.

Tables (6):
    materials         – 学习材料
    knowledge_points  – 知识点
    questions         – 题库
    quiz_attempts     – 作答记录
    wrong_questions   – 错题本
    study_plans       – 学习计划
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Sequence

import aiosqlite

logger = logging.getLogger(__name__)


class Database:
    """Async SQLite database manager for a Super Tutor teaching session.

    Each session owns one ``super_tutor.db`` file managed through this class.
    The database contains six tables:

    * **materials** – learning materials with course type.
    * **knowledge_points** – knowledge points extracted from materials,
      with prerequisite/successor relationships and mastery tracking.
    * **questions** – quiz questions linked to knowledge points.
    * **quiz_attempts** – student answer records.
    * **wrong_questions** – wrong-answer notebook for review.
    * **study_plans** – personalised learning plans with KP sequences.

    Attributes:
        db_path: Absolute path to the SQLite database file.
        config: TutorConfig instance providing API keys and defaults.
    """

    # ==================================================================
    # DDL (Data Definition Language)
    # ==================================================================

    _DDL_MATERIALS = """
    CREATE TABLE IF NOT EXISTS materials (
        material_id  TEXT PRIMARY KEY,
        title        TEXT    NOT NULL,
        content      TEXT    NOT NULL DEFAULT '',
        course_type  TEXT    NOT NULL DEFAULT '',
        status       TEXT    NOT NULL DEFAULT 'draft',
        created_at   TEXT    NOT NULL,
        updated_at   TEXT    NOT NULL
    );
    """

    _DDL_KNOWLEDGE_POINTS = """
    CREATE TABLE IF NOT EXISTS knowledge_points (
        kp_id            TEXT PRIMARY KEY,
        material_id      TEXT    NOT NULL,
        title            TEXT    NOT NULL,
        summary          TEXT    NOT NULL DEFAULT '',
        content          TEXT    NOT NULL,
        keywords         TEXT    NOT NULL DEFAULT '[]',
        difficulty       TEXT    NOT NULL DEFAULT 'medium',
        course_type      TEXT    NOT NULL DEFAULT '',
        chapter_index    INTEGER NOT NULL DEFAULT 0,
        prerequisite_ids TEXT    NOT NULL DEFAULT '[]',
        successor_ids    TEXT    NOT NULL DEFAULT '[]',
        mastery_level    REAL    NOT NULL DEFAULT 0.0,
        assessment_count INTEGER NOT NULL DEFAULT 0,
        created_at       TEXT    NOT NULL,
        updated_at       TEXT    NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_kp_material_id
        ON knowledge_points(material_id);
    CREATE INDEX IF NOT EXISTS idx_kp_title
        ON knowledge_points(title);
    CREATE INDEX IF NOT EXISTS idx_kp_difficulty
        ON knowledge_points(difficulty);
    """

    _DDL_QUESTIONS = """
    CREATE TABLE IF NOT EXISTS questions (
        question_id        TEXT PRIMARY KEY,
        type               TEXT    NOT NULL,
        difficulty         TEXT    NOT NULL DEFAULT 'medium',
        subject            TEXT    NOT NULL DEFAULT '',
        topic              TEXT    NOT NULL DEFAULT '',
        stem               TEXT    NOT NULL,
        options            TEXT    NOT NULL DEFAULT '[]',
        correct_answer     TEXT    NOT NULL,
        explanation        TEXT    NOT NULL DEFAULT '',
        kp_id              TEXT    NOT NULL DEFAULT '',
        kp_context         TEXT    NOT NULL DEFAULT '',
        estimated_seconds  INTEGER NOT NULL DEFAULT 120,
        points             REAL    NOT NULL DEFAULT 1.0,
        tags               TEXT    NOT NULL DEFAULT '[]',
        metadata           TEXT    NOT NULL DEFAULT '{}',
        created_at         TEXT    NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_questions_topic
        ON questions(topic);
    CREATE INDEX IF NOT EXISTS idx_questions_difficulty
        ON questions(difficulty);
    CREATE INDEX IF NOT EXISTS idx_questions_type
        ON questions(type);
    CREATE INDEX IF NOT EXISTS idx_questions_kp_id
        ON questions(kp_id);
    """

    _DDL_QUIZ_ATTEMPTS = """
    CREATE TABLE IF NOT EXISTS quiz_attempts (
        attempt_id        TEXT PRIMARY KEY,
        student_id        TEXT    NOT NULL DEFAULT '',
        question_id       TEXT    NOT NULL,
        kp_id             TEXT    NOT NULL DEFAULT '',
        student_answer    TEXT,
        is_correct        INTEGER,
        score             REAL,
        time_spent_seconds INTEGER NOT NULL DEFAULT 0,
        hints_used        INTEGER NOT NULL DEFAULT 0,
        attempt_number    INTEGER NOT NULL DEFAULT 1,
        confidence        REAL,
        misconception_ids TEXT    NOT NULL DEFAULT '[]',
        note              TEXT    NOT NULL DEFAULT '',
        started_at        TEXT    NOT NULL,
        submitted_at      TEXT,
        metadata          TEXT    NOT NULL DEFAULT '{}'
    );
    CREATE INDEX IF NOT EXISTS idx_attempts_student_id
        ON quiz_attempts(student_id);
    CREATE INDEX IF NOT EXISTS idx_attempts_question_id
        ON quiz_attempts(question_id);
    CREATE INDEX IF NOT EXISTS idx_attempts_is_correct
        ON quiz_attempts(is_correct);
    CREATE INDEX IF NOT EXISTS idx_attempts_kp_id
        ON quiz_attempts(kp_id);
    """

    _DDL_WRONG_QUESTIONS = """
    CREATE TABLE IF NOT EXISTS wrong_questions (
        wrong_id          TEXT PRIMARY KEY,
        student_id        TEXT    NOT NULL,
        question_id       TEXT    NOT NULL,
        kp_id             TEXT    NOT NULL DEFAULT '',
        wrong_answer      TEXT,
        correct_answer    TEXT    NOT NULL,
        attempt_count     INTEGER NOT NULL DEFAULT 1,
        resolution_status TEXT    NOT NULL DEFAULT 'unresolved',
        note              TEXT    NOT NULL DEFAULT '',
        created_at        TEXT    NOT NULL,
        updated_at        TEXT    NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_wrong_student_id
        ON wrong_questions(student_id);
    CREATE INDEX IF NOT EXISTS idx_wrong_question_id
        ON wrong_questions(question_id);
    CREATE INDEX IF NOT EXISTS idx_wrong_kp_id
        ON wrong_questions(kp_id);
    CREATE INDEX IF NOT EXISTS idx_wrong_resolution
        ON wrong_questions(resolution_status);
    """

    _DDL_STUDY_PLANS = """
    CREATE TABLE IF NOT EXISTS study_plans (
        plan_id      TEXT PRIMARY KEY,
        student_id   TEXT    NOT NULL,
        title        TEXT    NOT NULL DEFAULT '',
        description  TEXT    NOT NULL DEFAULT '',
        goal         TEXT    NOT NULL DEFAULT '',
        start_date   TEXT    NOT NULL,
        end_date     TEXT,
        status       TEXT    NOT NULL DEFAULT 'active',
        kp_sequence  TEXT    NOT NULL DEFAULT '[]',
        metadata     TEXT    NOT NULL DEFAULT '{}',
        created_at   TEXT    NOT NULL,
        updated_at   TEXT    NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_study_plans_student_id
        ON study_plans(student_id);
    """

    # ==================================================================
    # Lifecycle
    # ==================================================================

    def __init__(self, db_path: str) -> None:
        """Initialise the Database manager.

        Args:
            db_path: Path to the SQLite database file (e.g.
                ``/home/user/super-tutor/super_tutor.db``).

        Raises:
            ValueError: If the parent directory does not exist.
        """
        self.db_path: str = self._validate_db_path(db_path)
        self._conn: Optional[aiosqlite.Connection] = None

    async def initialize(self) -> None:
        """Open the database and create all tables.

        This method is idempotent: calling it multiple times is safe (the
        connection is reused once opened).  Tables use ``IF NOT EXISTS`` so
        existing data is never overwritten.

        Raises:
            RuntimeError: If the database connection cannot be established.
            ValueError: If the database path is invalid.
        """
        if self._conn is not None:
            return  # already initialised

        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        # Enable WAL mode for better concurrent read performance.
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA foreign_keys=ON;")

        await self._create_tables()

    async def close(self) -> None:
        """Close the database connection gracefully.

        Safe to call even if ``initialize`` was never called.
        """
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ==================================================================
    # Internal: table creation & validation
    # ==================================================================

    async def _create_tables(self) -> None:
        """Execute all DDL statements to ensure required tables exist."""
        assert self._conn is not None
        ddl_statements = [
            self._DDL_MATERIALS,
            self._DDL_KNOWLEDGE_POINTS,
            self._DDL_QUESTIONS,
            self._DDL_QUIZ_ATTEMPTS,
            self._DDL_WRONG_QUESTIONS,
            self._DDL_STUDY_PLANS,
        ]
        for ddl in ddl_statements:
            await self._conn.executescript(ddl)
        await self._conn.commit()

    @staticmethod
    def _validate_db_path(db_path: str) -> str:
        """Validate and resolve the database path.

        Ensures the parent directory exists.

        Args:
            db_path: Requested database file path (may be relative or absolute).

        Returns:
            The resolved absolute path.

        Raises:
            ValueError: If the parent directory does not exist.
        """
        resolved = os.path.abspath(os.path.expanduser(db_path))
        parent_dir = os.path.dirname(resolved)

        if not os.path.isdir(parent_dir):
            raise ValueError(
                f"Parent directory for database does not exist: {parent_dir}"
            )

        return resolved

    # ==================================================================
    # Helpers
    # ==================================================================

    @staticmethod
    def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
        """Convert an aiosqlite.Row into a plain dict."""
        return dict(row)

    @staticmethod
    def _rows_to_dicts(rows: Sequence[aiosqlite.Row]) -> list[dict[str, Any]]:
        """Convert a sequence of aiosqlite.Row objects into a list of dicts."""
        return [dict(r) for r in rows]

    # ==================================================================
    # Material CRUD
    # ==================================================================

    async def create_material(self, material: dict[str, Any]) -> str:
        """Insert a new learning material record.

        Args:
            material: A dict with at least ``"material_id"``, ``"title"``,
                ``"content"``, ``"created_at"``, ``"updated_at"``.
                Optional keys: ``"course_type"``, ``"status"``.

        Returns:
            The ``material_id`` of the newly created row.
        """
        assert self._conn is not None
        await self._conn.execute(
            """INSERT INTO materials (material_id, title, content, course_type,
               status, created_at, updated_at)
               VALUES (:material_id, :title, :content, :course_type,
                       :status, :created_at, :updated_at)""",
            {
                "material_id": material["material_id"],
                "title": material["title"],
                "content": material.get("content", ""),
                "course_type": material.get("course_type", ""),
                "status": material.get("status", "draft"),
                "created_at": material["created_at"],
                "updated_at": material["updated_at"],
            },
        )
        await self._conn.commit()
        return material["material_id"]

    async def get_material(self, material_id: str) -> Optional[dict[str, Any]]:
        """Retrieve a single material by its ID.

        Args:
            material_id: The material UUID.

        Returns:
            A dict with material fields, or *None* if not found.
        """
        assert self._conn is not None
        cursor = await self._conn.execute(
            "SELECT * FROM materials WHERE material_id = ?", (material_id,)
        )
        row = await cursor.fetchone()
        return self._row_to_dict(row) if row else None

    async def update_material(
        self, material_id: str, updates: dict[str, Any]
    ) -> None:
        """Partially update a material record.

        Args:
            material_id: The material to update.
            updates: Dict of column → new value.  Only whitelisted columns
                (``title``, ``content``, ``course_type``, ``status``,
                ``updated_at``) are applied.
        """
        allowed = {"title", "content", "course_type", "status", "updated_at"}
        filtered = {k: v for k, v in updates.items() if k in allowed}
        if not filtered:
            return
        assert self._conn is not None
        set_clause = ", ".join(f"{k} = ?" for k in filtered)
        values = list(filtered.values()) + [material_id]
        await self._conn.execute(
            f"UPDATE materials SET {set_clause} WHERE material_id = ?",
            values,
        )
        await self._conn.commit()

    # ==================================================================
    # Knowledge Point CRUD
    # ==================================================================

    async def insert_knowledge_point(self, kp: dict[str, Any]) -> str:
        """Insert a knowledge point.

        Args:
            kp: Dict with keys matching ``knowledge_points`` columns.
                Required: ``kp_id``, ``material_id``, ``content``,
                ``created_at``.

        Returns:
            The ``kp_id`` of the inserted row.
        """
        assert self._conn is not None

        def _json_field(value: Any) -> str:
            if isinstance(value, (list, dict)):
                return json.dumps(value, ensure_ascii=False)
            return str(value) if value else "[]"

        await self._conn.execute(
            """INSERT INTO knowledge_points
               (kp_id, material_id, title, summary, content, keywords,
                difficulty, course_type, chapter_index, prerequisite_ids,
                successor_ids, mastery_level, assessment_count,
                created_at, updated_at)
               VALUES
               (:kp_id, :material_id, :title, :summary, :content, :keywords,
                :difficulty, :course_type, :chapter_index, :prerequisite_ids,
                :successor_ids, :mastery_level, :assessment_count,
                :created_at, :updated_at)""",
            {
                "kp_id": kp["kp_id"],
                "material_id": kp["material_id"],
                "title": kp["title"],
                "summary": kp.get("summary", ""),
                "content": kp["content"],
                "keywords": _json_field(kp.get("keywords", [])),
                "difficulty": kp.get("difficulty", "medium"),
                "course_type": kp.get("course_type", ""),
                "chapter_index": kp.get("chapter_index", 0),
                "prerequisite_ids": _json_field(kp.get("prerequisite_ids", [])),
                "successor_ids": _json_field(kp.get("successor_ids", [])),
                "mastery_level": kp.get("mastery_level", 0.0),
                "assessment_count": kp.get("assessment_count", 0),
                "created_at": kp["created_at"],
                "updated_at": kp["updated_at"],
            },
        )
        await self._conn.commit()
        return kp["kp_id"]

    async def get_knowledge_point(self, kp_id: str) -> Optional[dict[str, Any]]:
        """Retrieve a single knowledge point by ID."""
        assert self._conn is not None
        cursor = await self._conn.execute(
            "SELECT * FROM knowledge_points WHERE kp_id = ?", (kp_id,)
        )
        row = await cursor.fetchone()
        return self._row_to_dict(row) if row else None

    async def list_knowledge_points_by_material(
        self, material_id: str
    ) -> list[dict[str, Any]]:
        """List all knowledge points belonging to a material, ordered by page."""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """SELECT * FROM knowledge_points
               WHERE material_id = ?
               ORDER BY chapter_index ASC, created_at ASC""",
            (material_id,),
        )
        rows = await cursor.fetchall()
        return self._rows_to_dicts(rows)

    async def list_knowledge_points_with_mastery(
        self,
    ) -> list[dict[str, Any]]:
        """List all knowledge points with their mastery levels.

        Returns:
            A list of knowledge point dicts ordered by mastery_level
            ascending (weakest first).
        """
        assert self._conn is not None
        cursor = await self._conn.execute(
            """SELECT * FROM knowledge_points
               ORDER BY mastery_level ASC"""
        )
        rows = await cursor.fetchall()
        return self._rows_to_dicts(rows)

    async def upsert_knowledge_point_mastery(
        self, kp_id: str, mastery_level: float
    ) -> None:
        """Update the mastery level of a knowledge point.

        Args:
            kp_id: The knowledge point identifier.
            mastery_level: New mastery level (0.0 – 1.0).
        """
        assert self._conn is not None
        await self._conn.execute(
            "UPDATE knowledge_points SET mastery_level = ? WHERE kp_id = ?",
            (mastery_level, kp_id),
        )
        await self._conn.commit()

    async def update_knowledge_point(
        self, kp_id: str, updates: dict[str, Any]
    ) -> None:
        """Update specific columns of a knowledge point.

        Args:
            kp_id: The knowledge point identifier.
            updates: Dict of column_name → new_value.  Only the keys
                present are updated; all others are left unchanged.
        """
        assert self._conn is not None
        allowed = {
            "title", "summary", "content", "keywords", "difficulty",
            "course_type", "chapter_index", "prerequisite_ids",
            "successor_ids", "mastery_level", "assessment_count",
            "updated_at",
        }
        set_clauses: list[str] = []
        params: list[Any] = []
        for col, val in updates.items():
            if col not in allowed:
                continue
            set_clauses.append(f"{col} = ?")
            if isinstance(val, (list, dict)):
                params.append(json.dumps(val, ensure_ascii=False))
            else:
                params.append(val)

        if not set_clauses:
            return

        params.append(kp_id)
        sql = f"UPDATE knowledge_points SET {', '.join(set_clauses)} WHERE kp_id = ?"
        await self._conn.execute(sql, params)
        await self._conn.commit()

    # ==================================================================
    # Question CRUD
    # ==================================================================

    async def insert_question(self, question: dict[str, Any]) -> str:
        """Insert a quiz question.

        Args:
            question: Dict with keys matching ``questions`` columns.
                Required: ``question_id``, ``type``, ``stem``,
                ``correct_answer``, ``created_at``.

        Returns:
            The ``question_id`` of the inserted row.
        """
        assert self._conn is not None

        def _json_field(value: Any) -> str:
            if isinstance(value, (list, dict)):
                return json.dumps(value, ensure_ascii=False)
            return str(value) if value else "[]"

        await self._conn.execute(
            """INSERT OR REPLACE INTO questions
               (question_id, type, difficulty, subject, topic,
                stem, options, correct_answer, explanation, kp_id,
                kp_context, estimated_seconds, points, tags, metadata,
                created_at)
               VALUES
               (:question_id, :type, :difficulty, :subject, :topic,
                :stem, :options, :correct_answer, :explanation, :kp_id,
                :kp_context, :estimated_seconds, :points, :tags, :metadata,
                :created_at)""",
            {
                "question_id": question["question_id"],
                "type": question["type"],
                "difficulty": question.get("difficulty", "medium"),
                "subject": question.get("subject", ""),
                "topic": question.get("topic", ""),
                "stem": question["stem"],
                "options": _json_field(question.get("options", [])),
                "correct_answer": _json_field(question["correct_answer"]),
                "explanation": question.get("explanation", ""),
                "kp_id": question.get("kp_id", ""),
                "kp_context": question.get("kp_context", ""),
                "estimated_seconds": question.get("estimated_seconds", 120),
                "points": question.get("points", 1.0),
                "tags": _json_field(question.get("tags", [])),
                "metadata": _json_field(question.get("metadata", {})),
                "created_at": question["created_at"],
            },
        )
        await self._conn.commit()
        return question["question_id"]

    async def get_question(self, question_id: str) -> Optional[dict[str, Any]]:
        """Retrieve a single question by ID."""
        assert self._conn is not None
        cursor = await self._conn.execute(
            "SELECT * FROM questions WHERE question_id = ?", (question_id,)
        )
        row = await cursor.fetchone()
        return self._row_to_dict(row) if row else None

    # ==================================================================
    # Quiz Attempt CRUD
    # ==================================================================

    async def insert_attempt(self, attempt: dict[str, Any]) -> str:
        """Insert a quiz attempt record.

        Args:
            attempt: Dict with keys matching ``quiz_attempts`` columns.
                Required: ``attempt_id``, ``question_id``, ``started_at``.

        Returns:
            The ``attempt_id`` of the inserted row.
        """
        assert self._conn is not None

        def _json_field(value: Any) -> str:
            if isinstance(value, (list, dict)):
                return json.dumps(value, ensure_ascii=False)
            return str(value) if value else "[]"

        student_answer = attempt.get("student_answer")
        if isinstance(student_answer, (dict, list)):
            student_answer = json.dumps(student_answer, ensure_ascii=False)
        elif student_answer is not None:
            student_answer = str(student_answer)

        await self._conn.execute(
            """INSERT OR REPLACE INTO quiz_attempts
               (attempt_id, student_id, question_id, kp_id, student_answer,
                is_correct, score, time_spent_seconds, hints_used,
                attempt_number, confidence, misconception_ids, note,
                started_at, submitted_at, metadata)
               VALUES
               (:attempt_id, :student_id, :question_id, :kp_id, :student_answer,
                :is_correct, :score, :time_spent_seconds, :hints_used,
                :attempt_number, :confidence, :misconception_ids, :note,
                :started_at, :submitted_at, :metadata)""",
            {
                "attempt_id": attempt["attempt_id"],
                "student_id": attempt.get("student_id", ""),
                "question_id": attempt["question_id"],
                "kp_id": attempt.get("kp_id", ""),
                "student_answer": student_answer,
                "is_correct": (
                    1 if attempt.get("is_correct")
                    else (0 if attempt.get("is_correct") is False else None)
                ),
                "score": attempt.get("score"),
                "time_spent_seconds": attempt.get("time_spent_seconds", 0),
                "hints_used": attempt.get("hints_used", 0),
                "attempt_number": attempt.get("attempt_number", 1),
                "confidence": attempt.get("confidence"),
                "misconception_ids": _json_field(
                    attempt.get("misconception_ids", [])
                ),
                "note": attempt.get("note", ""),
                "started_at": attempt["started_at"],
                "submitted_at": attempt.get("submitted_at"),
                "metadata": _json_field(attempt.get("metadata", {})),
            },
        )
        await self._conn.commit()
        return attempt["attempt_id"]

    async def list_attempts_by_student(
        self,
        student_id: str,
        is_correct: Optional[bool] = None,
        kp_id: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """List quiz attempts filtered by student, with optional filters.

        Args:
            student_id: The student identifier.
            is_correct: Optional correctness filter.
            kp_id: Optional knowledge point filter.
            limit: Max rows to return.
            offset: Pagination offset.

        Returns:
            A tuple of (items, total_count).
        """
        assert self._conn is not None

        params: list[Any] = [student_id]
        where = "WHERE student_id = ?"
        if is_correct is not None:
            where += " AND is_correct = ?"
            params.append(1 if is_correct else 0)
        if kp_id is not None:
            where += " AND kp_id = ?"
            params.append(kp_id)

        # Total count
        count_cursor = await self._conn.execute(
            f"SELECT COUNT(*) FROM quiz_attempts {where}", params
        )
        count_row = await count_cursor.fetchone()
        total = count_row[0] if count_row else 0

        # Paginated rows
        cursor = await self._conn.execute(
            f"""SELECT * FROM quiz_attempts {where}
                ORDER BY submitted_at DESC
                LIMIT ? OFFSET ?""",
            params + [limit, offset],
        )
        rows = await cursor.fetchall()
        return (self._rows_to_dicts(rows), total)

    # ==================================================================
    # Wrong Questions CRUD (错题本)
    # ==================================================================

    async def insert_wrong_question(self, record: dict[str, Any]) -> str:
        """Insert a wrong-answer notebook entry.

        Args:
            record: Dict with keys ``wrong_id``, ``student_id``,
                ``question_id``, ``correct_answer``, ``created_at``,
                ``updated_at``.  Optional: ``kp_id``, ``wrong_answer``,
                ``attempt_count``, ``resolution_status``, ``note``.

        Returns:
            The ``wrong_id`` of the inserted row.
        """
        assert self._conn is not None
        await self._conn.execute(
            """INSERT OR REPLACE INTO wrong_questions
               (wrong_id, student_id, question_id, kp_id, wrong_answer,
                correct_answer, attempt_count, resolution_status, note,
                created_at, updated_at)
               VALUES
               (:wrong_id, :student_id, :question_id, :kp_id, :wrong_answer,
                :correct_answer, :attempt_count, :resolution_status, :note,
                :created_at, :updated_at)""",
            {
                "wrong_id": record["wrong_id"],
                "student_id": record["student_id"],
                "question_id": record["question_id"],
                "kp_id": record.get("kp_id", ""),
                "wrong_answer": record.get("wrong_answer"),
                "correct_answer": record["correct_answer"],
                "attempt_count": record.get("attempt_count", 1),
                "resolution_status": record.get(
                    "resolution_status", "unresolved"
                ),
                "note": record.get("note", ""),
                "created_at": record["created_at"],
                "updated_at": record["updated_at"],
            },
        )
        await self._conn.commit()
        return record["wrong_id"]

    async def get_wrong_question(
        self, wrong_id: str
    ) -> Optional[dict[str, Any]]:
        """Retrieve a single wrong-question entry by ID."""
        assert self._conn is not None
        cursor = await self._conn.execute(
            "SELECT * FROM wrong_questions WHERE wrong_id = ?", (wrong_id,)
        )
        row = await cursor.fetchone()
        return self._row_to_dict(row) if row else None

    async def get_wrong_question_by_student_and_question(
        self, student_id: str, question_id: str
    ) -> Optional[dict[str, Any]]:
        """Find an existing wrong-question entry for a student + question pair.

        Returns ``None`` when no entry exists for this pair.
        """
        assert self._conn is not None
        cursor = await self._conn.execute(
            """SELECT * FROM wrong_questions
               WHERE student_id = ? AND question_id = ?
               LIMIT 1""",
            (student_id, question_id),
        )
        row = await cursor.fetchone()
        return self._row_to_dict(row) if row else None

    async def list_wrong_questions_by_student(
        self,
        student_id: str,
        resolution_status: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """Paginated list of wrong questions for a student.

        Args:
            student_id: The student identifier.
            resolution_status: Optional filter (``unresolved``,
                ``reviewing``, ``resolved``).
            limit: Max rows to return.
            offset: Pagination offset.

        Returns:
            A tuple of (items, total_count).
        """
        assert self._conn is not None

        params: list[Any] = [student_id]
        where = "WHERE student_id = ?"
        if resolution_status is not None:
            where += " AND resolution_status = ?"
            params.append(resolution_status)

        # Total count
        count_cursor = await self._conn.execute(
            f"SELECT COUNT(*) FROM wrong_questions {where}", params
        )
        count_row = await count_cursor.fetchone()
        total = count_row[0] if count_row else 0

        # Paginated rows
        cursor = await self._conn.execute(
            f"""SELECT * FROM wrong_questions {where}
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?""",
            params + [limit, offset],
        )
        rows = await cursor.fetchall()
        return (self._rows_to_dicts(rows), total)

    async def update_wrong_question(
        self, wrong_id: str, updates: dict[str, Any]
    ) -> None:
        """Update fields of a single wrong-question entry.

        Args:
            wrong_id: The wrong question identifier.
            updates: Key-value pairs to update (e.g.
                ``{"resolution_status": "resolved"}``).
        """
        assert self._conn is not None
        set_clauses = []
        values: list[Any] = []
        for key, value in updates.items():
            set_clauses.append(f"{key} = ?")
            values.append(value)
        values.append(wrong_id)
        await self._conn.execute(
            f"UPDATE wrong_questions SET {', '.join(set_clauses)} "
            f"WHERE wrong_id = ?",
            values,
        )
        await self._conn.commit()

    # ==================================================================
    # Study Plan CRUD
    # ==================================================================

    async def create_study_plan(self, plan: dict[str, Any]) -> str:
        """Create a study plan with an embedded KP sequence.

        Args:
            plan: Dict with ``plan_id``, ``student_id``, ``start_date``,
                ``created_at``, ``updated_at``.  Optional: ``title``,
                ``description``, ``goal``, ``end_date``, ``status``,
                ``kp_sequence``, ``metadata``.

        Returns:
            The ``plan_id`` of the newly created plan.
        """
        assert self._conn is not None

        kp_sequence = plan.get("kp_sequence", [])
        if isinstance(kp_sequence, list):
            kp_sequence = json.dumps(kp_sequence, ensure_ascii=False)

        await self._conn.execute(
            """INSERT INTO study_plans
               (plan_id, student_id, title, description, goal,
                start_date, end_date, status, kp_sequence, metadata,
                created_at, updated_at)
               VALUES
               (:plan_id, :student_id, :title, :description, :goal,
                :start_date, :end_date, :status, :kp_sequence, :metadata,
                :created_at, :updated_at)""",
            {
                "plan_id": plan["plan_id"],
                "student_id": plan["student_id"],
                "title": plan.get("title", ""),
                "description": plan.get("description", ""),
                "goal": plan.get("goal", ""),
                "start_date": plan["start_date"],
                "end_date": plan.get("end_date"),
                "status": plan.get("status", "active"),
                "kp_sequence": kp_sequence,
                "metadata": json.dumps(plan.get("metadata", {})),
                "created_at": plan["created_at"],
                "updated_at": plan["updated_at"],
            },
        )
        await self._conn.commit()
        return plan["plan_id"]

    async def get_study_plan(self, plan_id: str) -> Optional[dict[str, Any]]:
        """Retrieve a single study plan by ID."""
        assert self._conn is not None
        cursor = await self._conn.execute(
            "SELECT * FROM study_plans WHERE plan_id = ?", (plan_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        result = self._row_to_dict(row)
        # Deserialise kp_sequence from JSON
        try:
            result["kp_sequence"] = json.loads(result.get("kp_sequence", "[]"))
        except (json.JSONDecodeError, TypeError):
            result["kp_sequence"] = []
        return result
