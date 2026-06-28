"""Tests for KnowledgeEngine — parse, query, and mastery operations."""

from __future__ import annotations

import json
from typing import Any

import pytest

from super_tutor.core.database import Database
from super_tutor.core.exceptions import MaterialError
from super_tutor.engine.knowledge_engine import KnowledgeEngine
from tests.conftest import _create_test_material, _insert_test_kp


# ======================================================================
# Minimal fake LLM client
# ======================================================================


class FakeLLMClient:
    """Lightweight test double that returns canned parse results."""

    def __init__(self, canned: dict | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._canned = canned  # Override default response

    async def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 4096,
        timeout: int = 120,
    ) -> str:
        user_msg = ""
        for m in messages:
            if m.get("role") == "user":
                user_msg = m.get("content", "")
                break
        self.calls.append({"user_message": user_msg[:200]})

        if self._canned is not None:
            return json.dumps(self._canned, ensure_ascii=False)

        return json.dumps({
            "knowledge_points": [
                {
                    "index": 0,
                    "title": "牛顿第一定律",
                    "content": "物体在不受外力作用时，保持静止或匀速直线运动状态。",
                    "summary": "惯性定律 — 物体不受外力时保持原有运动状态",
                    "difficulty": "easy",
                    "keywords": ["牛顿第一定律", "惯性", "匀速直线运动"],
                    "prerequisite_indices": [],
                },
                {
                    "index": 1,
                    "title": "牛顿第二定律",
                    "content": "F=ma，物体的加速度与合外力成正比，与质量成反比。",
                    "summary": "加速度定律 — F=ma",
                    "difficulty": "medium",
                    "keywords": ["牛顿第二定律", "F=ma", "力", "加速度", "质量"],
                    "prerequisite_indices": [0],
                },
            ]
        }, ensure_ascii=False)


# ======================================================================
# Tests
# ======================================================================


class TestKnowledgeEngineParse:
    """KnowledgeEngine.parse() tests."""

    async def test_parse_creates_knowledge_points(self, test_db):
        """parse should create KPs in the database from LLM response."""
        mat_id = await _create_test_material(test_db, material_id="mat-parse")

        engine = KnowledgeEngine(db=test_db, llm_client=FakeLLMClient())
        kps = await engine.parse(
            content="牛顿第一定律：物体在不受外力作用时，保持静止或匀速直线运动状态。\n牛顿第二定律：F=ma。",
            course_type="physics",
            material_id=mat_id,
        )

        assert len(kps) == 2
        assert kps[0].title == "牛顿第一定律"
        assert kps[1].title == "牛顿第二定律"
        assert kps[1].prerequisite_ids == [kps[0].kp_id]

        # Verify DB persistence
        rows = await test_db.list_knowledge_points_by_material(mat_id)
        assert len(rows) == 2
        titles = {r["title"] for r in rows}
        assert titles == {"牛顿第一定律", "牛顿第二定律"}

    async def test_parse_stores_bidirectional_relations(self, test_db):
        """parse should write both prerequisite_ids and successor_ids."""
        mat_id = await _create_test_material(test_db, material_id="mat-bi")

        engine = KnowledgeEngine(db=test_db, llm_client=FakeLLMClient())
        kps = await engine.parse(
            content="物理内容",
            course_type="physics",
            material_id=mat_id,
        )

        # KP 1's prerequisite is KP 0
        kp1 = kps[1]
        assert len(kp1.prerequisite_ids) == 1

        # KP 0 should have KP 1 as successor
        kp0_row = await test_db.get_knowledge_point(kps[0].kp_id)
        successor_ids = json.loads(kp0_row["successor_ids"])
        assert kps[1].kp_id in successor_ids

    async def test_parse_empty_result_raises(self, test_db):
        """parse with empty knowledge_points should raise MaterialError."""
        mat_id = await _create_test_material(test_db, material_id="mat-empty")
        fake_llm = FakeLLMClient(canned={"knowledge_points": []})

        engine = KnowledgeEngine(db=test_db, llm_client=fake_llm)
        with pytest.raises(MaterialError, match="未返回任何知识点"):
            await engine.parse(
                content="空内容",
                course_type="physics",
                material_id=mat_id,
            )

    async def test_parse_invalid_json_raises(self, test_db):
        """parse with non-JSON LLM response should raise MaterialError."""

        class BrokenFakeLLM:
            async def chat(self, **kwargs):
                return "这不是 JSON"

        mat_id = await _create_test_material(test_db, material_id="mat-broken")
        engine = KnowledgeEngine(db=test_db, llm_client=BrokenFakeLLM())

        with pytest.raises(MaterialError, match="不是有效 JSON"):
            await engine.parse(
                content="内容",
                course_type="physics",
                material_id=mat_id,
            )

    async def test_parse_with_markdown_fence(self, test_db):
        """parse should strip Markdown code fences from LLM response."""
        mat_id = await _create_test_material(test_db, material_id="mat-fence")

        class FenceFakeLLM:
            async def chat(self, **kwargs):
                return '```json\n{"knowledge_points": [{"index": 0, "title": "测试", "content": "内容", "summary": "摘要", "difficulty": "easy", "keywords": ["k"], "prerequisite_indices": []}]}\n```'

        engine = KnowledgeEngine(db=test_db, llm_client=FenceFakeLLM())
        kps = await engine.parse(
            content="测试内容",
            course_type="test",
            material_id=mat_id,
        )

        assert len(kps) == 1
        assert kps[0].title == "测试"


