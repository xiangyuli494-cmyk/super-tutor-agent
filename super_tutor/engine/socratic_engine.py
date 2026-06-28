"""Socratic Engine — 苏格拉底式引导追问引擎。

基于错题和知识点内容，通过层层递进的引导性问题帮助学生
自主发现正确答案，而非直接告知。

状态机（会话级，仅存 ``st.session_state``，不持久化到 DB）::

    L1_GUIDING → L2_HINTING → L3_NEAR_ANSWER → RESOLVED
         ↓            ↓              ↓
         └────────────┴──────────────┴──→ SHOW_ANSWER

Usage::

    engine = SocraticEngine(db, llm_client)
    turn = await engine.start_dialogue("kp-001", "wrong-001")
    # ... user responds ...
    history = [build_history_entry(turn, user_response)]
    next_turn = await engine.continue_dialogue(history, user_response)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from super_tutor.core.database import Database
from super_tutor.core.exceptions import LLMError, MaterialError
from super_tutor.core.llm_client import LLMClient
from super_tutor.models.socratic import (
    SocraticTurn,
    build_history_entry,
    format_history_for_prompt,
    L1_GUIDING,
    SHOW_ANSWER,
    RESOLVED,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default prompt path
# ---------------------------------------------------------------------------
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_DEFAULT_SOCRATIC_PROMPT = _PROMPTS_DIR / "socratic.md"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_MAX_DIALOGUE_TURNS = 6  # 超过此轮数自动升级到 SHOW_ANSWER


class SocraticEngine:
    """苏格拉底式引导追问引擎。

    封装了错题上下文获取、LLM 引导生成和层级状态管理。
    对话状态仅保存在 ``st.session_state`` 中，不写入数据库。

    Usage::

        engine = SocraticEngine(db, llm_client)
        turn = await engine.start_dialogue("kp-001", "wrong-001")
        # 展示 turn.teacher_message 给学生...
        history = [build_history_entry(turn, "学生的回答")]
        next_turn = await engine.continue_dialogue(history, "学生的回答")
    """

    def __init__(
        self,
        db: Database,
        llm_client: LLMClient,
        prompt_path: Optional[str] = None,
    ) -> None:
        """Initialise the Socratic engine.

        Args:
            db: An initialised ``Database`` instance.
            llm_client: An ``LLMClient`` instance for LLM calls.
            prompt_path: Optional custom prompt path (defaults to
                ``prompts/socratic.md``).
        """
        self._db = db
        self._llm = llm_client
        self._prompt_path = prompt_path or str(_DEFAULT_SOCRATIC_PROMPT)

    # ==================================================================
    # start_dialogue — 开始新对话
    # ==================================================================

    async def start_dialogue(
        self,
        kp_id: str,
        wrong_question_id: str,
    ) -> SocraticTurn:
        """Start a new Socratic dialogue for a wrong question.

        Fetches the knowledge point and wrong-question record from the
        database, builds the context prompt, and calls the LLM to
        generate an **L1_GUIDING** opening question.

        Args:
            kp_id: The knowledge point ID.
            wrong_question_id: The wrong-question record ID.

        Returns:
            A ``SocraticTurn`` with ``level=L1_GUIDING``.

        Raises:
            MaterialError: If the KP or wrong question is not found,
                or if the LLM returns an invalid response.
        """
        # -- 1. Fetch context data --------------------------------------------
        kp_data, wrong_data = await self._fetch_context(kp_id, wrong_question_id)

        # -- 2. Build user prompt ---------------------------------------------
        user_prompt = self._build_start_prompt(kp_data, wrong_data)

        # -- 3. Call LLM ------------------------------------------------------
        raw_json = await self._call_llm(user_prompt)

        # -- 4. Parse & return ------------------------------------------------
        turn = self._parse_turn(raw_json, kp_id, wrong_question_id)

        logger.info(
            "Socratic dialogue started: kp=%s wrong=%s turn=%s level=%s",
            kp_id,
            wrong_question_id,
            turn.turn_id,
            turn.level,
        )
        return turn

    # ==================================================================
    # continue_dialogue — 继续对话
    # ==================================================================

    async def continue_dialogue(
        self,
        history: list[dict[str, Any]],
        user_response: str,
    ) -> SocraticTurn:
        """Continue an ongoing Socratic dialogue.

        Evaluates the student's response against the dialogue history
        and decides whether to escalate (L1→L2→L3), de-escalate,
        resolve, or show the answer.

        Args:
            history: Previous dialogue turns (each a dict from
                ``build_history_entry()``).  Must contain at least one
                entry with ``kp_id`` and ``wrong_question_id``.
            user_response: The student's latest response text.

        Returns:
            A ``SocraticTurn`` with the next teacher message and
            updated level.

        Raises:
            ValueError: If *history* is empty.
            MaterialError: If the context cannot be fetched or the LLM
                returns an invalid response.
        """
        if not history:
            raise ValueError("history 不能为空")

        # -- 1. Check for explicit "show answer" request ---------------------
        if self._is_show_answer_request(user_response):
            return self._build_show_answer_turn(history)

        # -- 2. Check for max turns exceeded ---------------------------------
        if len(history) >= _MAX_DIALOGUE_TURNS:
            logger.info(
                "Max dialogue turns (%d) reached — escalating to SHOW_ANSWER",
                _MAX_DIALOGUE_TURNS,
            )
            return await self._force_show_answer(history, user_response)

        # -- 3. Extract context from first history entry ----------------------
        kp_id = history[0].get("kp_id", "")
        wrong_question_id = history[0].get("wrong_question_id", "")

        if not kp_id or not wrong_question_id:
            raise ValueError("history[0] 缺少 kp_id 或 wrong_question_id")

        # -- 4. Fetch context data -------------------------------------------
        kp_data, wrong_data = await self._fetch_context(kp_id, wrong_question_id)

        # -- 5. Build user prompt with history --------------------------------
        user_prompt = self._build_continue_prompt(
            kp_data, wrong_data, history, user_response
        )

        # -- 6. Call LLM ------------------------------------------------------
        raw_json = await self._call_llm(user_prompt)

        # -- 7. Parse & return ------------------------------------------------
        turn = self._parse_turn(raw_json, kp_id, wrong_question_id)

        logger.info(
            "Socratic dialogue continued: turn=%s level=%s resolved=%s",
            turn.turn_id,
            turn.level,
            turn.resolved,
        )
        return turn

    # ==================================================================
    # Internal: context fetching
    # ==================================================================

    async def _fetch_context(
        self, kp_id: str, wrong_question_id: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Fetch KP and wrong-question data, raising on missing records."""
        # KP
        kp_data = await self._db.get_knowledge_point(kp_id)
        if kp_data is None:
            raise MaterialError(f"知识点不存在: {kp_id}")

        # Wrong question — may also need the original question for stem
        wrong_data = await self._db.get_wrong_question(wrong_question_id)
        if wrong_data is None:
            raise MaterialError(f"错题记录不存在: {wrong_question_id}")

        # Enrich with question stem if available
        question_id = wrong_data.get("question_id", "")
        if question_id:
            q_row = await self._db.get_question(question_id)
            if q_row:
                wrong_data["_question_stem"] = q_row.get("stem", "")
                wrong_data["_question_explanation"] = q_row.get("explanation", "")
                wrong_data["_question_type"] = q_row.get("type", "")

        return kp_data, wrong_data

    # ==================================================================
    # Internal: prompt building
    # ==================================================================

    @staticmethod
    def _build_start_prompt(
        kp_data: dict[str, Any],
        wrong_data: dict[str, Any],
    ) -> str:
        """Build the user prompt for ``start_dialogue``."""
        lines = [
            "## 知识点",
            f"- 标题: {kp_data.get('title', '')}",
            f"- 难度: {kp_data.get('difficulty', 'medium')}",
            f"- 内容: {kp_data.get('content', '')}",
            "",
            "## 错题",
            f"- 题干: {wrong_data.get('_question_stem', wrong_data.get('note', ''))}",
            f"- 学生答案: {wrong_data.get('wrong_answer', '')}",
            f"- 正确答案: {wrong_data.get('correct_answer', '')}",
        ]

        explanation = wrong_data.get(
            "_question_explanation", ""
        ) or wrong_data.get("note", "")
        if explanation:
            lines.append(f"- 解析: {explanation}")

        lines.append("")
        lines.append("请从 L1_GUIDING 层级开始引导。")
        return "\n".join(lines)

    @staticmethod
    def _build_continue_prompt(
        kp_data: dict[str, Any],
        wrong_data: dict[str, Any],
        history: list[dict[str, Any]],
        user_response: str,
    ) -> str:
        """Build the user prompt for ``continue_dialogue``."""
        lines = [
            "## 知识点",
            f"- 标题: {kp_data.get('title', '')}",
            f"- 难度: {kp_data.get('difficulty', 'medium')}",
            f"- 内容: {kp_data.get('content', '')}",
            "",
            "## 错题",
            f"- 题干: {wrong_data.get('_question_stem', wrong_data.get('note', ''))}",
            f"- 学生答案: {wrong_data.get('wrong_answer', '')}",
            f"- 正确答案: {wrong_data.get('correct_answer', '')}",
        ]

        explanation = wrong_data.get(
            "_question_explanation", ""
        ) or wrong_data.get("note", "")
        if explanation:
            lines.append(f"- 解析: {explanation}")

        lines.append("")
        lines.append("## 对话历史")
        lines.append(format_history_for_prompt(history))
        lines.append("")
        lines.append("## 学生本轮回应")
        lines.append(user_response)
        lines.append("")
        lines.append("请根据学生的回应判断下一层级并生成引导内容。")
        return "\n".join(lines)

    # ==================================================================
    # Internal: LLM call
    # ==================================================================

    async def _call_llm(
        self, user_prompt: str
    ) -> str:
        """Call the LLM with the socratic system prompt and user context.

        Args:
            user_prompt: The formatted user prompt.

        Returns:
            Raw LLM response text (JSON string).

        Raises:
            MaterialError: If the prompt file cannot be loaded or the
                LLM call fails.
        """
        # Load system prompt
        try:
            system_prompt = Path(self._prompt_path).read_text(encoding="utf-8")
        except OSError as exc:
            raise MaterialError(
                f"无法加载苏格拉底提示词: {self._prompt_path} ({exc})"
            ) from exc

        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        try:
            raw = await self._llm.chat(
                messages=messages,
                temperature=0.7,
                max_tokens=2048,
                timeout=120,
            )
        except LLMError as exc:
            raise MaterialError(f"LLM 苏格拉底追问失败: {exc}") from exc

        return _strip_markdown_fence(raw)

    # ==================================================================
    # Internal: parse & helpers
    # ==================================================================

    def _parse_turn(
        self,
        raw_json: str,
        kp_id: str,
        wrong_question_id: str,
    ) -> SocraticTurn:
        """Parse LLM JSON response into a ``SocraticTurn``.

        Raises:
            MaterialError: If the JSON is invalid or missing required fields.
        """
        try:
            data = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            logger.error("Socratic LLM 返回无效 JSON: %s", raw_json[:500])
            raise MaterialError(
                f"苏格拉底追问结果不是有效 JSON: {exc}"
            ) from exc

        level = data.get("level", L1_GUIDING)
        teacher_message = data.get("teacher_message", "")

        if not teacher_message:
            raise MaterialError("LLM 未返回 teacher_message")

        return SocraticTurn(
            turn_id=str(uuid4()),
            kp_id=kp_id,
            wrong_question_id=wrong_question_id,
            level=level,
            teacher_message=teacher_message,
            expected_concepts=data.get("expected_concepts", []),
            reasoning=data.get("reasoning", ""),
            resolved=data.get("resolved", False)
                or level in (RESOLVED, SHOW_ANSWER),
            resolution_note=data.get("resolution_note", ""),
        )

    @staticmethod
    def _is_show_answer_request(user_response: str) -> bool:
        """Detect whether the student is explicitly asking for the answer."""
        triggers = [
            "显示答案", "告诉我答案", "直接说答案", "公布答案",
            "看答案", "给答案", "答案是什么", "正确答案是什么",
            "我不会", "完全不会", "太难了", "想不出来",
            "show answer", "tell me the answer", "give me the answer",
        ]
        lowered = user_response.strip().lower()
        return any(t.lower() in lowered for t in triggers)

    def _build_show_answer_turn(
        self,
        history: list[dict[str, Any]],
    ) -> SocraticTurn:
        """Build a SHOW_ANSWER turn directly (no LLM needed for detection).

        This is a fast path — the LLM is still called once to produce
        a quality answer explanation.  But we set the level to
        SHOW_ANSWER so the LLM knows it should provide the full answer.
        """
        first = history[0]
        kp_id = first.get("kp_id", "")
        wrong_question_id = first.get("wrong_question_id", "")

        logger.info(
            "User requested answer explicitly — switching to SHOW_ANSWER"
        )
        # We return a placeholder and let the caller re-enter via the
        # normal continue_dialogue path, but we can just short-circuit
        # here.
        return SocraticTurn(
            turn_id=str(uuid4()),
            kp_id=kp_id,
            wrong_question_id=wrong_question_id,
            level=SHOW_ANSWER,
            teacher_message=(
                '好的，让我来为你详细解析这道题。\n\n'
                '不过在此之前 — 你能先告诉我你目前对这个知识点的理解吗？'
                '这样我可以更有针对性地解释。\n\n'
                '（如果你希望直接看答案，请再次输入 **显示答案**。）'
            ),
            expected_concepts=[],
            reasoning="学生请求显示答案，先进行一轮软确认",
            resolved=False,
            resolution_note="",
        )

    async def _force_show_answer(
        self,
        history: list[dict[str, Any]],
        user_response: str,
    ) -> SocraticTurn:
        """Force a SHOW_ANSWER when max turns exceeded.

        Calls the LLM with an explicit instruction to provide the full
        answer and explanation.
        """
        first = history[0]
        kp_id = first.get("kp_id", "")
        wrong_question_id = first.get("wrong_question_id", "")
        kp_data, wrong_data = await self._fetch_context(kp_id, wrong_question_id)

        user_prompt = (
            self._build_continue_prompt(kp_data, wrong_data, history, user_response)
            + "\n\n**注意：已达到最大对话轮数，请直接以 SHOW_ANSWER 层级给出完整解析。**"
        )

        raw_json = await self._call_llm(user_prompt)
        return self._parse_turn(raw_json, kp_id, wrong_question_id)


# ===================================================================
# Module-level helpers
# ===================================================================


def _strip_markdown_fence(raw: str) -> str:
    """Remove Markdown code fences from an LLM response if present."""
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines)
    return raw
