"""SQLite + sqlite-vec persistence layer for Super Tutor Agent.

Provides structured artifact storage, vector-based semantic search, token-usage
tracking, and Git commit logging.  Uses aiosqlite for async I/O and attempts to
load the sqlite-vec extension for vector search; falls back gracefully to SQL
``LIKE`` queries on ``summary_256`` when the extension is unavailable.
"""

from __future__ import annotations

import json
import logging
import os
import struct
from typing import Any, Optional, Sequence
import uuid

import aiosqlite

from super_tutor.config import TutorConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional OpenAI SDK for embedding generation
# ---------------------------------------------------------------------------
try:
    from openai import AsyncOpenAI

    _HAS_OPENAI = True
except ImportError:  # pragma: no cover
    _HAS_OPENAI = False

# ---------------------------------------------------------------------------
# Try to locate the sqlite-vec loadable extension at import time.
# The Python ``sqlite-vec`` wheel ships the shared library; we probe a few
# common module layouts so the extension can be loaded even when the wheel
# was installed in a non-standard location.
# ---------------------------------------------------------------------------
_VEC_EXTENSION_PATH: Optional[str] = None
try:
    import sqlite_vec  # type: ignore[import-untyped]

    _VEC_EXTENSION_PATH = sqlite_vec.vec_path()  # type: ignore[attr-defined]
except Exception:
    try:
        import importlib.resources

        _VEC_EXTENSION_PATH = str(
            importlib.resources.files("sqlite_vec") / "vec0"
        )
    except Exception:
        _VEC_EXTENSION_PATH = None


# Default embedding dimension used when one cannot be inferred from the API
# response (1536 = text-embedding-ada-002 / many open-source models).
_DEFAULT_EMBEDDING_DIM = 1536


