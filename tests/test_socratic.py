"""Tests for the SocraticEngine — dialogue start, continue, and level transitions."""

from __future__ import annotations

import json

from typing import Any

import pytest

from super_tutor.engine.socratic_engine import SocraticEngine, _strip_markdown_fence
from super_tutor.models.socratic import (
    SocraticTurn,
    build_history_entry,
    format_history_for_prompt,
    L1_GUIDING,
    L2_HINTING,
    L3_NEAR_ANSWER,
    RESOLVED,
    SHOW_ANSWER,
)


# ======================================================================
# Fake LLM client for Socratic tests
# ======================================================================


class FakeLLMClient:
    """Test double that returns canned Socratic dialogue responses."""

    def __init__(self) -> None:
        """Initialize the fake LLM with an empty call log."""
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 4096,
        timeout: int = 120,
    ) -> str:
        """Return a canned Socratic dialogue response based on message content.

        Detects: start_dialogue → L1_GUIDING, "显示答案" → SHOW_ANSWER,
        understanding → RESOLVED, default → L2_HINTING escalation.
        """
        user_message = ""
        for m in messages:
            if m.get("role") == "user":
                user_message = m.get("content", "")
                break
        self.calls.append({"user_message": user_message[:200]})

        # Max turns → force SHOW_ANSWER
        if "已达到最大对话轮数" in user_message:
            return json.dumps({
                "level": "SHOW_ANSWER",
                "teacher_message": "完整解析...",
                "expected_concepts": ["牛顿第一定律"],
                "reasoning": "已达最大对话轮数",
                "resolved": True,
                "resolution_note": "超过最大对话轮数",
            }, ensure_ascii=False)

        is_start = "请从 L1_GUIDING 层级开始引导" in user_message
        if is_start:
            return json.dumps({
                "level": "L1_GUIDING",
                "teacher_message": "你能用自己的话复述一下这道题吗？",
                "expected_concepts": ["牛顿第一定律", "惯性"],
                "reasoning": "从最宽泛的问题开始",
                "resolved": False,
                "resolution_note": "",
            }, ensure_ascii=False)

        # continue_dialogue — check for "显示答案" shortcut
        if "显示答案" in user_message:
            return json.dumps({
                "level": "SHOW_ANSWER",
                "teacher_message": "完整解析...",
                "expected_concepts": ["牛顿第一定律"],
                "reasoning": "学生请求显示答案",
                "resolved": True,
                "resolution_note": "学生请求显示答案",
            }, ensure_ascii=False)

        # Resolve if understanding detected
        response_marker = "## 学生本轮回应"
        latest_response = user_message
        if response_marker in user_message:
            latest_response = user_message.split(response_marker)[-1]
        if any(w in latest_response for w in ["我理解了", "根据牛顿第一定律"]):
            return json.dumps({
                "level": "RESOLVED",
                "teacher_message": "非常好！你已经正确理解了。",
                "expected_concepts": ["牛顿第一定律"],
                "reasoning": "学生展示出正确理解",
                "resolved": True,
                "resolution_note": "学生自主理解了知识点",
            }, ensure_ascii=False)

        # Default escalation
        return json.dumps({
            "level": "L2_HINTING",
            "teacher_message": "换个角度想想...",
            "expected_concepts": ["摩擦力", "匀速运动"],
            "reasoning": "学生回答不够准确，升级到L2",
            "resolved": False,
            "resolution_note": "",
        }, ensure_ascii=False)


@pytest.fixture
def fake_llm() -> FakeLLMClient:
    """Create a FakeLLMClient for Socratic dialogue tests."""
    return FakeLLMClient()


# ============================================================================
# Unit tests — model
# ============================================================================


