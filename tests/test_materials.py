"""Tests for material CRUD operations — direct database level."""

from __future__ import annotations

from datetime import datetime, timezone

from tests.conftest import _create_test_material


class TestMaterials:
    """Material database operations."""

    async def test_create_material(self, test_db):
        """create_material should insert and return the material_id."""
        now = datetime.now(timezone.utc).isoformat()
        mat_id = await test_db.create_material({
            "material_id": "mat-test-1",
            "title": "大学物理",
            "content": "牛顿三大定律",
            "course_type": "physics",
            "status": "draft",
            "created_at": now,
            "updated_at": now,
        })
        assert mat_id == "mat-test-1"

        row = await test_db.get_material("mat-test-1")
        assert row is not None
        assert row["title"] == "大学物理"
        assert row["course_type"] == "physics"

    async def test_get_material_nonexistent(self, test_db):
        """get_material should return None for unknown IDs."""
        row = await test_db.get_material("nonexistent")
        assert row is None

    async def test_get_material_returns_all_fields(self, test_db):
        """get_material should return content, status, course_type."""
        await _create_test_material(
            test_db,
            material_id="mat-full",
            title="完整材料",
            content="详细内容",
            course_type="mathematics",
            status="ready",
        )

        row = await test_db.get_material("mat-full")
        assert row is not None
        assert row["content"] == "详细内容"
        assert row["course_type"] == "mathematics"
        assert row["status"] == "ready"