class Database:
    """Async SQLite database manager for a Super Tutor teaching session.

    Each session owns one ``super_tutor.db`` file managed through this class.
    The database contains five tables:

    * **projects** – project-scoped metadata.
    * **artifacts** – structured summaries of AI-produced outputs.
    * **task_log** – audit trail of workflow actions.
    * **token_usage** – per-call token consumption records.
    * **git_commits** – log of automatic Git commits.

    When the *sqlite-vec* extension is available a ``vec_artifacts`` virtual
    table is created for KNN vector search.  Otherwise semantic search degrades
    to SQL ``LIKE`` matching on the ``summary_256`` column.

    Attributes:
        db_path: Absolute path to the SQLite database file.
        config: TutorConfig instance providing API keys and defaults.
        vec_available: ``True`` when sqlite-vec was loaded and the virtual
            table was created successfully.
        embedding_dim: Dimension of the embedding vectors (inferred from the
            first API response or set to a sensible default).
    """

    # -- DDL (Data Definition Language) ---------------------------------------

    _DDL_PROJECTS = """
    CREATE TABLE IF NOT EXISTS projects (
        id           TEXT PRIMARY KEY,
        name         TEXT    NOT NULL,
        path         TEXT    NOT NULL DEFAULT '',
        status       TEXT    NOT NULL DEFAULT 'active',
        created_at   TEXT    NOT NULL,
        updated_at   TEXT    NOT NULL,
        repo_url     TEXT,
        summary      TEXT    NOT NULL DEFAULT ''
    );
    """

    _DDL_ARTIFACTS = """
    CREATE TABLE IF NOT EXISTS artifacts (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        artifact_uuid TEXT   NOT NULL UNIQUE,
        project_id   TEXT    NOT NULL,
        role         TEXT    NOT NULL,
        type         TEXT    NOT NULL,
        module       TEXT,
        title        TEXT    NOT NULL DEFAULT '',
        summary_256  TEXT    NOT NULL DEFAULT '',
        full_text    TEXT    NOT NULL,
        file_path    TEXT,
        version      INTEGER NOT NULL DEFAULT 1,
        created_at   TEXT    NOT NULL,
        embedding    BLOB,
        parent_id    TEXT,
        FOREIGN KEY (parent_id) REFERENCES artifacts(artifact_uuid)
    );
    CREATE INDEX IF NOT EXISTS idx_artifacts_project_id
        ON artifacts(project_id);
    CREATE INDEX IF NOT EXISTS idx_artifacts_role
        ON artifacts(role);
    CREATE INDEX IF NOT EXISTS idx_artifacts_type
        ON artifacts(type);
    CREATE INDEX IF NOT EXISTS idx_artifacts_module
        ON artifacts(module);
    CREATE INDEX IF NOT EXISTS idx_artifacts_parent_id
        ON artifacts(parent_id);
    """

    _DDL_TASK_LOG = """
    CREATE TABLE IF NOT EXISTS task_log (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id   TEXT    NOT NULL,
        task_id      TEXT,
        module       TEXT    NOT NULL,
        role         TEXT    NOT NULL,
        from_role    TEXT    NOT NULL,
        to_role      TEXT    NOT NULL,
        action       TEXT    NOT NULL,
        status       TEXT    NOT NULL DEFAULT 'pending',
        message      TEXT,
        metadata     TEXT,
        artifact_id  TEXT,
        created_at   TEXT    NOT NULL,
        FOREIGN KEY (artifact_id) REFERENCES artifacts(artifact_uuid)
    );
    CREATE INDEX IF NOT EXISTS idx_task_log_project_id
        ON task_log(project_id);
    CREATE INDEX IF NOT EXISTS idx_task_log_role
        ON task_log(role);
    CREATE INDEX IF NOT EXISTS idx_task_log_artifact_id
        ON task_log(artifact_id);
    """

    _DDL_TOKEN_USAGE = """
    CREATE TABLE IF NOT EXISTS token_usage (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id        TEXT    NOT NULL,
        role              TEXT    NOT NULL,
        task_id           TEXT,
        model             TEXT,
        tier              TEXT,
        prompt_tokens     INTEGER NOT NULL DEFAULT 0,
        completion_tokens INTEGER NOT NULL DEFAULT 0,
        total_tokens      INTEGER NOT NULL DEFAULT 0,
        cost_estimate     REAL,
        created_at        TEXT    NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_token_usage_project_id
        ON token_usage(project_id);
    CREATE INDEX IF NOT EXISTS idx_token_usage_role
        ON token_usage(role);
    """

    _DDL_GIT_COMMITS = """
    CREATE TABLE IF NOT EXISTS git_commits (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id    TEXT    NOT NULL,
        module        TEXT,
        sha           TEXT    UNIQUE NOT NULL,
        message       TEXT    NOT NULL,
        author        TEXT    DEFAULT 'forge',
        branch        TEXT,
        files_changed TEXT,
        created_at    TEXT    NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_git_commits_project_id
        ON git_commits(project_id);
    CREATE INDEX IF NOT EXISTS idx_git_commits_module
        ON git_commits(module);
    """

    # -- Super Tutor 专用表 -------------------------------------------------

    _DDL_KNOWLEDGE_CHUNKS = """
    CREATE TABLE IF NOT EXISTS knowledge_chunks (
        chunk_id     TEXT PRIMARY KEY,
        material_id  TEXT    NOT NULL,
        content      TEXT    NOT NULL,
        summary      TEXT    NOT NULL DEFAULT '',
        topic        TEXT    NOT NULL DEFAULT '',
        difficulty   TEXT    NOT NULL DEFAULT 'medium',
        keywords     TEXT    NOT NULL DEFAULT '[]',
        page_start   INTEGER,
        page_end     INTEGER,
        embedding    BLOB,
        metadata     TEXT    NOT NULL DEFAULT '{}',
        created_at   TEXT    NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_chunks_material_id
        ON knowledge_chunks(material_id);
    CREATE INDEX IF NOT EXISTS idx_chunks_topic
        ON knowledge_chunks(topic);
    CREATE INDEX IF NOT EXISTS idx_chunks_difficulty
        ON knowledge_chunks(difficulty);
    """

    _DDL_QUESTIONS = """
    CREATE TABLE IF NOT EXISTS questions (
        question_id        TEXT PRIMARY KEY,
        session_id         TEXT,
        type               TEXT    NOT NULL,
        difficulty         TEXT    NOT NULL DEFAULT 'medium',
        subject            TEXT    NOT NULL DEFAULT '',
        topic              TEXT    NOT NULL DEFAULT '',
        stem               TEXT    NOT NULL,
        options            TEXT    NOT NULL DEFAULT '[]',
        correct_answer     TEXT    NOT NULL,
        explanation        TEXT    NOT NULL DEFAULT '',
        chunk_ids          TEXT    NOT NULL DEFAULT '[]',
        knowledge_node_ids TEXT    NOT NULL DEFAULT '[]',
        estimated_seconds  INTEGER NOT NULL DEFAULT 120,
        points             REAL    NOT NULL DEFAULT 1.0,
        tags               TEXT    NOT NULL DEFAULT '[]',
        metadata           TEXT    NOT NULL DEFAULT '{}',
        created_at         TEXT    NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_questions_session_id
        ON questions(session_id);
    CREATE INDEX IF NOT EXISTS idx_questions_topic
        ON questions(topic);
    CREATE INDEX IF NOT EXISTS idx_questions_difficulty
        ON questions(difficulty);
    CREATE INDEX IF NOT EXISTS idx_questions_type
        ON questions(type);
    """

    _DDL_QUIZ_ATTEMPTS = """
    CREATE TABLE IF NOT EXISTS quiz_attempts (
        attempt_id        TEXT PRIMARY KEY,
        session_id        TEXT    NOT NULL,
        question_id       TEXT    NOT NULL,
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
    CREATE INDEX IF NOT EXISTS idx_attempts_session_id
        ON quiz_attempts(session_id);
    CREATE INDEX IF NOT EXISTS idx_attempts_question_id
        ON quiz_attempts(question_id);
    CREATE INDEX IF NOT EXISTS idx_attempts_is_correct
        ON quiz_attempts(is_correct);
    """

    # ------------------------------------------------------------------

    def __init__(
        self,
        db_path: str,
        config: Optional[TutorConfig] = None,
        projects_root: Optional[str] = None,
    ) -> None:
        """Initialise the Database manager.

        Args:
            db_path: Path to the SQLite database file (e.g.
                ``/home/user/super-tutor/sessions/my_session/super_tutor.db``).
            config: TutorConfig instance.  When *None* the singleton is used.
            projects_root: Optional project root directory to scope *db_path*
                within.  When provided the resolved *db_path* must reside
                inside this root.

        Raises:
            ValueError: If *db_path* is outside *projects_root* or the parent
                directory does not exist.
        """
        self.db_path: str = self._validate_db_path(db_path, projects_root)
        self.config: TutorConfig = (
            config if config is not None else TutorConfig.get_instance()
        )

        self.vec_available: bool = False
        self.embedding_dim: int = _DEFAULT_EMBEDDING_DIM

        self._conn: Optional[aiosqlite.Connection] = None
        self._openai_client: Optional[AsyncOpenAI] = None

    # ==================================================================
    # Lifecycle
    # ==================================================================

    @staticmethod
    def _validate_db_path(db_path: str, projects_root: Optional[str] = None) -> str:
        """Validate and resolve the database path.

        Ensures the parent directory exists and (when *projects_root* is given)
        that the resolved path resides inside the allowed project root.

        Args:
            db_path: Requested database file path (may be relative or absolute).
            projects_root: Optional project root directory to scope the path within.

        Returns:
            The resolved absolute path.

        Raises:
            ValueError: If the path escapes the allowed root or the parent
                directory does not exist.
        """
        resolved = os.path.abspath(os.path.expanduser(db_path))
        parent_dir = os.path.dirname(resolved)

        if not os.path.isdir(parent_dir):
            raise ValueError(
                f"Parent directory for database does not exist: {parent_dir}"
            )

        if projects_root is not None:
            root = os.path.abspath(os.path.expanduser(projects_root))
            if not resolved.startswith(root + os.sep) and resolved != root:
                raise ValueError(
                    f"Database path {resolved!r} is outside the project root "
                    f"{root!r}."
                )

        return resolved

    async def initialize(self) -> None:
        """Open the database, create all tables, and load sqlite-vec.

        This method is idempotent: calling it multiple times is safe (the
        connection is reused once opened).  Tables use ``IF NOT EXISTS`` so
        existing data is never overwritten.

        Raises:
            RuntimeError: If the database connection cannot be established.
            ValueError: If the database path is invalid (see
                :meth:`_validate_db_path`).
        """
        if self._conn is not None:
            return  # already initialised

        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        # Enable WAL mode for better concurrent read performance.
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA foreign_keys=ON;")

        await self._create_tables()
        await self._init_vector_search()

    async def close(self) -> None:
        """Close the database connection gracefully.

        Safe to call even if ``initialize`` was never called.
        """
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
            self.vec_available = False

    # ==================================================================
    # Internal: table creation & extension loading
    # ==================================================================

    async def _create_tables(self) -> None:
        """Execute all DDL statements to ensure required tables exist."""
        assert self._conn is not None
        ddl_statements = [
            self._DDL_PROJECTS,
            self._DDL_ARTIFACTS,
            self._DDL_TASK_LOG,
            self._DDL_TOKEN_USAGE,
            self._DDL_GIT_COMMITS,
            self._DDL_KNOWLEDGE_CHUNKS,
            self._DDL_QUESTIONS,
            self._DDL_QUIZ_ATTEMPTS,
        ]
        for ddl in ddl_statements:
            await self._conn.executescript(ddl)
        await self._conn.commit()

    async def _init_vector_search(self) -> None:
        """Attempt to load sqlite-vec and create the vec0 virtual table.

        On success ``self.vec_available`` is set to ``True`` and
        ``vec_artifacts`` becomes usable.  On any failure a warning is
        logged and the flag stays ``False`` – semantic search will
        transparently fall back to SQL ``LIKE``.
        """
        if _VEC_EXTENSION_PATH is None:
            logger.warning(
                "sqlite-vec extension not found; semantic search will use LIKE fallback."
            )
            self.vec_available = False
            return

        assert self._conn is not None
        try:
            # aiosqlite wraps the stdlib sqlite3 connection; enable extension
            # loading on the underlying driver connection and load the vec
            # extension.
            driver_conn = self._conn._connection  # type: ignore[attr-defined]
            driver_conn.enable_load_extension(True)
            driver_conn.load_extension(_VEC_EXTENSION_PATH)
            driver_conn.enable_load_extension(False)

            # Create the virtual table.  The dimension must match the
            # embedding model; we use the configured / detected value.
            await self._conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_artifacts "
                f"USING vec0(embedding float[{self.embedding_dim}]);"
            )
            await self._conn.commit()
            self.vec_available = True
            logger.info("sqlite-vec loaded; vector search is available.")
        except Exception as exc:
            logger.warning(
                "Failed to load sqlite-vec extension (%s); "
                "semantic search will use LIKE fallback.",
                exc,
            )
            self.vec_available = False

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

    def _embedding_to_blob(self, embedding: list[float]) -> bytes:
        """Pack a list of floats into a BLOB for SQLite storage."""
        return struct.pack(f"{len(embedding)}f", *embedding)

    def _blob_to_embedding(self, blob: bytes) -> list[float]:
        """Unpack a BLOB back into a list of floats."""
        count = len(blob) // 4
        return list(struct.unpack(f"{count}f", blob))

    # ==================================================================
    # Project CRUD
    # ==================================================================

    async def get_project(self, project_id: str) -> Optional[dict[str, Any]]:
        """Retrieve a single project by its identifier.

        Args:
            project_id: The project UUID.

        Returns:
            A dict with project fields, or *None* if not found.
        """
        assert self._conn is not None
        cursor = await self._conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        )
        row = await cursor.fetchone()
        return self._row_to_dict(row) if row else None

    async def list_projects(
        self, status: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """List all projects, optionally filtered by status.

        Args:
            status: If provided, only projects with this status are returned
                (e.g. ``"active"``, ``"archived"``, ``"completed"``).

        Returns:
            A list of project dicts (may be empty).
        """
        assert self._conn is not None
        if status is not None:
            cursor = await self._conn.execute(
                "SELECT * FROM projects WHERE status = ? ORDER BY created_at DESC",
                (status,),
            )
        else:
            cursor = await self._conn.execute(
                "SELECT * FROM projects ORDER BY created_at DESC"
            )
        rows = await cursor.fetchall()
        return self._rows_to_dicts(rows)

    async def create_project(self, project: dict[str, Any]) -> str:
        """Insert a new project record.

        Args:
            project: A dict with at least ``"id"``, ``"name"``, ``"path"``,
                ``"created_at"``, ``"updated_at"``.  Optional keys: ``"status"``,
                ``"repo_url"``, ``"summary"``.

        Returns:
            The ``project_id`` of the newly created row.
        """
        assert self._conn is not None
        await self._conn.execute(
            """INSERT INTO projects (id, name, path, status, created_at,
               updated_at, repo_url, summary)
               VALUES (:id, :name, :path, :status, :created_at,
                       :updated_at, :repo_url, :summary)""",
            {
                "id": project["id"],
                "name": project["name"],
                "path": project.get("path", ""),
                "status": project.get("status", "active"),
                "created_at": project["created_at"],
                "updated_at": project["updated_at"],
                "repo_url": project.get("repo_url"),
                "summary": project.get("summary", ""),
            },
        )
        await self._conn.commit()
        return project["id"]

    # ==================================================================
    # Artifact CRUD + search
    # ==================================================================

    async def insert_artifact(self, artifact: dict[str, Any]) -> str:
        """Insert an artifact and generate its embedding vector.

        The ``summary_256`` field (or ``title`` as fallback) is sent to the
        DeepSeek embedding API to produce a vector.  Both the vector BLOB and,
        when sqlite-vec is available, a vec0 table entry are stored.

        Args:
            artifact: A dict matching the Pydantic ``Artifact`` model fields.
                Required keys: ``project_id``, ``role``, ``type``,
                ``created_at``, ``full_text``.  Optional: ``artifact_uuid``,
                ``module``, ``title``, ``summary_256``, ``file_path``,
                ``version``, ``parent_id``.

        Returns:
            The ``artifact_uuid`` of the newly inserted row.
        """
        assert self._conn is not None

        artifact_uuid = (
            artifact.get("artifact_uuid")
            or artifact.get("id")
            or str(uuid.uuid4())
        )

        # Generate embedding from the summary text.
        text_for_embedding = artifact.get("summary_256") or artifact.get("title", "")
        embedding: Optional[list[float]] = None
        embedding_blob: Optional[bytes] = None
        if text_for_embedding:
            try:
                embedding = await self._generate_embedding(text_for_embedding)
                embedding_blob = self._embedding_to_blob(embedding)
            except Exception as exc:
                logger.warning("Failed to generate embedding for artifact %s: %s",
                               artifact_uuid, exc)

        cursor = await self._conn.execute(
            """INSERT INTO artifacts
               (artifact_uuid, project_id, role, type, module, title,
                summary_256, full_text, file_path, version, created_at,
                embedding, parent_id)
               VALUES
               (:artifact_uuid, :project_id, :role, :type, :module, :title,
                :summary_256, :full_text, :file_path, :version, :created_at,
                :embedding, :parent_id)""",
            {
                "artifact_uuid": artifact_uuid,
                "project_id": artifact["project_id"],
                "role": artifact["role"],
                "type": artifact["type"],
                "module": artifact.get("module"),
                "title": artifact.get("title", ""),
                "summary_256": artifact.get("summary_256", ""),
                "full_text": artifact.get("full_text", ""),
                "file_path": artifact.get("file_path"),
                "version": artifact.get("version", 1),
                "created_at": artifact["created_at"],
                "embedding": embedding_blob,
                "parent_id": artifact.get("parent_id"),
            },
        )
        row_id = cursor.lastrowid
        await self._conn.commit()

        # Also insert into the vec0 virtual table when available.
        if self.vec_available and embedding is not None and row_id is not None:
            try:
                embedding_json = json.dumps(embedding)
                await self._conn.execute(
                    "INSERT INTO vec_artifacts (rowid, embedding) VALUES (?, ?)",
                    (row_id, embedding_json),
                )
                await self._conn.commit()
            except Exception as exc:
                logger.warning(
                    "Failed to insert vec0 entry for artifact row %s: %s",
                    row_id, exc,
                )

        return artifact_uuid

    async def get_artifact(
        self, artifact_uuid: str
    ) -> Optional[dict[str, Any]]:
        """Retrieve a single artifact by its UUID.

        Args:
            artifact_uuid: The artifact's business identifier (``artifact_uuid``
                column).

        Returns:
            A dict of artifact fields, or *None*.
        """
        assert self._conn is not None
        cursor = await self._conn.execute(
            "SELECT * FROM artifacts WHERE artifact_uuid = ?",
            (artifact_uuid,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        result = self._row_to_dict(row)
        # Exclude the raw embedding BLOB from the returned dict – callers
        # rarely need it.
        result.pop("embedding", None)
        return result

    async def query_artifacts(
        self,
        project_id: str,
        role: Optional[str] = None,
        type: Optional[str] = None,
        module: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Query artifacts with structured WHERE filters.

        Args:
            project_id: Required project scope.
            role: Optional AI role filter (``"claude-a"``, ``"codex"``,
                ``"claude-b"``).
            type: Optional artifact type filter (``"code"``, ``"audit"``, …).
            module: Optional module name filter.
            limit: Maximum rows to return.
            offset: Number of rows to skip (for pagination).

        Returns:
            A list of artifact dicts (embedding BLOB excluded).
        """
        assert self._conn is not None
        clauses = ["project_id = :project_id"]
        params: dict[str, Any] = {"project_id": project_id}

        if role is not None:
            clauses.append("role = :role")
            params["role"] = role
        if type is not None:
            clauses.append("type = :type")
            params["type"] = type
        if module is not None:
            clauses.append("module = :module")
            params["module"] = module

        where = " AND ".join(clauses)
        sql = (
            f"SELECT id, artifact_uuid, project_id, role, type, module, "
            f"title, summary_256, full_text, file_path, version, created_at, "
            f"parent_id "
            f"FROM artifacts WHERE {where} "
            f"ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
        )
        params["limit"] = limit
        params["offset"] = offset

        cursor = await self._conn.execute(sql, params)
        rows = await cursor.fetchall()
        return self._rows_to_dicts(rows)

    async def update_artifact_summary(
        self, artifact_uuid: str, summary: str
    ) -> None:
        """Update the ``summary_256`` field of an existing artifact.

        Args:
            artifact_uuid: The artifact's business identifier.
            summary: The new summary text (at most 256 characters).

        Raises:
            LookupError: If no artifact with *artifact_uuid* exists.
        """
        assert self._conn is not None
        cursor = await self._conn.execute(
            "UPDATE artifacts SET summary_256 = ? WHERE artifact_uuid = ?",
            (summary, artifact_uuid),
        )
        if cursor.rowcount == 0:
            raise LookupError(
                f"No artifact found with artifact_uuid={artifact_uuid!r}"
            )
        await self._conn.commit()

    async def search_artifacts(
        self,
        project_id: str,
        query: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Semantic (vector) search over artifacts, with LIKE fallback.

        When sqlite-vec is available the method generates a query embedding,
        performs a KNN lookup in ``vec_artifacts``, and returns the matching
        artifacts ordered by cosine distance.  Otherwise it degrades to a
        SQL ``LIKE`` search against ``summary_256``.

        Args:
            project_id: Scope the search to a single project.
            query: Natural-language search query.
            limit: Maximum number of results.

        Returns:
            A list of artifact dicts ordered by relevance (most relevant
            first).  Each dict includes an extra ``_distance`` key when
            vector search was used.
        """
        assert self._conn is not None

        if self.vec_available:
            return await self._vector_search(project_id, query, limit)
        else:
            return await self._fallback_search(project_id, query, limit)

    async def _vector_search(
        self,
        project_id: str,
        query: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """KNN vector search via sqlite-vec.

        Generates an embedding for *query*, then uses the vec0 ``MATCH``
        operator to find the closest vectors in ``vec_artifacts``.
        """
        assert self._conn is not None

        try:
            query_embedding = await self._generate_embedding(query)
        except Exception as exc:
            logger.warning("Embedding generation failed for search; using LIKE: %s", exc)
            return await self._fallback_search(project_id, query, limit)

        embedding_json = json.dumps(query_embedding)
        try:
            cursor = await self._conn.execute(
                """SELECT v.rowid, v.distance
                   FROM vec_artifacts v
                   WHERE v.embedding MATCH ?
                   ORDER BY v.distance
                   LIMIT ?""",
                (embedding_json, limit),
            )
            vec_rows = await cursor.fetchall()
        except Exception as exc:
            logger.warning("Vector search query failed; using LIKE: %s", exc)
            return await self._fallback_search(project_id, query, limit)

        if not vec_rows:
            return []

        # Join back to artifacts on rowid and filter by project_id.
        results: list[dict[str, Any]] = []
        for vec_row in vec_rows:
            artifact_cursor = await self._conn.execute(
                """SELECT id, artifact_uuid, project_id, role, type, module,
                          title, summary_256, full_text, file_path, version,
                          created_at, parent_id
                   FROM artifacts
                   WHERE id = ? AND project_id = ?""",
                (vec_row["rowid"], project_id),
            )
            art_row = await artifact_cursor.fetchone()
            if art_row is not None:
                art_dict = self._row_to_dict(art_row)
                art_dict["_distance"] = vec_row["distance"]
                results.append(art_dict)

        return results

    async def _fallback_search(
        self,
        project_id: str,
        query: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Keyword-based fallback search using SQL LIKE.

        Splits *query* into whitespace-delimited tokens and matches each
        against ``summary_256`` (case-insensitive).  Results are ranked by
        how many tokens matched.
        """
        assert self._conn is not None

        tokens = query.strip().split()
        if not tokens:
            # No meaningful query – return the most recent artifacts.
            cursor = await self._conn.execute(
                """SELECT id, artifact_uuid, project_id, role, type, module,
                          title, summary_256, full_text, file_path, version,
                          created_at, parent_id
                   FROM artifacts
                   WHERE project_id = ?
                   ORDER BY created_at DESC
                   LIMIT ?""",
                (project_id, limit),
            )
            rows = await cursor.fetchall()
            return self._rows_to_dicts(rows)

        # Build conditions:
        # - score_clauses: sum of CASE WHEN for counting matched tokens.
        # - where_clauses: OR-connected LIKE filters.
        score_clauses: list[str] = []
        score_params: list[str] = []
        where_clauses: list[str] = []
        where_params: list[str] = []
        for token in tokens:
            param_value = f"%{token}%"
            score_clauses.append(
                f"CASE WHEN summary_256 LIKE ? THEN 1 ELSE 0 END"
            )
            score_params.append(param_value)
            where_clauses.append("summary_256 LIKE ?")
            where_params.append(param_value)

        score_expr = " + ".join(score_clauses)
        where_cond = " OR ".join(where_clauses)

        sql = (
            f"SELECT id, artifact_uuid, project_id, role, type, module, "
            f"       title, summary_256, full_text, file_path, version, "
            f"       created_at, parent_id, ({score_expr}) AS _score "
            f"FROM artifacts "
            f"WHERE project_id = ? AND ({where_cond}) "
            f"ORDER BY _score DESC, created_at DESC "
            f"LIMIT ?"
        )
        # Param order: score placeholders → project_id → where placeholders → limit
        params_all: list[Any] = (
            score_params + [project_id] + where_params + [limit]
        )

        cursor = await self._conn.execute(sql, params_all)
        rows = await cursor.fetchall()
        return self._rows_to_dicts(rows)

    # ==================================================================
    # Embedding generation
    # ==================================================================

    async def _generate_embedding(self, text: str) -> list[float]:
        """Generate an embedding vector for *text* via DeepSeek API.

        Uses the OpenAI-compatible SDK pointed at the configured DeepSeek
        base URL.  The client is lazily created on first use.

        Args:
            text: The input text to embed.

        Returns:
            A list of floating-point values representing the embedding.

        Raises:
            RuntimeError: If the ``openai`` package is not installed.
            Exception: On API or network errors (propagated to caller).
        """
        if not _HAS_OPENAI:
            raise RuntimeError(
                "The 'openai' package is required for embedding generation. "
                "Install it with: pip install openai"
            )

        if self._openai_client is None:
            self._openai_client = AsyncOpenAI(
                api_key=self.config.deepseek_api_key,
                base_url=self.config.deepseek_base_url,
            )

        response = await self._openai_client.embeddings.create(
            model="deepseek-embedding",
            input=text,
        )

        embedding = response.data[0].embedding
        # Cache the dimension so the vec0 table can be created correctly on
        # the next initialise call.
        if embedding:
            self.embedding_dim = len(embedding)

        return embedding

    # ==================================================================
    # Token usage
    # ==================================================================

    async def log_token_usage(self, usage: dict[str, Any]) -> None:
        """Record a single token-consumption event.

        Args:
            usage: A dict with keys ``project_id``, ``role`` (required), and
                optionally ``task_id``, ``model``, ``tier``, ``prompt_tokens``,
                ``completion_tokens``, ``total_tokens``, ``cost_estimate``,
                ``created_at``.
        """
        assert self._conn is not None
        await self._conn.execute(
            """INSERT INTO token_usage
               (project_id, role, task_id, model, tier, prompt_tokens,
                completion_tokens, total_tokens, cost_estimate, created_at)
               VALUES
               (:project_id, :role, :task_id, :model, :tier, :prompt_tokens,
                :completion_tokens, :total_tokens, :cost_estimate, :created_at)""",
            {
                "project_id": usage["project_id"],
                "role": usage["role"],
                "task_id": usage.get("task_id"),
                "model": usage.get("model"),
                "tier": usage.get("tier"),
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
                "cost_estimate": usage.get("cost_estimate"),
                "created_at": usage.get("created_at", ""),
            },
        )
        await self._conn.commit()

    async def get_token_stats(self, project_id: str) -> dict[str, Any]:
        """Return token consumption aggregated by role for a project.

        Args:
            project_id: The project to query.

        Returns:
            A dict with keys:
            * ``project_id`` – the requested project.
            * ``total_tokens`` – sum of all tokens consumed.
            * ``by_role`` – dict mapping role name to total tokens.
            * ``call_count`` – total number of logged calls.
        """
        assert self._conn is not None

        # Total across all roles.
        cursor = await self._conn.execute(
            """SELECT COALESCE(SUM(total_tokens), 0) AS total,
                      COUNT(*) AS call_count
               FROM token_usage
               WHERE project_id = ?""",
            (project_id,),
        )
        total_row = await cursor.fetchone()

        # Per-role breakdown.
        cursor = await self._conn.execute(
            """SELECT role, COALESCE(SUM(total_tokens), 0) AS tokens
               FROM token_usage
               WHERE project_id = ?
               GROUP BY role""",
            (project_id,),
        )
        role_rows = await cursor.fetchall()
        by_role: dict[str, int] = {r["role"]: r["tokens"] for r in role_rows}

        return {
            "project_id": project_id,
            "total_tokens": total_row["total"] if total_row else 0,
            "by_role": by_role,
            "call_count": total_row["call_count"] if total_row else 0,
        }

    # ==================================================================
    # Git commits
    # ==================================================================

    async def log_commit(self, commit: dict[str, Any]) -> None:
        """Record a Git commit event.

        Args:
            commit: A dict with keys ``project_id``, ``sha``, ``message``
                (required), and optionally ``module``, ``author``, ``branch``,
                ``files_changed``, ``created_at``.  ``files_changed`` is
                stored as a JSON string when it is a list.
        """
        assert self._conn is not None

        files = commit.get("files_changed")
        if isinstance(files, list):
            files = json.dumps(files)

        await self._conn.execute(
            """INSERT INTO git_commits
               (project_id, module, sha, message, author, branch, files_changed,
                created_at)
               VALUES
               (:project_id, :module, :sha, :message, :author, :branch,
                :files_changed, :created_at)""",
            {
                "project_id": commit["project_id"],
                "module": commit.get("module"),
                "sha": commit["sha"],
                "message": commit["message"],
                "author": commit.get("author", "forge"),
                "branch": commit.get("branch"),
                "files_changed": files,
                "created_at": commit.get("created_at", ""),
            },
        )
        await self._conn.commit()

    async def list_commits(
        self,
        project_id: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """List recent Git commits for a project.

        Args:
            project_id: The project to query.
            limit: Maximum number of commits to return (most recent first).

        Returns:
            A list of commit dicts.  The ``files_changed`` field is
            deserialised from JSON when present.
        """
        assert self._conn is not None
        cursor = await self._conn.execute(
            """SELECT id, project_id, module, sha, message, author, branch,
                      files_changed, created_at
               FROM git_commits
               WHERE project_id = ?
               ORDER BY created_at DESC
               LIMIT ?""",
            (project_id, limit),
        )
        rows = await cursor.fetchall()
        results = self._rows_to_dicts(rows)
        for r in results:
            if isinstance(r.get("files_changed"), str):
                try:
                    r["files_changed"] = json.loads(r["files_changed"])
                except (json.JSONDecodeError, TypeError):
                    pass
        return results

    # ==================================================================
    # Knowledge Chunk CRUD
    # ==================================================================

    async def insert_chunk(self, chunk: dict[str, Any]) -> str:
        """Insert a knowledge chunk and optionally generate its embedding.

        Args:
            chunk: Dict with keys matching ``KnowledgeChunk`` model fields.
                Required: ``chunk_id``, ``material_id``, ``content``,
                ``created_at``.

        Returns:
            The ``chunk_id`` of the inserted row.
        """
        assert self._conn is not None
        keywords = chunk.get("keywords", [])
        if isinstance(keywords, list):
            keywords = json.dumps(keywords)
        metadata = chunk.get("metadata", {})
        if isinstance(metadata, dict):
            metadata = json.dumps(metadata)

        # Generate embedding from summary (or content as fallback).
        embedding_blob: Optional[bytes] = None
        text_to_embed = chunk.get("summary") or chunk.get("content", "")
        if text_to_embed and _HAS_OPENAI:
            try:
                embedding = await self._generate_embedding(text_to_embed)
                embedding_blob = self._embedding_to_blob(embedding)
            except Exception as exc:
                logger.warning("Failed to generate embedding for chunk: %s", exc)

        await self._conn.execute(
            """INSERT INTO knowledge_chunks
               (chunk_id, material_id, content, summary, topic, difficulty,
                keywords, page_start, page_end, embedding, metadata, created_at)
               VALUES
               (:chunk_id, :material_id, :content, :summary, :topic, :difficulty,
                :keywords, :page_start, :page_end, :embedding, :metadata, :created_at)""",
            {
                "chunk_id": chunk["chunk_id"],
                "material_id": chunk["material_id"],
                "content": chunk["content"],
                "summary": chunk.get("summary", ""),
                "topic": chunk.get("topic", ""),
                "difficulty": chunk.get("difficulty", "medium"),
                "keywords": keywords,
                "page_start": chunk.get("page_start"),
                "page_end": chunk.get("page_end"),
                "embedding": embedding_blob,
                "metadata": metadata,
                "created_at": chunk["created_at"],
            },
        )
        await self._conn.commit()
        return chunk["chunk_id"]

    async def get_chunk(self, chunk_id: str) -> Optional[dict[str, Any]]:
        """Retrieve a single knowledge chunk by ID."""
        assert self._conn is not None
        cursor = await self._conn.execute(
            "SELECT * FROM knowledge_chunks WHERE chunk_id = ?", (chunk_id,)
        )
        row = await cursor.fetchone()
        return self._row_to_dict(row) if row else None

    async def list_chunks_by_material(
        self, material_id: str
    ) -> list[dict[str, Any]]:
        """List all chunks belonging to a material, ordered by page."""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """SELECT * FROM knowledge_chunks
               WHERE material_id = ?
               ORDER BY page_start ASC, created_at ASC""",
            (material_id,),
        )
        rows = await cursor.fetchall()
        return self._rows_to_dicts(rows)

    async def search_chunks_by_topic(
        self, topic: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Find chunks matching a topic tag (substring match)."""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """SELECT * FROM knowledge_chunks
               WHERE topic LIKE ?
               ORDER BY created_at DESC
               LIMIT ?""",
            (f"%{topic}%", limit),
        )
        rows = await cursor.fetchall()
        return self._rows_to_dicts(rows)

    # ==================================================================
    # Question CRUD
    # ==================================================================

    async def insert_question(self, question: dict[str, Any]) -> str:
        """Insert a quiz question.

        Args:
            question: Dict with keys matching ``Question`` model fields.
                Required: ``question_id``, ``type``, ``stem``,
                ``correct_answer``, ``created_at``.

        Returns:
            The ``question_id`` of the inserted row.
        """
        assert self._conn is not None

        def _json_field(value: Any) -> str:
            """Serialize list/dict fields to JSON string for SQLite storage."""
            if isinstance(value, (list, dict)):
                return json.dumps(value, ensure_ascii=False)
            return str(value) if value else "[]"

        await self._conn.execute(
            """INSERT INTO questions
               (question_id, session_id, type, difficulty, subject, topic,
                stem, options, correct_answer, explanation, chunk_ids,
                knowledge_node_ids, estimated_seconds, points, tags, metadata,
                created_at)
               VALUES
               (:question_id, :session_id, :type, :difficulty, :subject, :topic,
                :stem, :options, :correct_answer, :explanation, :chunk_ids,
                :knowledge_node_ids, :estimated_seconds, :points, :tags, :metadata,
                :created_at)""",
            {
                "question_id": question["question_id"],
                "session_id": question.get("session_id"),
                "type": question["type"],
                "difficulty": question.get("difficulty", "medium"),
                "subject": question.get("subject", ""),
                "topic": question.get("topic", ""),
                "stem": question["stem"],
                "options": _json_field(question.get("options", [])),
                "correct_answer": _json_field(question["correct_answer"]),
                "explanation": question.get("explanation", ""),
                "chunk_ids": _json_field(question.get("chunk_ids", [])),
                "knowledge_node_ids": _json_field(
                    question.get("knowledge_node_ids", [])
                ),
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

    async def list_questions_by_session(
        self, session_id: str
    ) -> list[dict[str, Any]]:
        """List all questions belonging to a quiz session."""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """SELECT * FROM questions
               WHERE session_id = ?
               ORDER BY created_at ASC""",
            (session_id,),
        )
        rows = await cursor.fetchall()
        return self._rows_to_dicts(rows)

    # ==================================================================
    # Quiz Attempt CRUD
    # ==================================================================

    async def insert_attempt(self, attempt: dict[str, Any]) -> str:
        """Insert a quiz attempt record.

        Args:
            attempt: Dict with keys matching ``QuizAttempt`` model fields.
                Required: ``attempt_id``, ``session_id``, ``question_id``,
                ``started_at``.

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
            """INSERT INTO quiz_attempts
               (attempt_id, session_id, question_id, student_answer, is_correct,
                score, time_spent_seconds, hints_used, attempt_number, confidence,
                misconception_ids, note, started_at, submitted_at, metadata)
               VALUES
               (:attempt_id, :session_id, :question_id, :student_answer, :is_correct,
                :score, :time_spent_seconds, :hints_used, :attempt_number, :confidence,
                :misconception_ids, :note, :started_at, :submitted_at, :metadata)""",
            {
                "attempt_id": attempt["attempt_id"],
                "session_id": attempt["session_id"],
                "question_id": attempt["question_id"],
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

    async def get_attempt(self, attempt_id: str) -> Optional[dict[str, Any]]:
        """Retrieve a single attempt by ID."""
        assert self._conn is not None
        cursor = await self._conn.execute(
            "SELECT * FROM quiz_attempts WHERE attempt_id = ?", (attempt_id,)
        )
        row = await cursor.fetchone()
        return self._row_to_dict(row) if row else None

    async def list_attempts_by_session(
        self, session_id: str
    ) -> list[dict[str, Any]]:
        """List all attempts in a quiz session."""
        assert self._conn is not None
        cursor = await self._conn.execute(
            """SELECT * FROM quiz_attempts
               WHERE session_id = ?
               ORDER BY started_at ASC""",
            (session_id,),
        )
        rows = await cursor.fetchall()
        return self._rows_to_dicts(rows)

    async def update_attempt_grading(
        self,
        attempt_id: str,
        is_correct: bool,
        score: float,
        misconception_ids: Optional[list[str]] = None,
    ) -> None:
        """Update an attempt with grading results after evaluation.

        Args:
            attempt_id: The attempt to update.
            is_correct: Whether the answer was correct.
            score: The awarded score.
            misconception_ids: Optional list of misconception tag IDs.
        """
        assert self._conn is not None
        mis_ids = (
            json.dumps(misconception_ids, ensure_ascii=False)
            if misconception_ids
            else "[]"
        )
        await self._conn.execute(
            """UPDATE quiz_attempts
               SET is_correct = ?, score = ?, misconception_ids = ?
               WHERE attempt_id = ?""",
            (1 if is_correct else 0, score, mis_ids, attempt_id),
        )
        await self._conn.commit()

    async def count_wrong_attempts_by_knowledge_node(
        self, knowledge_node_id: str
    ) -> int:
        """Count wrong attempts related to a specific knowledge node.

        This queries through the questions table to find attempts on questions
        that reference the given knowledge node.
        """
        assert self._conn is not None
        cursor = await self._conn.execute(
            """SELECT COUNT(*) as cnt FROM quiz_attempts qa
               JOIN questions q ON qa.question_id = q.question_id
               WHERE qa.is_correct = 0
                 AND q.knowledge_node_ids LIKE ?""",
            (f"%{knowledge_node_id}%",),
        )
        row = await cursor.fetchone()
        return row["cnt"] if row else 0