class TestSocraticTurnModel:
    """SocraticTurn Pydantic model validation and properties."""

    def test_create_turn(self):
        """Basic creation with required fields."""
        turn = SocraticTurn(
            kp_id="kp-001",
            wrong_question_id="wrong-001",
            teacher_message="你觉得这道题考察了什么概念？",
        )
        assert turn.level == L1_GUIDING
        assert turn.resolved is False
        assert turn.turn_id  # auto-generated
        assert len(turn.turn_id) > 0

    def test_is_terminal_resolved(self):
        """RESOLVED and SHOW_ANSWER levels should be terminal."""
        turn = SocraticTurn(
            kp_id="kp-001",
            wrong_question_id="wrong-001",
            teacher_message="很好！",
            level=RESOLVED,
            resolved=True,
        )
        assert turn.is_terminal is True

        turn2 = SocraticTurn(
            kp_id="kp-001",
            wrong_question_id="wrong-001",
            teacher_message="完整解析...",
            level=SHOW_ANSWER,
            resolved=True,
        )
        assert turn2.is_terminal is True

    def test_is_terminal_active(self):
        """L1/L2/L3 should NOT be terminal."""
        for level in (L1_GUIDING, L2_HINTING, L3_NEAR_ANSWER):
            turn = SocraticTurn(
                kp_id="kp-001",
                wrong_question_id="wrong-001",
                teacher_message="引导中...",
                level=level,
            )
            assert turn.is_terminal is False

    def test_build_history_entry(self):
        """build_history_entry should produce a dict with all key fields."""
        turn = SocraticTurn(
            kp_id="kp-001",
            wrong_question_id="wrong-001",
            teacher_message="你能复述一下题目吗？",
            level=L1_GUIDING,
        )
        entry = build_history_entry(turn, "题目说物体在光滑水平面上运动")
        assert entry["kp_id"] == "kp-001"
        assert entry["wrong_question_id"] == "wrong-001"
        assert entry["level"] == L1_GUIDING
        assert entry["teacher_message"] == "你能复述一下题目吗？"
        assert entry["user_response"] == "题目说物体在光滑水平面上运动"

    def test_format_history_empty(self):
        """Empty history should produce a placeholder message."""
        result = format_history_for_prompt([])
        assert "无历史" in result

    def test_format_history_with_entries(self):
        """History with entries should be properly formatted."""
        turn1 = SocraticTurn(
            kp_id="kp-001",
            wrong_question_id="wrong-001",
            teacher_message="Q1?",
            level=L1_GUIDING,
        )
        turn2 = SocraticTurn(
            kp_id="kp-001",
            wrong_question_id="wrong-001",
            teacher_message="Q2?",
            level=L2_HINTING,
        )
        history = [
            build_history_entry(turn1, "A1"),
            build_history_entry(turn2, "A2"),
        ]
        result = format_history_for_prompt(history)
        assert "第 1 轮" in result
        assert "第 2 轮" in result
        assert "Q1?" in result
        assert "A1" in result
        assert "Q2?" in result
        assert "A2" in result
        assert "笼统引导" in result
        assert "具体提示" in result


# ============================================================================
# Unit tests — helpers
# ============================================================================


class TestStripMarkdownFence:
    """Markdown fence stripping utility."""

    def test_strips_fence(self):
        raw = '```json\n{"key": "value"}\n```'
        result = _strip_markdown_fence(raw)
        assert result == '{"key": "value"}'

    def test_strips_fence_no_lang(self):
        raw = '```\n{"key": "value"}\n```'
        result = _strip_markdown_fence(raw)
        assert result == '{"key": "value"}'

    def test_no_fence_unchanged(self):
        raw = '{"key": "value"}'
        result = _strip_markdown_fence(raw)
        assert result == '{"key": "value"}'

    def test_leading_trailing_whitespace(self):
        raw = '  \n  {"key": "value"}  \n  '
        result = _strip_markdown_fence(raw)
        assert result == '{"key": "value"}'


