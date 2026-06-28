"""Knowledge Engine — 知识点解析、关系管理和掌握度追踪。

将 LLM 调用、知识点 CRUD 和前置/后继关系维护封装为高层次的
业务逻辑组件，供 orchestration 层直接使用。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from super_tutor.core.database import Database
from super_tutor.core.exceptions import LLMError, MaterialError
from super_tutor.core.llm_client import LLMClient
from super_tutor.models.knowledge import KnowledgePoint

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 默认 prompt 路径
# ---------------------------------------------------------------------------
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_DEFAULT_PARSE_PROMPT = _PROMPTS_DIR / "parse_knowledge.md"


class KnowledgeEngine:
    """知识点引擎 — 解析、查询和掌握度管理。

    封装了 LLM 解析、数据库 CRUD 和前置/后继关系
    的双向同步逻辑。

    Usage::

        engine = KnowledgeEngine(db, llm_client)
        kps = await engine.parse(content, "physics", "material-001")
        prereqs = await engine.get_prerequisites("kp-003")
    """

    def __init__(
        self,
        db: Database,
        llm_client: LLMClient,
        parse_prompt_path: str | None = None,
    ) -> None:
        """Initialise the engine.

        Args:
            db: An initialised ``Database`` instance.
            llm_client: An ``LLMClient`` instance for LLM calls.
            parse_prompt_path: Optional path to a custom parse prompt
                template.  Defaults to ``prompts/parse_knowledge.md``.
        """
        self._db = db
        self._llm = llm_client
        self._prompt_path = parse_prompt_path or str(_DEFAULT_PARSE_PROMPT)

    # ==================================================================
    # Parse — LLM-driven knowledge point extraction
    # ==================================================================

    async def parse(
        self,
        content: str,
        course_type: str,
        material_id: str,
    ) -> list[KnowledgePoint]:
        """Parse *content* into structured knowledge points.

        Uses the LLM to identify knowledge point boundaries, evaluate
        difficulty, and detect prerequisite relationships.  Results are
        batch-inserted into the database, and prerequisite/successor
        links are written bidirectionally.

        Args:
            content: The raw text content of a learning material.
            course_type: e.g. ``"physics"``, ``"mathematics"``.
            material_id: The ``material_id`` this content belongs to.

        Returns:
            The list of newly created ``KnowledgePoint`` objects in
            dependency order (no-prereq first).

        Raises:
            MaterialError: If the LLM response cannot be parsed or the
                parse result is empty.
        """
        # -- 1. Load system prompt ------------------------------------------
        try:
            system_prompt = Path(self._prompt_path).read_text(encoding="utf-8")
        except OSError as exc:
            raise MaterialError(
                f"无法加载知识点解析提示词: {self._prompt_path} ({exc})"
            ) from exc

        # -- 2. Build messages & call LLM -----------------------------------
        user_prompt = (
            f"## 教材内容\n\n课程类型: {course_type}\n\n"
            f"{content}\n\n"
            f"请按 JSON 格式输出所有知识点的解析结果。"
        )
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        logger.info("开始解析教材 %s (course_type=%s, 内容长度=%d)...",
                     material_id, course_type, len(content))

        try:
            raw = await self._llm.chat(
                messages=messages,
                temperature=0.3,
                max_tokens=8192,
                timeout=180,
            )
        except LLMError as exc:
            raise MaterialError(
                f"LLM 调用失败 (material_id={material_id}): {exc}"
            ) from exc

        # -- 3. Parse JSON response -----------------------------------------
        raw = raw.strip()
        # Strip Markdown code fences if present
        if raw.startswith("```"):
            # Remove opening ```json ... ``` fence
            lines = raw.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            raw = "\n".join(lines)

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.error("LLM 返回的 JSON 无法解析: %s", raw[:500])
            raise MaterialError(
                f"知识点解析结果不是有效 JSON: {exc}"
            ) from exc

        raw_kps = data.get("knowledge_points", [])
        if not raw_kps:
            raise MaterialError("LLM 未返回任何知识点，请检查教材内容是否为空或格式是否正确。")

        # -- 4. Insert into DB & build index → kp_id mapping ----------------
        index_to_kp_id: dict[int, str] = {}
        created: list[KnowledgePoint] = []
        now = datetime.now(timezone.utc).isoformat()

        for item in raw_kps:
            kp_id = str(uuid4())
            idx = item.get("index", len(created))
            index_to_kp_id[idx] = kp_id

            kp = KnowledgePoint(
                kp_id=kp_id,
                material_id=material_id,
                title=item.get("title", ""),
                summary=item.get("summary", ""),
                content=item.get("content", ""),
                keywords=item.get("keywords", []),
                difficulty=item.get("difficulty", "medium"),
                course_type=course_type,
                chapter_index=item.get("chapter_index", idx),
                prerequisite_ids=[],   # filled in step 5
                successor_ids=[],      # filled in step 5
                assessment_count=0,
                created_at=now,
                updated_at=now,
            )

            await self._db.insert_knowledge_point(kp.model_dump())
            created.append(kp)

        logger.info("已插入 %d 个知识点 (material_id=%s)", len(created), material_id)

        # -- 5. Resolve & write bidirectional relationships -----------------
        for item in raw_kps:
            idx = item.get("index", 0)
            kp_id = index_to_kp_id.get(idx)
            if kp_id is None:
                continue

            prereq_indices: list[int] = item.get("prerequisite_indices", [])
            prerequisite_ids: list[str] = []
            for pi in prereq_indices:
                prereq_kp_id = index_to_kp_id.get(pi)
                if prereq_kp_id and prereq_kp_id != kp_id:
                    prerequisite_ids.append(prereq_kp_id)

            # Update this KP's prerequisite_ids
            if prerequisite_ids:
                await self._db.update_knowledge_point(
                    kp_id, {"prerequisite_ids": prerequisite_ids}
                )

            # Update each prerequisite's successor_ids (bidirectional)
            for prereq_kp_id in prerequisite_ids:
                prereq = await self._db.get_knowledge_point(prereq_kp_id)
                if prereq is None:
                    continue
                successors: list[str] = _parse_json_list(
                    prereq.get("successor_ids", "[]")
                )
                if kp_id not in successors:
                    successors.append(kp_id)
                    await self._db.update_knowledge_point(
                        prereq_kp_id, {"successor_ids": successors}
                    )

            # Update the in-memory model
            for kp in created:
                if kp.kp_id == kp_id:
                    kp.prerequisite_ids = prerequisite_ids
                if kp.kp_id in prerequisite_ids:
                    if kp_id not in kp.successor_ids:
                        kp.successor_ids.append(kp_id)
        logger.info(
            "已解析 %d 个知识点，建立 %d 条前置关系 (material_id=%s)",
            len(created),
            sum(len(kp.prerequisite_ids) for kp in created),
            material_id,
        )

        return created

    # ==================================================================
    # Query methods
    # ==================================================================

    async def get_by_material(self, material_id: str) -> list[KnowledgePoint]:
        """Return all knowledge points for a given material.

        Args:
            material_id: The material to query.

        Returns:
            A list of ``KnowledgePoint`` objects ordered by page.
        """
        rows = await self._db.list_knowledge_points_by_material(material_id)
        return [_row_to_knowledge_point(r) for r in rows]

    async def get_prerequisites(self, kp_id: str) -> list[KnowledgePoint]:
        """Return the direct prerequisites of a knowledge point.

        Args:
            kp_id: The knowledge point whose prerequisites to fetch.

        Returns:
            A list of ``KnowledgePoint`` objects that must be
            mastered before *kp_id*.
        """
        kp = await self._db.get_knowledge_point(kp_id)
        if kp is None:
            return []
        prereq_ids: list[str] = _parse_json_list(
            kp.get("prerequisite_ids", "[]")
        )
        result: list[KnowledgePoint] = []
        for pid in prereq_ids:
            row = await self._db.get_knowledge_point(pid)
            if row:
                result.append(_row_to_knowledge_point(row))
        return result

    async def get_successors(self, kp_id: str) -> list[KnowledgePoint]:
        """Return the direct successors of a knowledge point.

        Args:
            kp_id: The knowledge point whose successors to fetch.

        Returns:
            A list of ``KnowledgePoint`` objects that depend on
            *kp_id* as a prerequisite.
        """
        kp = await self._db.get_knowledge_point(kp_id)
        if kp is None:
            return []
        successor_ids: list[str] = _parse_json_list(
            kp.get("successor_ids", "[]")
        )
        result: list[KnowledgePoint] = []
        for sid in successor_ids:
            row = await self._db.get_knowledge_point(sid)
            if row:
                result.append(_row_to_knowledge_point(row))
        return result

    # ==================================================================
    # Mastery
    # ==================================================================

    async def update_mastery(self, kp_id: str, score: float) -> None:
        """Update the mastery level of a knowledge point.

        Args:
            kp_id: The knowledge point to update.
            score: New mastery level (0.0 – 1.0).  Values outside
                this range are clamped.
        """
        clamped = max(0.0, min(1.0, score))
        await self._db.upsert_knowledge_point_mastery(kp_id, clamped)
        logger.debug("Updated mastery for %s → %.2f", kp_id, clamped)


# ==================================================================
# Internal helpers
# ==================================================================


def _parse_json_list(raw: str | list) -> list[str]:
    """Parse a JSON-encoded list or return the list unchanged."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(item) for item in parsed]
        except json.JSONDecodeError:
            pass
    return []


def _row_to_knowledge_point(row: dict) -> KnowledgePoint:
    """Convert a database row dict to a ``KnowledgePoint`` model."""
    return KnowledgePoint(
        kp_id=row.get("kp_id", ""),
        material_id=row.get("material_id", ""),
        title=row.get("title", ""),
        summary=row.get("summary", ""),
        content=row.get("content", ""),
        keywords=_parse_json_list(row.get("keywords", "[]")),
        difficulty=row.get("difficulty", "medium"),
        course_type=row.get("course_type", ""),
        chapter_index=row.get("chapter_index", 0),
        prerequisite_ids=_parse_json_list(row.get("prerequisite_ids", "[]")),
        successor_ids=_parse_json_list(row.get("successor_ids", "[]")),
        mastery_level=row.get("mastery_level", 0.0),
        assessment_count=row.get("assessment_count", 0),
        created_at=row.get("created_at", ""),
        updated_at=row.get("updated_at", ""),
    )