class TestKnowledgeEngineQuery:
    """KnowledgeEngine query method tests."""

    async def test_get_by_material(self, test_db):
        """get_by_material should return all KPs for a material."""
        mat_id = await _create_test_material(test_db, material_id="mat-query")
        await _insert_test_kp(test_db, kp_id="kp-a", material_id=mat_id, title="A")
        await _insert_test_kp(test_db, kp_id="kp-b", material_id=mat_id, title="B")

        engine = KnowledgeEngine(db=test_db, llm_client=FakeLLMClient())
        kps = await engine.get_by_material(mat_id)

        assert len(kps) == 2
        titles = {kp.title for kp in kps}
        assert titles == {"A", "B"}

    async def test_get_by_material_empty(self, test_db):
        """get_by_material should return empty list for unknown material."""
        engine = KnowledgeEngine(db=test_db, llm_client=FakeLLMClient())
        kps = await engine.get_by_material("nonexistent")
        assert kps == []

    async def test_get_prerequisites(self, test_db):
        """get_prerequisites should return prerequisite KPs."""
        mat_id = await _create_test_material(test_db, material_id="mat-prereq")
        await _insert_test_kp(
            test_db, kp_id="kp-base", material_id=mat_id, title="基础知识",
        )
        await _insert_test_kp(
            test_db, kp_id="kp-adv", material_id=mat_id, title="进阶知识",
            prerequisite_ids=["kp-base"],
        )

        engine = KnowledgeEngine(db=test_db, llm_client=FakeLLMClient())
        prereqs = await engine.get_prerequisites("kp-adv")

        assert len(prereqs) == 1
        assert prereqs[0].kp_id == "kp-base"

    async def test_get_prerequisites_none(self, test_db):
        """get_prerequisites should return empty list for KP with no prereqs."""
        mat_id = await _create_test_material(test_db, material_id="mat-nopre")
        await _insert_test_kp(
            test_db, kp_id="kp-lonely", material_id=mat_id, title="孤立知识点",
        )

        engine = KnowledgeEngine(db=test_db, llm_client=FakeLLMClient())
        prereqs = await engine.get_prerequisites("kp-lonely")
        assert prereqs == []

    async def test_get_prerequisites_nonexistent_kp(self, test_db):
        """get_prerequisites for nonexistent kp_id should return empty list."""
        engine = KnowledgeEngine(db=test_db, llm_client=FakeLLMClient())
        prereqs = await engine.get_prerequisites("nonexistent")
        assert prereqs == []

    async def test_get_successors(self, test_db):
        """get_successors should return successor KPs."""
        mat_id = await _create_test_material(test_db, material_id="mat-succ")
        await _insert_test_kp(
            test_db, kp_id="kp-base2", material_id=mat_id, title="基础",
            successor_ids=["kp-next"],
        )
        await _insert_test_kp(
            test_db, kp_id="kp-next", material_id=mat_id, title="后继",
        )

        engine = KnowledgeEngine(db=test_db, llm_client=FakeLLMClient())
        successors = await engine.get_successors("kp-base2")

        assert len(successors) == 1
        assert successors[0].kp_id == "kp-next"

    async def test_get_successors_none(self, test_db):
        """get_successors should return empty list for KP with no successors."""
        mat_id = await _create_test_material(test_db, material_id="mat-nosucc")
        await _insert_test_kp(
            test_db, kp_id="kp-leaf", material_id=mat_id, title="叶子节点",
        )

        engine = KnowledgeEngine(db=test_db, llm_client=FakeLLMClient())
        successors = await engine.get_successors("kp-leaf")
        assert successors == []


class TestKnowledgeEngineMastery:
    """KnowledgeEngine.update_mastery() tests."""

    async def test_update_mastery(self, test_db):
        """update_mastery should set the mastery level on a KP."""
        mat_id = await _create_test_material(test_db, material_id="mat-mast")
        await _insert_test_kp(
            test_db, kp_id="kp-mast", material_id=mat_id,
            title="待掌握", mastery_level=0.0,
        )

        engine = KnowledgeEngine(db=test_db, llm_client=FakeLLMClient())
        await engine.update_mastery("kp-mast", 0.85)

        row = await test_db.get_knowledge_point("kp-mast")
        assert row["mastery_level"] == 0.85

    async def test_update_mastery_clamped(self, test_db):
        """update_mastery should clamp values outside 0.0–1.0."""
        mat_id = await _create_test_material(test_db, material_id="mat-clamp")
        await _insert_test_kp(
            test_db, kp_id="kp-clamp", material_id=mat_id, title="钳位测试",
        )

        engine = KnowledgeEngine(db=test_db, llm_client=FakeLLMClient())

        # Above 1.0 → clamped to 1.0
        await engine.update_mastery("kp-clamp", 2.5)
        row = await test_db.get_knowledge_point("kp-clamp")
        assert row["mastery_level"] == 1.0

        # Below 0.0 → clamped to 0.0
        await engine.update_mastery("kp-clamp", -0.5)
        row = await test_db.get_knowledge_point("kp-clamp")
        assert row["mastery_level"] == 0.0
