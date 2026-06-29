"""SQLite 持久化层 — Super Tutor 的全部数据存储。

【功能说明】
基于 aiosqlite 的异步 SQLite 数据库管理器，管理 6 张核心表：
1. materials — 学习材料（上传的 PDF/文本）
2. knowledge_points — 知识点（含双向前置/后继关系、掌握度追踪）
3. questions — 题库（由 LLM 生成的题目）
4. quiz_attempts — 作答记录（学生每次做题的结果）
5. wrong_questions — 错题本（自动收录的错题，含解决状态）
6. study_plans — 学习计划（拓扑排序后的知识点序列）

数据库特性：
- WAL 模式（提升并发读取性能）
- 外键约束（保证数据完整性）
- 所有 CRUD 方法均异步（async/await）
- JSON 字段（prerequisite_ids、successor_ids 等）存储为字符串，读写时序列化/反序列化

【耦合关系】
- 被 app.py 的 _init_services() 创建唯一实例，存入 st.session_state[_S_DB]
- 被所有 5 个 Engine 依赖（KnowledgeEngine、QuizEngine、AssessmentEngine、
  PlanEngine、SocraticEngine）
- 不依赖项目内其他模块（底层基础设施）
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional, Sequence

import aiosqlite

logger = logging.getLogger(__name__)


class Database:
    """异步 SQLite 数据库管理器。

    每个教学会话拥有一个 super_tutor.db 文件，通过本类管理。
    所有 CRUD 方法均为异步，使用参数化查询防止 SQL 注入。

    6 张核心表的数据关系：
    materials (1) ──→ (N) knowledge_points
    knowledge_points (1) ──→ (N) questions
    questions (1) ──→ (N) quiz_attempts
    questions + students ──→ wrong_questions
    knowledge_points ──→ study_plans (kp_sequence 字段)

    Usage::

        db = Database(db_path="/path/to/super_tutor.db")
        await db.initialize()  # 创建连接 + 建表
        await db.create_material({...})
        await db.close()
    """

    # ==================================================================
    # DDL — 6 张表的建表语句
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
    # 生命周期 — 连接管理
    # ==================================================================

    def __init__(self, db_path: str) -> None:
        """初始化 Database 实例（不建立连接）。

        Args:
            db_path: SQLite 数据库文件路径（如 ~/.super-tutor/super_tutor.db）。

        Raises:
            ValueError: 如果父目录不存在。
        """
        self.db_path: str = self._validate_db_path(db_path)
        self._conn: Optional[aiosqlite.Connection] = None

    async def initialize(self) -> None:
        """打开数据库连接并创建所有表（幂等操作）。

        特性：
        - 首次调用时：建立连接 → 启用 WAL 模式 → 开启外键约束 → 建表
        - 重复调用时：直接返回（_conn 已存在）
        - 所有表使用 IF NOT EXISTS，不会覆盖已有数据

        Raises:
            RuntimeError: 数据库连接建立失败。
        """
        if self._conn is not None:
            return  # 已初始化，幂等返回

        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row  # 支持按列名访问结果
        # WAL 模式 — 允许并发读写，读取不阻塞写入
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        # 外键约束 — 确保数据引用完整性
        await self._conn.execute("PRAGMA foreign_keys=ON;")

        await self._create_tables()

    async def close(self) -> None:
        """优雅关闭数据库连接。

        即使从未调用 initialize() 也安全（_conn 为 None 时跳过）。
        """
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ==================================================================
    # 内部方法 — 建表和路径验证
    # ==================================================================

    async def _create_tables(self) -> None:
        """按顺序执行所有 DDL 语句，确保 6 张表全部存在。

        使用 executescript 支持多语句一次性执行（含 CREATE INDEX）。
        """
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
        """验证并解析数据库路径，确保父目录存在。

        Args:
            db_path: 请求的数据库文件路径（可为相对路径或包含 ~）。

        Returns:
            str: 解析后的绝对路径。

        Raises:
            ValueError: 父目录不存在。
        """
        resolved = os.path.abspath(os.path.expanduser(db_path))
        parent_dir = os.path.dirname(resolved)

        if not os.path.isdir(parent_dir):
            raise ValueError(
                f"Parent directory for database does not exist: {parent_dir}"
            )

        return resolved

    # ==================================================================
    # 辅助工具 — 行转换
    # ==================================================================

    @staticmethod
    def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
        """将 aiosqlite.Row 转为普通 dict。"""
        return dict(row)

    @staticmethod
    def _rows_to_dicts(rows: Sequence[aiosqlite.Row]) -> list[dict[str, Any]]:
        """批量转换：aisqlite.Row 列表 → dict 列表。"""
        return [dict(r) for r in rows]

    # ==================================================================
    # 1. 学习材料 CRUD — materials 表
    # ==================================================================

    async def create_material(self, material: dict[str, Any]) -> str:
        """插入一条新的学习材料记录。

        Args:
            material: 至少包含 material_id、title、content、created_at、updated_at。
                      可选：course_type、status。

        Returns:
            str: 新创建记录的 material_id。
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
        """按 ID 查询单条学习材料。

        Returns:
            dict | None: 找到返回字段字典，未找到返回 None。
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
        """部分更新学习材料记录。

        仅更新白名单内的字段：title、content、course_type、status、updated_at。

        Args:
            material_id: 要更新的材料 ID。
            updates: 字段→新值的字典。
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
    # 2. 知识点 CRUD — knowledge_points 表
    #    核心表：存储知识点及其双向前置/后继关系（JSON 字符串）
    # ==================================================================

    async def insert_knowledge_point(self, kp: dict[str, Any]) -> str:
        """插入一条知识点记录。

        注意：
        - keywords、prerequisite_ids、successor_ids 在 DB 中存为 JSON 字符串
        - 插入时通过 _json_field() 自动序列化列表/字典

        Args:
            kp: 至少包含 kp_id、material_id、content、created_at。

        Returns:
            str: 新创建记录的 kp_id。
        """
        assert self._conn is not None

        def _json_field(value: Any) -> str:
            """将列表/字典序列化为 JSON 字符串，用于 DB 存储。"""
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
        """按 ID 查询单个知识点。

        Returns:
            dict | None: 找到返回字段字典（JSON 字段为字符串，需调用 _parse_json_list 解析），
                         未找到返回 None。
        """
        assert self._conn is not None
        cursor = await self._conn.execute(
            "SELECT * FROM knowledge_points WHERE kp_id = ?", (kp_id,)
        )
        row = await cursor.fetchone()
        return self._row_to_dict(row) if row else None

    async def list_knowledge_points_by_material(
        self, material_id: str
    ) -> list[dict[str, Any]]:
        """列出某教材的所有知识点，按章节序号和时间排序。

        用于 KnowledgeEngine.get_by_material()。
        """
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
        """列出所有知识点及其掌握度，按掌握度升序（最薄弱优先）。

        用于 PlanEngine 和 AssessmentEngine 的数据查询。
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
        """更新单个知识点的掌握度（覆盖写入）。

        Args:
            kp_id: 知识点 ID。
            mastery_level: 新的掌握度值（0.0–1.0）。
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
        """部分更新知识点字段。

        用于 KnowledgeEngine.parse() 中双向写入前置/后继关系。
        列表/字典类型的值自动序列化为 JSON 字符串。

        Args:
            kp_id: 知识点 ID。
            updates: 字段→新值的字典（只更新白名单内字段）。
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
    # 3. 题目 CRUD — questions 表
    #    使用 INSERT OR REPLACE 支持幂等写入
    # ==================================================================

    async def insert_question(self, question: dict[str, Any]) -> str:
        """插入一道题目（幂等：已存在则替换）。

        Args:
            question: 至少包含 question_id、type、stem、correct_answer、created_at。

        Returns:
            str: question_id。
        """
        assert self._conn is not None

        def _json_field(value: Any) -> str:
            """将列表/字典序列化为 JSON 字符串。"""
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
        """按 ID 查询单道题目。

        用于错题本渲染时获取题干、正确答案和解析。
        """
        assert self._conn is not None
        cursor = await self._conn.execute(
            "SELECT * FROM questions WHERE question_id = ?", (question_id,)
        )
        row = await cursor.fetchone()
        return self._row_to_dict(row) if row else None

    # ==================================================================
    # 4. 作答记录 CRUD — quiz_attempts 表
    #    每次学生提交答案后写入一条记录
    # ==================================================================

    async def insert_attempt(self, attempt: dict[str, Any]) -> str:
        """插入一条作答记录。

        Args:
            attempt: 至少包含 attempt_id、question_id、started_at。

        Returns:
            str: attempt_id。
        """
        assert self._conn is not None

        def _json_field(value: Any) -> str:
            """Serialize a list/dict to JSON string for DB storage.

            Lists and dicts → json.dumps(); other values → str(); falsy → "[]".
            """
            if isinstance(value, (list, dict)):
                return json.dumps(value, ensure_ascii=False)
            return str(value) if value else "[]"

        # 学生答案可能是字符串或复杂对象（如 JSON），统一序列化
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
        """按学生分页查询作答记录，支持按正确性和知识点过滤。

        Args:
            student_id: 学生标识。
            is_correct: 可选的正确性过滤（True=只看正确，False=只看错误）。
            kp_id: 可选的知识点过滤。
            limit: 每页条数。
            offset: 分页偏移量。

        Returns:
            tuple: (items 列表, total 总数)。
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

        # 查询总数（用于分页计算）
        count_cursor = await self._conn.execute(
            f"SELECT COUNT(*) FROM quiz_attempts {where}", params
        )
        count_row = await count_cursor.fetchone()
        total = count_row[0] if count_row else 0

        # 分页查询
        cursor = await self._conn.execute(
            f"""SELECT * FROM quiz_attempts {where}
                ORDER BY submitted_at DESC
                LIMIT ? OFFSET ?""",
            params + [limit, offset],
        )
        rows = await cursor.fetchall()
        return (self._rows_to_dicts(rows), total)

    # ==================================================================
    # 5. 错题本 CRUD — wrong_questions 表
    #    自动收录学生答错的题目，支持解决状态追踪
    # ==================================================================

    async def insert_wrong_question(self, record: dict[str, Any]) -> str:
        """插入一条错题记录（幂等：已存在则替换）。

        Args:
            record: 至少包含 wrong_id、student_id、question_id、
                    correct_answer、created_at、updated_at。

        Returns:
            str: wrong_id。
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
        """按 ID 查询单条错题记录。

        用于 SocraticEngine 获取错题上下文。
        """
        assert self._conn is not None
        cursor = await self._conn.execute(
            "SELECT * FROM wrong_questions WHERE wrong_id = ?", (wrong_id,)
        )
        row = await cursor.fetchone()
        return self._row_to_dict(row) if row else None

    async def get_wrong_question_by_student_and_question(
        self, student_id: str, question_id: str
    ) -> Optional[dict[str, Any]]:
        """按学生+题目组合查找已有错题记录。

        用于 QuizEngine.add_to_wrong_book() 判断是新增还是追加。
        同一学生答错同一道题时，不新增记录，而是递增 attempt_count。

        Returns:
            dict | None: 找到返回现有记录，未找到返回 None。
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
        """分页列出学生的错题记录。

        Args:
            student_id: 学生标识。
            resolution_status: 可选的解决状态过滤
                （unresolved=未解决、reviewing=复习中、resolved=已解决）。
            limit: 每页条数。
            offset: 分页偏移量。

        Returns:
            tuple: (items 列表, total 总数)。
        """
        assert self._conn is not None

        params: list[Any] = [student_id]
        where = "WHERE student_id = ?"
        if resolution_status is not None:
            where += " AND resolution_status = ?"
            params.append(resolution_status)

        # 查询总数
        count_cursor = await self._conn.execute(
            f"SELECT COUNT(*) FROM wrong_questions {where}", params
        )
        count_row = await count_cursor.fetchone()
        total = count_row[0] if count_row else 0

        # 分页查询，按创建时间倒序（最新的错题先显示）
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
        """更新单条错题记录的字段。

        Args:
            wrong_id: 错题记录 ID。
            updates: 要更新的字段→新值字典（如 {"resolution_status": "resolved"}）。
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
    # 6. 学习计划 CRUD — study_plans 表
    #    kp_sequence 字段存储 JSON 序列化的知识点序列
    # ==================================================================

    async def create_study_plan(self, plan: dict[str, Any]) -> str:
        """创建一条学习计划记录。

        kp_sequence 字段如果在 Python 中是列表，自动序列化为 JSON 字符串存储。

        Args:
            plan: 至少包含 plan_id、student_id、start_date、created_at、updated_at。

        Returns:
            str: plan_id。
        """
        assert self._conn is not None

        # 序列化 kp_sequence（知识点 ID 列表 → JSON 字符串）
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
        """按 ID 查询单个学习计划。

        kp_sequence 字段从 JSON 字符串反序列化为 Python 列表。
        """
        assert self._conn is not None
        cursor = await self._conn.execute(
            "SELECT * FROM study_plans WHERE plan_id = ?", (plan_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        result = self._row_to_dict(row)
        # 反序列化 kp_sequence：JSON 字符串 → Python 列表
        try:
            result["kp_sequence"] = json.loads(result.get("kp_sequence", "[]"))
        except (json.JSONDecodeError, TypeError):
            result["kp_sequence"] = []
        return result