class TestShowAnswerDetection:
    """Detection of explicit answer requests."""

    @pytest.mark.parametrize("text", [
        "显示答案",
        "告诉我答案吧",
        "直接说答案",
        "公布答案",
        "看答案",
        "给答案",
        "答案是什么",
        "正确答案是什么",
        "我不会",
        "完全不会啊",
        "太难了",
        "想不出来",
        "show answer",
        "tell me the answer",
        "give me the answer",
    ])
    def test_detects_show_answer(self, text):
        """Common trigger phrases should be detected."""
        assert SocraticEngine._is_show_answer_request(text) is True

    @pytest.mark.parametrize("text", [
        "牛顿第一定律是惯性定律",
        "物体在不受力时保持匀速运动",
        "我觉得应该是B选项",
        "不太确定，可能是摩擦力的问题",
        "让我再想想",
    ])
    def test_does_not_detect_normal_responses(self, text):
        """Normal learning responses should NOT trigger show-answer."""
        assert SocraticEngine._is_show_answer_request(text) is False


# ============================================================================
# Integration tests — require DB + Fake LLM
# ============================================================================


async def _insert_kp(test_db, kp_id="kp-001", title="牛顿第一定律",
                      content="物体在不受外力作用时，保持静止或匀速直线运动状态。",
                      difficulty="medium"):
    """Helper: insert a knowledge point into the test DB."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    await test_db._conn.execute(
        """INSERT INTO knowledge_points
           (kp_id, material_id, title, summary, content, keywords,
            difficulty, prerequisite_ids, successor_ids, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (kp_id, "mat-1", title, "summary", content, "[]",
         difficulty, "[]", "[]", now, now),
    )
    await test_db._conn.commit()


