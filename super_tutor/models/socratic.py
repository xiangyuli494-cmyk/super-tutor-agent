"""Super Tutor — 苏格拉底式引导追问模型。

定义 SocraticEngine 的单轮对话输出结构，
包含引导层级、教师消息和解决状态。
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Socratic level constants
# ---------------------------------------------------------------------------

L1_GUIDING = "L1_GUIDING"            # 笼统引导 — 最宽泛的开放性问题
L2_HINTING = "L2_HINTING"            # 具体提示 — 方向性暗示
L3_NEAR_ANSWER = "L3_NEAR_ANSWER"    # 接近答案 — 几乎给出推理步骤
RESOLVED = "RESOLVED"                # 已解决 — 学生展示出正确理解
SHOW_ANSWER = "SHOW_ANSWER"          # 显示答案 — 学生要求/需要直接看解析

VALID_LEVELS: set[str] = {
    L1_GUIDING,
    L2_HINTING,
    L3_NEAR_ANSWER,
    RESOLVED,
    SHOW_ANSWER,
}


# ============================================================================
# SocraticTurn — 单轮苏格拉底对话
# ============================================================================


class SocraticTurn(BaseModel):
    """苏格拉底式引导追问中的单轮教师回复。

    由 ``SocraticEngine.start_dialogue()`` 或
    ``SocraticEngine.continue_dialogue()`` 生成，
    不持久化到数据库，仅保存在 ``st.session_state`` 中。
    """

    turn_id: str = Field(
        default_factory=lambda: str(uuid4()),
        description="本轮唯一标识",
    )
    kp_id: str = Field(
        ...,
        description="关联的知识点 ID",
    )
    wrong_question_id: str = Field(
        ...,
        description="关联的错题 ID",
    )
    level: str = Field(
        default=L1_GUIDING,
        description=f"当前引导层级：{' / '.join(sorted(VALID_LEVELS))}",
    )
    teacher_message: str = Field(
        ...,
        min_length=1,
        description="教师对学生的引导问题/提示/解析（Markdown 格式）",
    )
    expected_concepts: list[str] = Field(
        default_factory=list,
        description="学生应在此轮中想到的关键概念列表",
    )
    reasoning: str = Field(
        default="",
        description="内部推理（选择该层级和内容的简短理由，不展示给学生）",
    )
    resolved: bool = Field(
        default=False,
        description="True 表示对话已结束（RESOLVED 或 SHOW_ANSWER）",
    )
    resolution_note: str = Field(
        default="",
        description="当 resolved=True 时的解决方式说明",
    )

    @property
    def is_terminal(self) -> bool:
        """本轮是否为对话的最后一轮。"""
        return self.resolved or self.level in (RESOLVED, SHOW_ANSWER)


# ============================================================================
# Helpers
# ============================================================================


def build_history_entry(turn: SocraticTurn, user_response: str) -> dict[str, Any]:
    """将一轮对话转为历史条目，用于下次 ``continue_dialogue`` 调用。

    Args:
        turn: 本轮教师回复。
        user_response: 学生对本轮教师消息的回应。

    Returns:
        一个包含本轮完整信息的 dict。
    """
    return {
        "turn_id": turn.turn_id,
        "kp_id": turn.kp_id,
        "wrong_question_id": turn.wrong_question_id,
        "level": turn.level,
        "teacher_message": turn.teacher_message,
        "user_response": user_response,
    }


def format_history_for_prompt(history: list[dict[str, Any]]) -> str:
    """将对话历史格式化为可嵌入 LLM prompt 的文本。

    Args:
        history: ``build_history_entry`` 返回的条目列表。

    Returns:
        格式化的对话历史文本。
    """
    if not history:
        return "（无历史对话）"

    lines: list[str] = []
    for i, entry in enumerate(history):
        level_label = _level_label(entry.get("level", ""))
        lines.append(f"## 第 {i + 1} 轮 ({level_label})")
        lines.append(f"**教师**: {entry.get('teacher_message', '')}")
        lines.append(f"**学生**: {entry.get('user_response', '')}")
        lines.append("")
    return "\n".join(lines)


def _level_label(level: str) -> str:
    """Return a human-readable label for a socratic level."""
    labels: dict[str, str] = {
        L1_GUIDING: "笼统引导",
        L2_HINTING: "具体提示",
        L3_NEAR_ANSWER: "接近答案",
        RESOLVED: "已解决",
        SHOW_ANSWER: "显示答案",
    }
    return labels.get(level, level)