async def _insert_wrong(test_db, wrong_id="wrong-001", question_id="q-001",
                        kp_id="kp-001", wrong_answer="A",
                        correct_answer="B"):
    """Helper: insert a wrong question record and its source question."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    # Source question
    await test_db._conn.execute(
        """INSERT INTO questions
           (question_id, type, difficulty, stem, options, correct_answer,
            explanation, kp_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            question_id, "multiple_choice", "medium",
            "一个物体在光滑水平面上以恒定速度运动，这说明什么？",
            json.dumps([
                {"key": "A", "text": "物体受到平衡力"},
                {"key": "B", "text": "物体不受任何外力"},
                {"key": "C", "text": "物体受到恒定的外力"},
                {"key": "D", "text": "无法判断"},
            ]),
            correct_answer,
            "根据牛顿第一定律，光滑水平面意味着没有摩擦力。",
            kp_id, now,
        ),
    )
    # Wrong question record
    await test_db._conn.execute(
        """INSERT INTO wrong_questions
           (wrong_id, student_id, question_id, kp_id, wrong_answer,
            correct_answer, attempt_count, resolution_status,
            note, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            wrong_id, "default", question_id, kp_id, wrong_answer,
            correct_answer, 1, "unresolved", "", now, now,
        ),
    )
    await test_db._conn.commit()


class TestStartDialogue:
    """start_dialogue integration tests with FakeLLMClient."""

    @pytest.mark.asyncio
    async def test_start_returns_l1_guiding(self, test_db, fake_llm):
        """start_dialogue should return an L1_GUIDING turn."""
        await _insert_kp(test_db)
        await _insert_wrong(test_db)

        engine = SocraticEngine(test_db, fake_llm)
        turn = await engine.start_dialogue("kp-001", "wrong-001")

        assert isinstance(turn, SocraticTurn)
        assert turn.level == L1_GUIDING
        assert turn.resolved is False
        assert turn.kp_id == "kp-001"
        assert turn.wrong_question_id == "wrong-001"
        assert len(turn.teacher_message) > 0
        assert len(turn.expected_concepts) > 0

    @pytest.mark.asyncio
    async def test_start_missing_kp_raises(self, test_db, fake_llm):
        """Calling start_dialogue with a non-existent KP should raise."""
        engine = SocraticEngine(test_db, fake_llm)
        with pytest.raises(Exception):  # MaterialError
            await engine.start_dialogue("kp-nonexistent", "wrong-001")

    @pytest.mark.asyncio
    async def test_start_missing_wrong_raises(self, test_db, fake_llm):
        """Calling start_dialogue with a non-existent wrong question should raise."""
        await _insert_kp(test_db)
        engine = SocraticEngine(test_db, fake_llm)
        with pytest.raises(Exception):  # MaterialError
            await engine.start_dialogue("kp-001", "wrong-nonexistent")


class TestContinueDialogue:
    """continue_dialogue integration tests with FakeLLMClient."""

    @pytest.mark.asyncio
    async def test_continue_empty_history_raises(self, test_db, fake_llm):
        """Empty history should raise ValueError."""
        engine = SocraticEngine(test_db, fake_llm)
        with pytest.raises(ValueError, match="不能为空"):
            await engine.continue_dialogue([], "some response")

    @pytest.mark.asyncio
    async def test_continue_show_answer_shortcut(self, test_db, fake_llm):
        """Typing '显示答案' should short-circuit to SHOW_ANSWER without LLM call."""
        await _insert_kp(test_db)
        await _insert_wrong(test_db)

        engine = SocraticEngine(test_db, fake_llm)
        # First, start a dialogue
        turn = await engine.start_dialogue("kp-001", "wrong-001")
        history = [build_history_entry(turn, "不太确定")]

        # Now say "显示答案" — should short-circuit
        calls_before = len(fake_llm.calls)
        result = await engine.continue_dialogue(history, "我不会，显示答案吧")
        calls_after = len(fake_llm.calls)

        assert result.level == SHOW_ANSWER
        assert result.resolved is False  # 软确认阶段
        # Should not have called LLM (short-circuited)
        assert calls_after == calls_before

    @pytest.mark.asyncio
    async def test_continue_normal_escalation(self, test_db, fake_llm):
        """A vague student response should escalate from L1 to L2."""
        await _insert_kp(test_db)
        await _insert_wrong(test_db)

        engine = SocraticEngine(test_db, fake_llm)
        turn = await engine.start_dialogue("kp-001", "wrong-001")
        history = [build_history_entry(turn, "我觉得可能是和力有关？")]

        result = await engine.continue_dialogue(history, "我觉得可能是和力有关？")

        assert isinstance(result, SocraticTurn)
        assert result.level in (L2_HINTING, L3_NEAR_ANSWER, RESOLVED, SHOW_ANSWER)
        # FakeLLM returns L2_HINTING for non-matching responses
        assert result.level == L2_HINTING
        assert result.resolved is False

    @pytest.mark.asyncio
    async def test_continue_resolve(self, test_db, fake_llm):
        """A correct understanding should resolve the dialogue."""
        await _insert_kp(test_db)
        await _insert_wrong(test_db)

        engine = SocraticEngine(test_db, fake_llm)
        turn = await engine.start_dialogue("kp-001", "wrong-001")
        history = [build_history_entry(turn, "不太确定")]

        result = await engine.continue_dialogue(
            history, "我理解了，根据牛顿第一定律，物体不受外力时保持匀速运动"
        )

        assert result.level == RESOLVED
        assert result.resolved is True

    @pytest.mark.asyncio
    async def test_continue_max_turns(self, test_db, fake_llm):
        """After max turns, should force SHOW_ANSWER."""
        await _insert_kp(test_db)
        await _insert_wrong(test_db)

        engine = SocraticEngine(test_db, fake_llm)
        turn = await engine.start_dialogue("kp-001", "wrong-001")

        # Build 6 turns of history (max)
        history = [build_history_entry(turn, "不知道")]
        for i in range(5):
            history.append(build_history_entry(
                SocraticTurn(
                    kp_id="kp-001",
                    wrong_question_id="wrong-001",
                    teacher_message=f"Hint {i}",
                    level=L2_HINTING,
                ),
                "还是不太明白",
            ))

        # This should trigger max-turns force
        result = await engine.continue_dialogue(history, "还是不知道")

        # Should have called LLM with SHOW_ANSWER instruction
        assert result.level == SHOW_ANSWER
        assert result.resolved is True
