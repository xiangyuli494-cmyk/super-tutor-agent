"""Super Tutor — Streamlit 前端入口（单页应用）。

【功能说明】
基于 Streamlit 构建的智能教学系统前端，提供以下完整用户流程：

1. 📥 导入教材 — 上传 PDF（PyPDF2 提取）或粘贴文本
2. 🤖 AI 解析 — 调用 KnowledgeEngine 提取结构化知识点（含难度、前置/后继关系）
3. 📋 知识点展示 — 以表格形式展示，支持预览和确认
4. 📝 练习答题 — QuizEngine 出题 + 程序/LLM 混合批改
5. 📖 错题本 — 自动收录错题，支持按知识点筛选 + 苏格拉底追问
6. 🔬 诊断评估 — AssessmentEngine 生成诊断性题目 + 3 条前置规则校准
7. 📅 学习计划 — PlanEngine 拓扑排序 + 优先级公式 + 日排期

页面布局（layout="wide"）：
┌────────────────────────────────────────────┐
│ 🎓 Super Tutor — 智能教学系统               │
│ ┌─ 📄 上传 PDF ──┬── ✏️ 粘贴文本 ────────┐ │
│ │                 │                        │ │
│ ├─ 课程类型 ─────┴── 教材标题 ────────────┤ │
│ │                 [🔍 开始解析]            │ │
│ └─────────────────────────────────────────┘ │
│ ┌─ 📋 知识点列表 ─────────────────────────┐ │
│ │ [确认，开始诊断评估 →] [🔄 重新上传]      │ │
│ └─────────────────────────────────────────┘ │
│ ┌─ Tab: 📝 练习答题 ─────────────────────┐ │
│ │   Tab: 📖 错题本                       │ │
│ │   Tab: 🔬 诊断评估                      │ │
│ │   Tab: 📅 学习计划                      │ │
│ └─────────────────────────────────────────┘ │
└────────────────────────────────────────────┘

【session_state 管理（20 个 key）】
使用 st.session_state 管理全部应用状态（无外部状态管理库）：
- _S_DB / _S_LLM / _S_ENGINE      — 核心服务单例（惰性初始化）
- _S_KPS / _S_MATERIAL_ID          — 解析结果
- _S_QUIZ_ENGINE / _S_QUESTIONS    — 练习答题状态
- _S_ASSESSMENT_*                  — 诊断评估状态（Engine / Questions / Report）
- _S_PLAN / _S_PLAN_ACTIVE_KP      — 学习计划状态
- _S_SOCRATIC_*                    — 苏格拉底追问状态（Engine / Active / History）
- _S_PARSE_ERROR                   — 错误展示

【异步处理策略】
Streamlit 不支持原生 async，使用 _run_async() 包装：
- 优先 asyncio.run()（Python 3.7+ 标准方式）
- 若已有 event loop → 尝试 nest_asyncio.apply() + run_until_complete()
- 仅对非 UI 的 I/O 密集操作使用异步（LLM 调用、数据库写入）

【耦合关系】
- 依赖 super_tutor/ 下所有包：config、core、engine、models
- 导入 5 个 Engine：KnowledgeEngine、QuizEngine、AssessmentEngine、
  PlanEngine、SocraticEngine
- 导入 5 个 Model：KnowledgePoint、Question、QuizAttempt、
  AssessmentReport、StudyPlan、SocraticTurn
- 导入 2 个枚举：DifficultyLevel、QuestionType
- 导入 Database、LLMClient（直接实例化）
- 被 streamlit run 启动（仅此一种运行方式）
- 不定义任何业务逻辑 — 所有逻辑委托给 engine/ 层
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import uuid4

import streamlit as st

# ---------------------------------------------------------------------------
# Ensure the project root is on sys.path (for `streamlit run app.py`)
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from super_tutor.config import TutorConfig
from super_tutor.core.database import Database
from super_tutor.core.llm_client import LLMClient
from super_tutor.engine.assessment_engine import AssessmentEngine
from super_tutor.engine.knowledge_engine import KnowledgeEngine
from super_tutor.engine.plan_engine import PlanEngine
from super_tutor.engine.quiz_engine import QuizEngine
from super_tutor.engine.socratic_engine import SocraticEngine
from super_tutor.models.assessment import AssessmentReport
from super_tutor.models.enums import DifficultyLevel, QuestionType
from super_tutor.models.plan import StudyPlan
from super_tutor.models.quiz import Question, QuizAttempt
from super_tutor.models.socratic import build_history_entry

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("super_tutor.app")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
COURSE_TYPES = [
    "physics",
    "mathematics",
    "chemistry",
    "biology",
    "computer_science",
    "history",
    "literature",
    "english",
    "economics",
    "other",
]

# ---------------------------------------------------------------------------
# Session state keys
# ---------------------------------------------------------------------------
_S_DB = "tutor_db"
_S_LLM = "tutor_llm"
_S_ENGINE = "tutor_engine"
_S_KPS = "tutor_knowledge_points"
_S_MATERIAL_ID = "tutor_material_id"
_S_PARSE_ERROR = "tutor_parse_error"
_S_QUIZ_ENGINE = "tutor_quiz_engine"
_S_QUIZ_MODE = "tutor_quiz_mode"
_S_QUESTIONS = "tutor_questions"
_S_ATTEMPTS = "tutor_attempts"
_S_QUIZ_SUBMITTED = "tutor_quiz_submitted"
_S_ASSESSMENT_ENGINE = "tutor_assessment_engine"
_S_ASSESSMENT_QUESTIONS = "tutor_assessment_questions"
_S_ASSESSMENT_REPORT = "tutor_assessment_report"
_S_ASSESSMENT_SUBMITTED = "tutor_assessment_submitted"
_S_PLAN = "tutor_plan"
_S_PLAN_ACTIVE_KP = "tutor_plan_active_kp"
_S_SOCRATIC_ENGINE = "tutor_socratic_engine"
_S_SOCRATIC_ACTIVE = "tutor_socratic_active"
_S_SOCRATIC_HISTORY = "tutor_socratic_history"
_S_SOCRATIC_TURN = "tutor_socratic_turn"


# ===================================================================
# Service initialisation
# ===================================================================


def _init_services() -> tuple[Database, LLMClient | None, KnowledgeEngine | None]:
    """Initialise config, database, LLM client, and KnowledgeEngine.

    Returns (db, llm_client, engine).  llm_client and engine may be None
    if the API key is not configured.
    """
    config = TutorConfig.load()

    # -- Database -----------------------------------------------------------
    db_path = os.getenv("TUTOR_DB_PATH") or str(
        Path.home() / ".super-tutor" / "super_tutor.db"
    )
    db_path = str(Path(db_path).expanduser().resolve())
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    db = Database(db_path=db_path)

    # -- LLM Client ----------------------------------------------------------
    llm_client: LLMClient | None = None
    engine: KnowledgeEngine | None = None

    if not config.api_key:
        logger.warning("API key not configured — LLM features disabled.")
        return db, None, None

    # Sync config to env vars so LLMClient (which reads env vars) can find them
    os.environ.setdefault("TUTOR_API_KEY", config.api_key)
    os.environ.setdefault("TUTOR_API_BASE_URL", config.api_base_url)
    os.environ.setdefault("TUTOR_MODEL", config.model)

    try:
        llm_client = LLMClient()
        engine = KnowledgeEngine(db=db, llm_client=llm_client)
        logger.info("KnowledgeEngine initialised.")
    except Exception as exc:
        logger.warning("Failed to init LLM client: %s", exc)
        return db, None, None

    return db, llm_client, engine


# ===================================================================
# PDF text extraction
# ===================================================================


def _extract_pdf_text(file_bytes: bytes) -> str:
    """Extract text from PDF bytes using PyPDF2.

    Returns the concatenated text of all pages.

    Raises:
        ImportError: If PyPDF2 is not installed.
        ValueError: If the PDF cannot be read.
    """
    try:
        from PyPDF2 import PdfReader
    except ImportError:
        raise ImportError(
            "PDF 解析需要 PyPDF2 库。请运行: pip install PyPDF2"
        )

    reader = PdfReader(BytesIO(file_bytes))
    pages: list[str] = []
    for i, page in enumerate(reader.pages):
        text = page.extract_text()
        if text:
            pages.append(text.strip())
        else:
            logger.debug("Page %d returned no text.", i + 1)

    if not pages:
        raise ValueError(
            "未能从 PDF 中提取任何文本。PDF 可能为扫描件（图片格式），"
            "请尝试直接粘贴文本。"
        )

    full_text = "\n\n".join(pages)
    logger.info(
        "Extracted %d chars from %d pages.", len(full_text), len(pages)
    )
    return full_text


# ===================================================================
# Async runner
# ===================================================================


def _run_async(coro):
    """Run an async coroutine in a Streamlit-friendly way.

    Uses ``asyncio.run()`` with a nested-event-loop workaround for
    environments where an event loop is already running.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    # Already inside an event loop — use nest_asyncio if available
    try:
        import nest_asyncio

        nest_asyncio.apply()
        return loop.run_until_complete(coro)
    except ImportError:
        raise RuntimeError(
            "当前环境已有运行中的事件循环，请安装 nest_asyncio: pip install nest_asyncio"
        )


# ===================================================================
# UI Components
# ===================================================================


def _render_api_key_warning() -> None:
    """Show a warning banner when no API key is configured."""
    st.warning(
        "⚠️ **未配置 API Key** — 知识点解析功能不可用。\n\n"
        "请在 `~/.super-tutor/settings.json` 中设置 `api_key`，"
        "或设置环境变量 `TUTOR_API_KEY`。"
    )


def _render_knowledge_table(kps: list) -> None:
    """Render knowledge points as a styled dataframe."""
    if not kps:
        st.info("未提取到任何知识点。")
        return

    st.subheader(f"📋 知识点列表（共 {len(kps)} 个）")

    # Build display rows
    rows: list[dict] = []
    for i, kp in enumerate(kps):
        prereq_topics = _resolve_prereq_topics(kp, kps)
        successor_topics = _resolve_successor_topics(kp, kps)

        rows.append(
            {
                "#": i + 1,
                "主题": kp.title or "（未命名）",
                "摘要": kp.summary or kp.content[:80] + "…",
                "难度": kp.difficulty,
                "关键词": ", ".join(kp.keywords[:5]) if kp.keywords else "—",
                "前置知识点": prereq_topics or "—",
                "后继知识点": successor_topics or "—",
            }
        )

    # Render with coloured difficulty pills via column_config
    st.dataframe(
        rows,
        use_container_width=True,
        column_config={
            "#": st.column_config.NumberColumn("#", width="small"),
            "主题": st.column_config.TextColumn("主题", width="medium"),
            "摘要": st.column_config.TextColumn("摘要", width="large"),
            "难度": st.column_config.TextColumn("难度", width="small"),
            "关键词": st.column_config.TextColumn("关键词", width="medium"),
            "前置知识点": st.column_config.TextColumn("前置知识点", width="medium"),
            "后继知识点": st.column_config.TextColumn("后继知识点", width="medium"),
        },
        hide_index=True,
    )


def _resolve_prereq_topics(kp, all_kps: list) -> str:
    """Map prerequisite_ids → topic names for display."""
    id_to_title: dict[str, str] = {k.kp_id: k.title for k in all_kps}
    names = [
        id_to_title.get(pid, pid[:8] + "…")
        for pid in kp.prerequisite_ids
    ]
    return " → ".join(names) if names else ""


def _resolve_successor_topics(kp, all_kps: list) -> str:
    """Map successor_ids → title names for display."""
    id_to_title: dict[str, str] = {k.kp_id: k.title for k in all_kps}
    names = [
        id_to_title.get(sid, sid[:8] + "…")
        for sid in kp.successor_ids
    ]
    return " → ".join(names) if names else ""


# ===================================================================
# Main page
# ===================================================================


def main() -> None:
    """Streamlit entry point."""
    st.set_page_config(
        page_title="Super Tutor — 智能教学系统",
        page_icon="🎓",
        layout="wide",
    )

    st.title("🎓 Super Tutor — 智能教学系统")
    st.caption("上传教材 → AI 提取知识点 → 诊断评估 → 个性化学习计划")

    st.divider()

    # -- Lazy-init services ------------------------------------------------
    if _S_DB not in st.session_state:
        with st.spinner("正在初始化服务…"):
            db, llm, engine = _init_services()
            _run_async(db.initialize())
            st.session_state[_S_DB] = db
            st.session_state[_S_LLM] = llm
            st.session_state[_S_ENGINE] = engine

    db: Database = st.session_state[_S_DB]
    llm: LLMClient | None = st.session_state[_S_LLM]
    engine: KnowledgeEngine | None = st.session_state[_S_ENGINE]

    if llm is None:
        _render_api_key_warning()

    # -- Input section -----------------------------------------------------
    st.subheader("📥 导入教材")

    tab_pdf, tab_text = st.tabs(["📄 上传 PDF", "✏️ 粘贴文本"])

    with tab_pdf:
        pdf_file = st.file_uploader(
            "上传 PDF 教材文件",
            type=["pdf"],
            help="支持文字型 PDF，扫描件（图片 PDF）请先用 OCR 工具转文字。",
        )
        if pdf_file:
            st.caption(
                f"已选择: **{pdf_file.name}** "
                f"({pdf_file.size / 1024:.1f} KB)"
            )

    with tab_text:
        text_input = st.text_area(
            "或直接粘贴教材文本",
            height=300,
            placeholder="在此粘贴教材内容…\n\n支持 Markdown 格式。",
        )

    # -- Options -----------------------------------------------------------
    col1, col2 = st.columns(2)
    with col1:
        course_type = st.selectbox(
            "课程类型",
            COURSE_TYPES,
            index=0,
            help="用于 LLM 更准确地识别知识点边界和难度。",
        )
    with col2:
        material_title = st.text_input(
            "教材标题（可选）",
            placeholder="如：高中物理必修一·第三章",
        )

    st.divider()

    # -- Parse button ------------------------------------------------------
    parse_disabled = (engine is None) or (not pdf_file and not text_input.strip())

    if st.button("🔍 开始解析", type="primary", disabled=parse_disabled):
        _do_parse(
            db=db,
            engine=engine,
            pdf_file=pdf_file,
            text_input=text_input,
            course_type=course_type,
            material_title=material_title,
        )

    # -- Results section ---------------------------------------------------
    if _S_KPS in st.session_state:
        st.divider()
        kps = st.session_state[_S_KPS]
        _render_knowledge_table(kps)

        st.divider()
        col_confirm, col_reset = st.columns([3, 1])
        with col_confirm:
            if st.button(
                "✅ 确认，开始诊断评估 →",
                type="primary",
                use_container_width=True,
            ):
                st.session_state[_S_QUIZ_MODE] = True
                st.rerun()
        with col_reset:
            if st.button("🔄 重新上传", use_container_width=True):
                _clear_results()
                st.rerun()

    # -- Quiz section ------------------------------------------------------
    if st.session_state.get(_S_QUIZ_MODE) and _S_KPS in st.session_state:
        st.divider()

        # ---- Tab: Quiz / Wrong Book -------------------------------------
        tab_quiz, tab_wrong, tab_assessment, tab_plan = st.tabs(
            ["📝 练习答题", "📖 错题本", "🔬 诊断评估", "📅 学习计划"]
        )

        kps = st.session_state[_S_KPS]
        engine = st.session_state.get(_S_ENGINE)

        # ---- Lazy-init QuizEngine (shared) ------------------------------
        if _S_QUIZ_ENGINE not in st.session_state:
            st.session_state[_S_QUIZ_ENGINE] = _init_quiz_engine()
        quiz_engine: QuizEngine | None = st.session_state[_S_QUIZ_ENGINE]

        # =================================================================
        # Tab: Quiz
        # =================================================================
        with tab_quiz:
            if quiz_engine is None:
                st.warning("⚠️ QuizEngine 不可用 — 请检查 LLM 配置。")
            else:
                # ---- KP selector -----------------------------------------
                kp_options: dict[str, str] = {
                    kp.kp_id: f"{kp.title} ({kp.difficulty})"
                    for kp in kps
                }
                selected_kp_ids = st.multiselect(
                    "选择要考查的知识点",
                    options=list(kp_options.keys()),
                    default=(
                        [st.session_state[_S_PLAN_ACTIVE_KP]]
                        if st.session_state.get(_S_PLAN_ACTIVE_KP)
                        and st.session_state[_S_PLAN_ACTIVE_KP] in kp_options
                        else list(kp_options.keys())
                    ),
                    format_func=lambda kid: kp_options.get(kid, kid[:8] + "…"),
                    help="可多选，题目将均匀分布在所选知识点上。",
                )

                # ---- Quiz options ----------------------------------------
                col_count, col_diff, col_types = st.columns(3)
                with col_count:
                    question_count = st.slider(
                        "题目数量", min_value=1, max_value=20, value=5
                    )
                with col_diff:
                    difficulty = st.selectbox(
                        "难度",
                        ["自动"] + [d.value for d in DifficultyLevel],
                        index=0,
                        help='选择"自动"则由 AI 按默认比例分配难度。',
                    )
                with col_types:
                    all_types = [t.value for t in QuestionType]
                    selected_types: list[str] | None = st.multiselect(
                        "题型（留空=全部）",
                        all_types,
                        default=[],
                        help="留空则覆盖全部题型。",
                    )
                    if not selected_types:
                        selected_types = None

                # ---- Generate button -------------------------------------
                if st.button(
                    "🎲 生成题目",
                    type="primary",
                    disabled=len(selected_kp_ids) == 0,
                    use_container_width=True,
                ):
                    _do_generate_quiz(
                        quiz_engine=quiz_engine,
                        selected_kp_ids=selected_kp_ids,
                        count=question_count,
                        difficulty=None if difficulty == "自动" else difficulty,
                        types=selected_types,
                    )
                    st.rerun()

            # ---- Render questions ----------------------------------------
            questions: list[Question] | None = st.session_state.get(_S_QUESTIONS)

            if questions:
                st.divider()
                st.caption(
                    f"已生成 **{len(questions)}** 道题目 · "
                    f"知识点: {', '.join(dict.fromkeys(q.kp_id[:8]+'…' for q in questions if q.kp_id))}"
                )

                student_answers: list[dict] = []
                all_answered = True

                for i, q in enumerate(questions):
                    st.markdown("---")
                    answer = _render_question(q, i)
                    student_answers.append(
                        {
                            "question_id": q.question_id,
                            "student_answer": answer,
                        }
                    )
                    if answer is None or answer == "" or answer == {}:
                        all_answered = False

                # ---- Submit button ---------------------------------------
                st.markdown("---")
                submitted = st.session_state.get(_S_QUIZ_SUBMITTED, False)

                if not submitted:
                    if st.button(
                        "📩 提交答案",
                        type="primary",
                        use_container_width=True,
                        disabled=not all_answered,
                    ):
                        _do_grade_quiz(
                            quiz_engine=quiz_engine,
                            questions=questions,
                            student_answers=student_answers,
                        )
                        st.rerun()
                    if not all_answered:
                        st.caption("⚠️ 请完成所有题目后再提交。")

                # ---- Results ---------------------------------------------
                attempts: list[QuizAttempt] | None = st.session_state.get(
                    _S_ATTEMPTS
                )
                if submitted and attempts:
                    _render_quiz_results(attempts, questions)
                    if st.button("🔄 重新出题", use_container_width=True):
                        st.session_state.pop(_S_QUESTIONS, None)
                        st.session_state.pop(_S_ATTEMPTS, None)
                        st.session_state.pop(_S_QUIZ_SUBMITTED, None)
                        st.rerun()

        # =================================================================
        # Tab: Wrong Book
        # =================================================================
        with tab_wrong:
            _render_wrong_book(db=db, kps=kps)

        # =================================================================
        # Tab: Diagnostic Assessment
        # =================================================================
        with tab_assessment:
            _render_assessment_tab(kps=kps)

        # =================================================================
        # Tab: Study Plan
        # =================================================================
        with tab_plan:
            _render_plan_tab(kps=kps)

    # -- Error display -----------------------------------------------------
    if _S_PARSE_ERROR in st.session_state:
        st.error(st.session_state[_S_PARSE_ERROR])
        if st.button("❌ 清除错误"):
            del st.session_state[_S_PARSE_ERROR]
            st.rerun()


# ===================================================================
# Parse logic
# ===================================================================


def _do_parse(
    db: Database,
    engine: KnowledgeEngine,
    pdf_file,
    text_input: str,
    course_type: str,
    material_title: str,
) -> None:
    """Execute the full parse pipeline: extract → persist material → parse KPs."""
    # -- Determine content source ------------------------------------------
    try:
        if pdf_file is not None:
            with st.spinner("📖 正在从 PDF 提取文本…"):
                content = _extract_pdf_text(pdf_file.getvalue())
                source_label = pdf_file.name
        else:
            content = text_input.strip()
            source_label = "手动粘贴"
    except (ImportError, ValueError) as exc:
        st.session_state[_S_PARSE_ERROR] = str(exc)
        st.rerun()

    if not content:
        st.session_state[_S_PARSE_ERROR] = "教材内容为空，请上传 PDF 或粘贴文本。"
        st.rerun()

    st.info(f"📊 已提取 **{len(content):,}** 字符（来源: {source_label}）")

    # -- Persist material --------------------------------------------------
    now = datetime.now(timezone.utc).isoformat()
    material_id = str(uuid4())
    title = material_title.strip() or source_label

    _run_async(
        db.create_material(
            {
                "material_id": material_id,
                "title": title,
                "content": content,
                "course_type": course_type,
                "status": "processing",
                "created_at": now,
                "updated_at": now,
            }
        )
    )
    logger.info("Material %s created: %s", material_id, title)

    # -- Parse knowledge points --------------------------------------------
    with st.spinner("🤖 AI 正在提取知识点…"):
        try:
            kps = _run_async(
                engine.parse(
                    content=content,
                    course_type=course_type,
                    material_id=material_id,
                )
            )
        except Exception as exc:
            _run_async(
                db.update_material(
                    material_id, {"status": "error", "updated_at": datetime.now(timezone.utc).isoformat()}
                )
            )
            st.session_state[_S_PARSE_ERROR] = (
                f"知识点解析失败: {exc}"
            )
            st.rerun()

    # -- Mark material ready -----------------------------------------------
    _run_async(
        db.update_material(
            material_id, {"status": "ready", "updated_at": datetime.now(timezone.utc).isoformat()}
        )
    )

    # -- Store results -----------------------------------------------------
    _clear_results()
    st.session_state[_S_KPS] = kps
    st.session_state[_S_MATERIAL_ID] = material_id

    logger.info(
        "Parse complete: %d KPs for material %s", len(kps), material_id
    )
    st.rerun()


def _clear_results() -> None:
    """Remove cached parse results from session state."""
    st.session_state.pop(_S_KPS, None)
    st.session_state.pop(_S_MATERIAL_ID, None)
    st.session_state.pop(_S_PARSE_ERROR, None)
    st.session_state.pop(_S_QUIZ_MODE, None)
    st.session_state.pop(_S_QUESTIONS, None)
    st.session_state.pop(_S_ATTEMPTS, None)
    st.session_state.pop(_S_QUIZ_SUBMITTED, None)
    st.session_state.pop(_S_ASSESSMENT_QUESTIONS, None)
    st.session_state.pop(_S_ASSESSMENT_REPORT, None)
    st.session_state.pop(_S_ASSESSMENT_SUBMITTED, None)
    st.session_state.pop(_S_PLAN, None)
    st.session_state.pop(_S_PLAN_ACTIVE_KP, None)
    st.session_state.pop(_S_SOCRATIC_ACTIVE, None)
    st.session_state.pop(_S_SOCRATIC_TURN, None)
    st.session_state.pop(_S_SOCRATIC_HISTORY, None)


# ===================================================================
# Quiz engine lazy-init
# ===================================================================


def _init_quiz_engine() -> QuizEngine | None:
    """Initialize QuizEngine, reusing existing DB, LLM and KnowledgeEngine."""
    db: Database | None = st.session_state.get(_S_DB)
    llm: LLMClient | None = st.session_state.get(_S_LLM)
    engine: KnowledgeEngine | None = st.session_state.get(_S_ENGINE)

    if not db or not llm or not engine:
        return None

    try:
        return QuizEngine(db=db, llm_client=llm, knowledge_engine=engine)
    except Exception as exc:
        logger.warning("Failed to init QuizEngine: %s", exc)
        return None


def _init_assessment_engine() -> AssessmentEngine | None:
    """Initialize AssessmentEngine, reusing existing DB, LLM, KnowledgeEngine and QuizEngine."""
    db: Database | None = st.session_state.get(_S_DB)
    llm: LLMClient | None = st.session_state.get(_S_LLM)
    knowledge_engine: KnowledgeEngine | None = st.session_state.get(_S_ENGINE)
    quiz_engine: QuizEngine | None = st.session_state.get(_S_QUIZ_ENGINE)

    if not db or not llm or not knowledge_engine:
        return None

    # Ensure QuizEngine is initialized (AssessmentEngine needs it for grading)
    if quiz_engine is None:
        quiz_engine = _init_quiz_engine()
        if quiz_engine is not None:
            st.session_state[_S_QUIZ_ENGINE] = quiz_engine

    if quiz_engine is None:
        return None

    try:
        return AssessmentEngine(
            db=db,
            llm_client=llm,
            knowledge_engine=knowledge_engine,
            quiz_engine=quiz_engine,
        )
    except Exception as exc:
        logger.warning("Failed to init AssessmentEngine: %s", exc)
        return None


def _init_socratic_engine() -> SocraticEngine | None:
    """Initialize SocraticEngine, reusing existing DB and LLM client."""
    db: Database | None = st.session_state.get(_S_DB)
    llm: LLMClient | None = st.session_state.get(_S_LLM)

    if not db or not llm:
        return None

    try:
        return SocraticEngine(db=db, llm_client=llm)
    except Exception as exc:
        logger.warning("Failed to init SocraticEngine: %s", exc)
        return None


# ===================================================================
# Question renderer (dispatches by type)
# ===================================================================


def _render_question(question: Question, index: int, prefix: str = "quiz"):
    """Render a single question and return the student's answer.

    Dispatches to the appropriate Streamlit widget based on question type.
    Returns ``None`` when the student has not yet provided an answer.
    """
    stem_md = f"**Q{index + 1}.** {question.stem}"
    q_type = (
        question.type.value
        if isinstance(question.type, QuestionType)
        else str(question.type)
    )

    # -- Metadata pills -------------------------------------------------------
    meta_parts: list[str] = []
    if question.difficulty:
        diff_val = (
            question.difficulty.value
            if isinstance(question.difficulty, DifficultyLevel)
            else str(question.difficulty)
        )
        meta_parts.append(f"`{diff_val}`")
    if question.kp_id:
        meta_parts.append(f"KP: `{question.kp_id[:8]}…`")
    if question.points:
        meta_parts.append(f"{question.points} 分")
    if meta_parts:
        st.caption("  ".join(meta_parts))

    # -- Dispatch by type -----------------------------------------------------

    if q_type == "multiple_choice":
        options = question.options or []
        if not options:
            st.warning("（该选择题缺少选项）")
            return None
        labels = [f"{o['key']}. {o['text']}" for o in options]
        choice = st.radio(
            stem_md, labels, key=f"{prefix}_q_{index}", index=None
        )
        return choice.split(".", 1)[0] if choice else None

    elif q_type == "true_false":
        return st.radio(
            stem_md,
            ["对", "错"],
            key=f"{prefix}_q_{index}",
            index=None,
            horizontal=True,
        )

    elif q_type == "fill_in_blank":
        return st.text_input(stem_md, key=f"{prefix}_q_{index}")

    elif q_type == "short_answer":
        return st.text_area(stem_md, height=100, key=f"{prefix}_q_{index}")

    elif q_type == "essay":
        return st.text_area(stem_md, height=200, key=f"{prefix}_q_{index}")

    else:
        # coding or any future type — fallback to text area
        return st.text_area(stem_md, height=150, key=f"{prefix}_q_{index}")


# ===================================================================
# Quiz generation
# ===================================================================


def _do_generate_quiz(
    quiz_engine: QuizEngine,
    selected_kp_ids: list[str],
    count: int,
    difficulty: str | None,
    types: list[str] | None,
) -> None:
    """Call QuizEngine.generate_questions() and store results in session."""
    with st.spinner(f"🤖 正在生成 {count} 道题目…"):
        try:
            questions = _run_async(
                quiz_engine.generate_questions(
                    kp_ids=selected_kp_ids,
                    count=count,
                    difficulty=difficulty,
                    types=types,
                )
            )
        except Exception as exc:
            st.error(f"题目生成失败: {exc}")
            return

    st.session_state[_S_QUESTIONS] = questions
    st.session_state[_S_QUIZ_SUBMITTED] = False
    st.session_state[_S_ATTEMPTS] = None
    logger.info("Generated %d questions for %d KPs", len(questions), len(selected_kp_ids))


# ===================================================================
# Quiz grading
# ===================================================================


def _do_grade_quiz(
    quiz_engine: QuizEngine,
    questions: list[Question],
    student_answers: list[dict],
) -> None:
    """Grade answers, store attempts, and add wrong answers to wrong book."""
    if not student_answers:
        st.warning("请至少回答一道题目。")
        return

    # -- Grade ----------------------------------------------------------------
    with st.spinner("🔍 正在批改…"):
        try:
            attempts = _run_async(
                quiz_engine.grade_answers(
                    questions=questions,
                    student_answers=student_answers,
                    student_id="default",
                )
            )
        except Exception as exc:
            st.error(f"批改失败: {exc}")
            return

        # -- Persist wrong answers --------------------------------------------
        q_map: dict[str, Question] = {q.question_id: q for q in questions}
        wrong_attempts = [a for a in attempts if a.is_correct is False]

        async def _batch_add_wrong_book():
            """Batch-persist all wrong attempts to the wrong-question book.

            Returns the count of successfully added entries.
            Failed entries are logged but do not block the batch.
            """
            count = 0
            for attempt in wrong_attempts:
                try:
                    await quiz_engine.add_to_wrong_book(
                        attempt, q_map.get(attempt.question_id)
                    )
                    count += 1
                except Exception as exc:
                    logger.warning(
                        "Failed to add wrong-book entry for %s: %s",
                        attempt.attempt_id,
                        exc,
                    )
            return count

        wrong_count = 0
        if wrong_attempts:
            wrong_count = _run_async(_batch_add_wrong_book())
            if wrong_count < len(wrong_attempts):
                st.warning(
                    f"⚠️ {len(wrong_attempts) - wrong_count} 道错题未能录入错题本。"
                )

    st.session_state[_S_ATTEMPTS] = attempts
    st.session_state[_S_QUIZ_SUBMITTED] = True

    correct = sum(1 for a in attempts if a.is_correct)
    logger.info(
        "Grading complete: %d/%d correct, %d wrong-book entries",
        correct,
        len(attempts),
        wrong_count,
    )


# ===================================================================
# Quiz results display
# ===================================================================


def _render_quiz_results(attempts: list[QuizAttempt], questions: list[Question]) -> None:
    """Display grading results with per-question feedback."""
    q_map: dict[str, Question] = {q.question_id: q for q in questions}
    correct = sum(1 for a in attempts if a.is_correct)
    total = len(attempts)

    st.divider()
    st.subheader(f"📊 批改结果（{correct}/{total} 正确）")

    if total > 0:
        pct = correct / total
        color = (
            "#34D399" if pct >= 0.8 else "#FBBF24" if pct >= 0.5 else "#F87171"
        )
        st.markdown(
            f"### 正确率: <span style='color:{color}'>{pct:.0%}</span>",
            unsafe_allow_html=True,
        )

    for i, attempt in enumerate(attempts):
        q = q_map.get(attempt.question_id)
        if q is None:
            continue

        icon = "✅" if attempt.is_correct else "❌"
        with st.expander(f"{icon} Q{i + 1}. {q.stem[:80]}{'…' if len(q.stem) > 80 else ''}"):
            st.caption(f"**题型**: {q.type.value if isinstance(q.type, QuestionType) else q.type}")
            if q.kp_id:
                st.caption(f"**知识点**: `{q.kp_id}`")

            if not attempt.is_correct:
                st.markdown("**你的答案:**")
                st.text(str(attempt.student_answer or "（未作答）"))
                st.markdown("**正确答案:**")
                correct_raw = q.correct_answer
                if isinstance(correct_raw, (dict, list)):
                    st.json(correct_raw)
                else:
                    st.text(str(correct_raw))

            if q.explanation:
                st.markdown("**解析:**")
                st.info(q.explanation)

            if q.hints:
                st.markdown("**提示:**")
                for h in q.hints:
                    st.caption(f"💡 {h}")


# ===================================================================
# Assessment — diagnostic assessment helpers
# ===================================================================


def _do_assessment_generate(assessment_engine: AssessmentEngine, kps: list) -> None:
    """Generate diagnostic assessment questions for all knowledge points."""
    kp_ids = [kp.kp_id for kp in kps]
    if not kp_ids:
        st.warning("没有可用的知识点。")
        return

    question_count = min(max(len(kp_ids), 15), 30)  # 15-30 questions

    with st.spinner(f"🔬 正在生成诊断性评估题目（覆盖 {len(kp_ids)} 个知识点）…"):
        try:
            questions = _run_async(
                assessment_engine.generate(
                    kp_ids=kp_ids,
                    student_id="default",
                    question_count=question_count,
                )
            )
        except Exception as exc:
            st.error(f"评估题目生成失败: {exc}")
            return

    st.session_state[_S_ASSESSMENT_QUESTIONS] = questions
    st.session_state[_S_ASSESSMENT_SUBMITTED] = False
    st.session_state[_S_ASSESSMENT_REPORT] = None
    logger.info(
        "Assessment generated: %d questions for %d KPs",
        len(questions),
        len(kp_ids),
    )


def _do_assessment_grade(
    assessment_engine: AssessmentEngine,
    questions: list[Question],
    student_answers: list[dict],
    kps: list,
) -> None:
    """Grade assessment answers and produce a mastery report."""
    if not student_answers:
        st.warning("请至少回答一道题目。")
        return

    with st.spinner("🔍 正在批改并生成诊断报告…"):
        try:
            report = _run_async(
                assessment_engine.grade(
                    questions=questions,
                    student_answers=student_answers,
                    student_id="default",
                )
            )
        except Exception as exc:
            st.error(f"评估批改失败: {exc}")
            return

    st.session_state[_S_ASSESSMENT_REPORT] = report
    st.session_state[_S_ASSESSMENT_SUBMITTED] = True

    logger.info(
        "Assessment complete: %.1f%% accuracy, weak=%d strong=%d rules=%d",
        report.accuracy * 100,
        len(report.weak_kps),
        len(report.strong_kps),
        len(report.rules_applied),
    )


def _render_assessment_report(report: AssessmentReport, kps: list) -> None:
    """Render the diagnostic assessment report.

    Displays overall stats, weak/strong KP lists, recommended learning order,
    and a placeholder button for generating a study plan.
    """
    st.divider()
    st.subheader("🔬 诊断评估报告")

    # ---- Overall stats -------------------------------------------------
    col_stat1, col_stat2, col_stat3 = st.columns(3)
    with col_stat1:
        pct = report.accuracy
        color = (
            "#34D399" if pct >= 0.8 else "#FBBF24" if pct >= 0.5 else "#F87171"
        )
        st.markdown(
            f"### 正确率: <span style='color:{color}'>{pct:.0%}</span>",
            unsafe_allow_html=True,
        )
    with col_stat2:
        st.metric("正确数 / 总题数", f"{report.correct_count} / {report.total_questions}")
    with col_stat3:
        dist = report.mastery_distribution
        st.metric(
            "知识点掌握状态",
            f"✅{dist['mastered']} 📖{dist['learning']} "
            f"🔍{dist['need_review']} 🔴{dist['need_relearn']}",
        )

    # ---- Report-level warnings ------------------------------------------
    if report.warnings:
        for w in report.warnings:
            st.warning(w)

    # ---- Weak KPs -------------------------------------------------------
    st.markdown("---")
    if report.weak_kps:
        st.markdown("### ⚠️ 薄弱知识点")
        cols = st.columns(min(len(report.weak_kps), 3))
        for i, r in enumerate(report.weak_kps):
            with cols[i % len(cols)]:
                status_icon = {
                    "need_review": "🔍",
                    "need_relearn": "🔴",
                }.get(r.status, "🟡")
                st.error(
                    f"{status_icon} **{r.title or r.kp_id[:8] + '…'}**\n\n"
                    f"掌握度: {r.adjusted_mastery:.0%}  "
                    f"({r.correct_count}/{r.total_count} 正确)\n\n"
                    + ("\n".join(f"• {w}" for w in r.warnings[-2:])
                       if r.warnings else "")
                )
    else:
        st.success("🎉 没有薄弱知识点！所有知识点掌握度均 > 0.5。")

    # ---- Strong KPs -----------------------------------------------------
    st.markdown("---")
    if report.strong_kps:
        st.markdown("### ⭐ 强项知识点")
        cols = st.columns(min(len(report.strong_kps), 3))
        for i, r in enumerate(report.strong_kps):
            with cols[i % len(cols)]:
                st.success(
                    f"✅ **{r.title or r.kp_id[:8] + '…'}**\n\n"
                    f"掌握度: {r.adjusted_mastery:.0%}  "
                    f"({r.correct_count}/{r.total_count} 正确)"
                )
    else:
        st.info("还没有强项知识点 — 继续加油！")

    # ---- Recommended learning order -------------------------------------
    st.markdown("---")
    st.markdown("### 📋 建议学习顺序")
    st.caption("按知识点依赖关系（拓扑排序），从基础到高级排列。")

    # Build KP title lookup
    kp_map: dict[str, Any] = {}
    for kp in kps:
        kp_map[kp.kp_id] = kp

    for i, r in enumerate(report.kp_results):
        prereq_titles = []
        for pid in r.prerequisite_ids:
            pkp = kp_map.get(pid)
            prereq_titles.append(pkp.title if pkp else pid[:8] + "…")

        succ_titles = []
        for sid in r.successor_ids:
            skp = kp_map.get(sid)
            succ_titles.append(skp.title if skp else sid[:8] + "…")

        mastery_bar_color = (
            "#34D399" if r.adjusted_mastery >= 0.8
            else "#FBBF24" if r.adjusted_mastery >= 0.5
            else "#F87171"
        )

        st.markdown(
            f"**{i + 1}.** {r.title or r.kp_id[:8] + '…'}  "
            f"<span style='color:{mastery_bar_color}'>"
            f"掌握度 {r.adjusted_mastery:.0%}</span>  "
            f"({r.correct_count}/{r.total_count} 正确)",
            unsafe_allow_html=True,
        )
        col_pre, col_succ, col_status = st.columns([2, 2, 1])
        with col_pre:
            st.caption(
                f"⬅️ 前置: {', '.join(prereq_titles) if prereq_titles else '无（链首）'}"
            )
        with col_succ:
            st.caption(
                f"➡️ 后继: {', '.join(succ_titles) if succ_titles else '无（链尾）'}"
            )
        with col_status:
            status_label = {
                "mastered": "✅ 已掌握",
                "learning": "📖 学习中",
                "need_review": "🔍 需复习",
                "need_relearn": "🔴 需重学",
            }.get(r.status, r.status)
            st.caption(status_label)

    # ---- Rules applied --------------------------------------------------
    if report.rules_applied:
        st.markdown("---")
        with st.expander(f"🔧 前置规则校准详情（{len(report.rules_applied)} 条）"):
            for rule_msg in report.rules_applied:
                st.caption(f"• {rule_msg}")

    # ---- Generate study plan button -------------------------------------
    st.markdown("---")
    plan_already_generated = _S_PLAN in st.session_state
    col_plan, col_retry = st.columns(2)
    with col_plan:
        if st.button(
            "🔄 重新生成学习计划" if plan_already_generated else "📅 生成学习计划 →",
            type="primary",
            use_container_width=True,
            help="根据诊断结果生成个性化学习计划（拓扑排序 + 优先级排期）。",
        ):
            _do_generate_plan(report)
            st.rerun()
    with col_retry:
        if st.button(
            "🔄 重新诊断",
            use_container_width=True,
            help="重新生成评估题目并再次诊断。",
        ):
            st.session_state.pop(_S_ASSESSMENT_QUESTIONS, None)
            st.session_state.pop(_S_ASSESSMENT_REPORT, None)
            st.session_state.pop(_S_ASSESSMENT_SUBMITTED, None)
            st.session_state.pop(_S_PLAN, None)
            st.rerun()


# ===================================================================
# Assessment tab renderer
# ===================================================================


def _render_assessment_tab(kps: list) -> None:
    """Render the diagnostic assessment tab.

    Flow:
    1. Lazy-init AssessmentEngine
    2. Show "开始诊断" button → generate assessment questions
    3. Render questions → collect answers → "提交评估"
    4. Show AssessmentReport
    """
    # ---- Lazy-init AssessmentEngine ------------------------------------
    if _S_ASSESSMENT_ENGINE not in st.session_state:
        st.session_state[_S_ASSESSMENT_ENGINE] = _init_assessment_engine()
    assessment_engine: AssessmentEngine | None = st.session_state[_S_ASSESSMENT_ENGINE]

    if assessment_engine is None:
        st.warning("⚠️ AssessmentEngine 不可用 — 请检查 LLM 配置。")
        return

    # ---- Get current assessment state ----------------------------------
    questions: list[Question] | None = st.session_state.get(_S_ASSESSMENT_QUESTIONS)
    submitted: bool = st.session_state.get(_S_ASSESSMENT_SUBMITTED, False)
    report: AssessmentReport | None = st.session_state.get(_S_ASSESSMENT_REPORT)

    kp_ids = [kp.kp_id for kp in kps]

    # ---- Step 1: Start diagnosis button --------------------------------
    if questions is None:
        st.info(
            "🔬 **诊断性评估** 将对所有知识点生成一套诊断题目，"
            "通过前置依赖规则精准定位薄弱环节。"
        )

        col_info1, col_info2, col_info3 = st.columns(3)
        with col_info1:
            st.metric("知识点数量", len(kps))
        with col_info2:
            st.metric("预计题目数", min(max(len(kps), 15), 30))
        with col_info3:
            st.caption("覆盖从前驱到后继\n的完整依赖链")

        if st.button(
            "🚀 开始诊断",
            type="primary",
            use_container_width=True,
            disabled=len(kp_ids) == 0,
        ):
            _do_assessment_generate(
                assessment_engine=assessment_engine,
                kps=kps,
            )
            st.rerun()
        return

    # ---- Step 2: Render questions --------------------------------------
    if not submitted:
        st.caption(
            f"已生成 **{len(questions)}** 道诊断性评估题目 · "
            f"知识点: {len(kp_ids)} 个"
        )

        student_answers: list[dict] = []
        all_answered = True

        for i, q in enumerate(questions):
            st.markdown("---")
            answer = _render_question(q, i, prefix="assess")
            student_answers.append(
                {
                    "question_id": q.question_id,
                    "student_answer": answer,
                    "time_spent_seconds": 0,
                }
            )
            if answer is None or answer == "" or answer == {}:
                all_answered = False

        # ---- Submit button ----------------------------------------------
        st.markdown("---")
        if st.button(
            "📩 提交评估",
            type="primary",
            use_container_width=True,
            disabled=not all_answered,
        ):
            _do_assessment_grade(
                assessment_engine=assessment_engine,
                questions=questions,
                student_answers=student_answers,
                kps=kps,
            )
            st.rerun()
        if not all_answered:
            st.caption("⚠️ 请完成所有题目后再提交。")
        return

    # ---- Step 3: Show report -------------------------------------------
    if report is not None:
        _render_assessment_report(report, kps)


# ===================================================================
# Plan generation helpers
# ===================================================================


def _do_generate_plan(report: AssessmentReport) -> None:
    """Generate a study plan from the assessment report via PlanEngine.

    Builds a mastery map from the report's KP results, then calls
    PlanEngine.generate() to create a topologically-sorted plan with
    priority-scored schedule items.  The resulting StudyPlan is stored
    in session state.
    """
    db: Database = st.session_state[_S_DB]

    mastery_map: dict[str, float] = {
        r.kp_id: r.adjusted_mastery for r in report.kp_results
    }
    kp_ids: list[str] = [r.kp_id for r in report.kp_results]

    if not kp_ids:
        st.warning("评估报告中没有知识点，无法生成计划。")
        return

    plan_engine = PlanEngine(db)

    with st.spinner("📅 正在生成个性化学习计划…"):
        try:
            plan = _run_async(
                plan_engine.generate(
                    kp_ids=kp_ids,
                    mastery_map=mastery_map,
                    student_id="default",
                    plan_title="个性化学习计划",
                    plan_goal="根据诊断评估结果，系统掌握所有知识点",
                )
            )
        except Exception as exc:
            st.error(f"学习计划生成失败: {exc}")
            logger.exception("Plan generation failed")
            return

    st.session_state[_S_PLAN] = plan
    logger.info(
        "Study plan generated: plan_id=%s kps=%d items=%d",
        plan.plan_id,
        len(plan.kp_sequence),
        plan.item_count,
    )


def _do_start_learning_kp(kp_id: str) -> None:
    """Generate quiz questions for a single KP and switch to quiz tab.

    Called from the learning plan page when the user clicks
    "开始学习此知识点 →" on a specific KP card.
    """
    quiz_engine: QuizEngine | None = st.session_state.get(_S_QUIZ_ENGINE)
    if quiz_engine is None:
        st.warning("⚠️ QuizEngine 不可用。")
        return

    with st.spinner(f"🎲 正在为知识点生成题目…"):
        try:
            questions = _run_async(
                quiz_engine.generate_questions(
                    kp_ids=[kp_id],
                    count=5,
                )
            )
        except Exception as exc:
            st.error(f"题目生成失败: {exc}")
            return

    st.session_state[_S_QUESTIONS] = questions
    st.session_state[_S_QUIZ_SUBMITTED] = False
    st.session_state[_S_ATTEMPTS] = None
    st.session_state[_S_PLAN_ACTIVE_KP] = kp_id

    logger.info("Plan learning start: KP=%s questions=%d", kp_id, len(questions))


# ===================================================================
# Plan tab renderer
# ===================================================================


def _render_plan_tab(kps: list) -> None:
    """Render the learning plan tab.

    Displays the topologically-sorted knowledge point learning path
    with mastery progress bars, difficulty tags, and per-KP action
    buttons.  Knowledge points with mastery < 0.5 are highlighted
    in red as priority learning items.

    Flow:
    1. If no plan exists and no report → show instructions.
    2. If no plan but report exists → show "生成学习计划" button.
    3. If plan exists → render the full learning path.
    """
    plan: StudyPlan | None = st.session_state.get(_S_PLAN)
    report: AssessmentReport | None = st.session_state.get(_S_ASSESSMENT_REPORT)

    # ---- No plan yet -------------------------------------------------------
    if plan is None:
        if report is None:
            st.info(
                "📅 **学习计划**\n\n"
                "请先完成以下步骤：\n\n"
                "1. 上传教材并解析知识点\n"
                "2. 在 **「🔬 诊断评估」** 标签页完成诊断评估\n"
                "3. 在评估报告中点击 **「生成学习计划」**"
            )
        else:
            st.success("✅ 诊断评估已完成，可以生成学习计划了！")
            if st.button(
                "📅 生成学习计划 →",
                type="primary",
                use_container_width=True,
            ):
                _do_generate_plan(report)
                st.rerun()
        return

    # ---- Plan exists — render ---------------------------------------------
    # Build mastery lookup from report
    mastery_map: dict[str, float] = {}
    if report:
        for r in report.kp_results:
            mastery_map[r.kp_id] = r.adjusted_mastery

    # Build KP title/difficulty lookup
    kp_map: dict[str, Any] = {kp.kp_id: kp for kp in kps}

    # Fill in KPs not in current material (fetched from DB)
    db: Database = st.session_state[_S_DB]
    for kid in plan.kp_sequence:
        if kid not in kp_map:
            kp_row = _run_async(db.get_knowledge_point(kid))
            if kp_row:
                from types import SimpleNamespace

                kp_map[kid] = SimpleNamespace(
                    kp_id=kid,
                    title=kp_row.get("title", kid[:8] + "…"),
                    difficulty=kp_row.get("difficulty", "medium"),
                )

    # ---- Header ------------------------------------------------------------
    st.subheader(f"📅 {plan.title}")

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("知识点总数", plan.item_count)
    with col2:
        st.metric("已完成", plan.completed_count)
    with col3:
        st.metric("进度", f"{plan.progress:.0%}")
    with col4:
        priority_count = sum(
            1 for kid in plan.kp_sequence if mastery_map.get(kid, 0.0) < 0.5
        )
        st.metric("优先学习", priority_count)

    st.caption(f"创建时间: {plan.created_at[:16].replace('T', ' ')}  |  "
               f"状态: {plan.status}")

    # ---- Learning path -----------------------------------------------------
    st.divider()
    st.subheader("📋 学习路径")
    st.caption("按知识点依赖关系拓扑排序，掌握度 < 50% 标记为优先学习。")

    activity_labels: dict[str, str] = {
        "learn_new": "📖 新学",
        "review": "🔍 复习",
        "practice": "✏️ 练习",
        "quiz": "📝 测验",
    }

    for i, kid in enumerate(plan.kp_sequence):
        kp = kp_map.get(kid)
        title = kp.title if kp else kid[:8] + "…"
        difficulty = kp.difficulty if kp else "medium"
        mastery = mastery_map.get(kid, 0.0)
        is_priority = mastery < 0.5

        # Find matching schedule item
        schedule_item = next(
            (it for it in plan.schedule if it.knowledge_node_id == kid), None
        )
        activity_type = schedule_item.activity_type if schedule_item else "review"
        estimated_min = schedule_item.estimated_minutes if schedule_item else 15
        activity_label = activity_labels.get(activity_type, activity_type)

        # ---- KP card -------------------------------------------------------
        priority_marker = " 🔴" if is_priority else ""
        st.markdown(f"### {i + 1}. {title}{priority_marker}")

        col_info, col_action = st.columns([3, 1])

        with col_info:
            st.progress(mastery)
            st.caption(
                f"掌握度 "
                f":{'red' if mastery < 0.5 else 'orange' if mastery < 0.8 else 'green'}"
                f"[{mastery:.0%}]  ·  "
                f"{activity_label}  ·  ⏱️ ~{estimated_min} 分钟  ·  "
                f"难度: {difficulty}"
            )

        with col_action:
            st.markdown("<br>", unsafe_allow_html=True)
            if st.button(
                "开始学习此知识点 →",
                key=f"learn_{kid}",
                use_container_width=True,
            ):
                _do_start_learning_kp(kid)
                st.rerun()

        st.divider()

    # ---- Footer actions ----------------------------------------------------
    col_regen, col_clear = st.columns(2)
    with col_regen:
        if report and st.button(
            "🔄 重新生成计划", use_container_width=True
        ):
            _do_generate_plan(report)
            st.rerun()
    with col_clear:
        if st.button("🗑 清除计划", use_container_width=True):
            st.session_state.pop(_S_PLAN, None)
            st.session_state.pop(_S_PLAN_ACTIVE_KP, None)
            st.rerun()


# ===================================================================
# Socratic dialogue helpers
# ===================================================================


def _do_start_socratic(kp_id: str, wrong_id: str) -> None:
    """Start a new Socratic dialogue for a wrong question.

    Initialises the SocraticEngine (lazy), calls ``start_dialogue()``,
    and stores the initial L1_GUIDING turn in session state.
    """
    engine: SocraticEngine | None = st.session_state.get(_S_SOCRATIC_ENGINE)
    if engine is None:
        engine = _init_socratic_engine()
        if engine is None:
            st.warning("⚠️ SocraticEngine 不可用 — 请检查 LLM 配置。")
            return
        st.session_state[_S_SOCRATIC_ENGINE] = engine

    # If clicking on a different wrong question, reset first
    if st.session_state.get(_S_SOCRATIC_ACTIVE) != wrong_id:
        _clear_socratic()

    with st.spinner("🤔 正在准备苏格拉底式引导…"):
        try:
            turn = _run_async(engine.start_dialogue(kp_id, wrong_id))
        except Exception as exc:
            st.error(f"启动苏格拉底对话失败: {exc}")
            logger.exception("Socratic start_dialogue failed")
            return

    st.session_state[_S_SOCRATIC_ACTIVE] = wrong_id
    st.session_state[_S_SOCRATIC_TURN] = turn
    st.session_state[_S_SOCRATIC_HISTORY] = []

    logger.info(
        "Socratic dialogue started: kp=%s wrong=%s turn=%s",
        kp_id,
        wrong_id,
        turn.turn_id,
    )


def _do_continue_socratic(user_response: str) -> None:
    """Continue an ongoing Socratic dialogue with the student's response.

    Builds a history entry from the current turn + user response, calls
    ``continue_dialogue()``, and updates session state with the next turn.
    """
    engine: SocraticEngine | None = st.session_state.get(_S_SOCRATIC_ENGINE)
    turn = st.session_state.get(_S_SOCRATIC_TURN)
    history: list[dict] = st.session_state.get(_S_SOCRATIC_HISTORY, [])

    if engine is None or turn is None:
        return

    # Build history entry from current turn + user response
    history.append(build_history_entry(turn, user_response))

    with st.spinner("🤔 AI 正在思考…"):
        try:
            next_turn = _run_async(
                engine.continue_dialogue(history, user_response)
            )
        except Exception as exc:
            st.error(f"对话处理失败: {exc}")
            logger.exception("Socratic continue_dialogue failed")
            return

    st.session_state[_S_SOCRATIC_HISTORY] = history
    st.session_state[_S_SOCRATIC_TURN] = next_turn

    logger.info(
        "Socratic dialogue continued: turn=%s level=%s resolved=%s",
        next_turn.turn_id,
        next_turn.level,
        next_turn.resolved,
    )


def _clear_socratic() -> None:
    """Clear all Socratic dialogue state from the session."""
    st.session_state.pop(_S_SOCRATIC_ACTIVE, None)
    st.session_state.pop(_S_SOCRATIC_TURN, None)
    st.session_state.pop(_S_SOCRATIC_HISTORY, None)


def _render_socratic_dialogue(wrong_id: str) -> None:
    """Render the Socratic dialogue UI for an active wrong question.

    Displays the conversation history (teacher + student messages),
    the current turn's teacher message, and input controls for the
    student to respond.  Supports two exit paths:

    * "我知道了 ✅" — the student feels they understand
    * "显示答案" — the student wants the full answer revealed
    """
    turn = st.session_state.get(_S_SOCRATIC_TURN)
    history: list[dict] = st.session_state.get(_S_SOCRATIC_HISTORY, [])

    if turn is None:
        return

    # ---- Level badge ---------------------------------------------------
    level_badges = {
        "L1_GUIDING": "🔵 笼统引导",
        "L2_HINTING": "🟡 具体提示",
        "L3_NEAR_ANSWER": "🟠 接近答案",
        "RESOLVED": "🟢 已解决",
        "SHOW_ANSWER": "📖 显示答案",
    }
    badge = level_badges.get(turn.level, turn.level)
    st.caption(f"🤔 苏格拉底追问 · 层级: {badge}")

    # ---- Conversation history -------------------------------------------
    for h in history:
        with st.chat_message("assistant"):
            st.markdown(h.get("teacher_message", ""))
        with st.chat_message("user"):
            st.markdown(h.get("user_response", ""))

    # ---- Current turn (teacher message) ---------------------------------
    with st.chat_message("assistant"):
        st.markdown(turn.teacher_message)

    # ---- Terminal state -------------------------------------------------
    if turn.is_terminal:
        if turn.resolution_note:
            st.caption(f"💡 {turn.resolution_note}")
        st.success("✅ 对话已结束")
        if st.button("关闭对话", key=f"socratic_close_{wrong_id}"):
            _clear_socratic()
            st.rerun()
        return

    # ---- Expected concepts hint -----------------------------------------
    if turn.expected_concepts:
        with st.expander("🧠 期望涉及的概念（教师参考）"):
            st.caption("、".join(turn.expected_concepts))

    # ---- Student input area ---------------------------------------------
    col_input, col_actions = st.columns([3, 1])

    with col_input:
        user_response = st.text_area(
            "你的回答",
            key=f"socratic_input_{wrong_id}",
            height=80,
            placeholder="输入你的想法…",
            label_visibility="collapsed",
        )

    with col_actions:
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button(
            "我知道了 ✅",
            key=f"socratic_resolve_{wrong_id}",
            use_container_width=True,
            help="我觉得已经理解了，确认我的理解。",
        ):
            _do_continue_socratic(
                "老师，我觉得我已经理解了。让我总结一下我的理解："
            )
            st.rerun()

        if st.button(
            "显示答案",
            key=f"socratic_show_{wrong_id}",
            use_container_width=True,
            help="直接显示正确答案和完整解析。",
        ):
            _do_continue_socratic("显示答案")
            st.rerun()

    # ---- Send button for text input -------------------------------------
    if st.button(
        "📤 发送",
        key=f"socratic_send_{wrong_id}",
        use_container_width=True,
        disabled=not user_response.strip(),
    ):
        _do_continue_socratic(user_response.strip())
        st.rerun()


# ===================================================================
# Wrong book
# ===================================================================


def _render_wrong_book(db: Database, kps: list) -> None:
    """Render the wrong-question book with filters, KP grouping, and actions.

    Fetches wrong-question entries from the database, displays them
    grouped by knowledge point, and provides "redo" and "Socratic
    follow-up" actions.
    """
    # ---- Fetch wrong questions --------------------------------------------
    raw_entries, total = _run_async(
        db.list_wrong_questions_by_student("default", limit=500, offset=0)
    )

    if not raw_entries:
        st.info("🎉 错题本为空 — 还没有答错的题目。")
        return

    # ---- Build KP title lookup --------------------------------------------
    kp_title_map: dict[str, str] = {}
    for kp in kps:
        kp_title_map[kp.kp_id] = kp.title

    # Enrich from DB for KPs not in the current material
    missing_kp_ids: set[str] = set()
    for entry in raw_entries:
        kid = entry.get("kp_id", "")
        if kid and kid not in kp_title_map:
            missing_kp_ids.add(kid)

    for kid in missing_kp_ids:
        kp_row = _run_async(db.get_knowledge_point(kid))
        if kp_row:
            kp_title_map[kid] = kp_row.get("title", kid[:8] + "…")
        else:
            kp_title_map[kid] = kid[:8] + "…"

    # ---- Collect all unique KPs for filter --------------------------------
    unique_kp_ids = list(dict.fromkeys(e.get("kp_id", "") for e in raw_entries))
    kp_filter_options: dict[str, str] = {
        "__all__": "全部知识点",
    }
    for kid in unique_kp_ids:
        if kid:
            title = kp_title_map.get(kid, kid[:8] + "…")
            kp_filter_options[kid] = f"{title}"

    # ---- Filters ----------------------------------------------------------
    col_kp, col_time = st.columns(2)
    with col_kp:
        selected_kp_filter = st.selectbox(
            "知识点",
            options=list(kp_filter_options.keys()),
            format_func=lambda k: kp_filter_options.get(k, k[:8] + "…"),
            key="wrong_book_kp_filter",
            help="按知识点筛选错题。",
        )
    with col_time:
        selected_time_filter = st.selectbox(
            "时间",
            options=["全部", "最近7天", "最近30天"],
            key="wrong_book_time_filter",
            help="按收录时间筛选错题。",
        )

    st.caption(f"共 **{total}** 条错题记录")

    # ---- Apply filters ----------------------------------------------------
    now_utc = datetime.now(timezone.utc)
    cutoff: str | None = None
    if selected_time_filter == "最近7天":
        cutoff = (now_utc - timedelta(days=7)).isoformat()
    elif selected_time_filter == "最近30天":
        cutoff = (now_utc - timedelta(days=30)).isoformat()

    filtered: list[dict] = []
    for entry in raw_entries:
        if selected_kp_filter != "__all__":
            if entry.get("kp_id", "") != selected_kp_filter:
                continue
        if cutoff is not None:
            created = entry.get("created_at", "")
            if created and created < cutoff:
                continue
        filtered.append(entry)

    if not filtered:
        st.info("没有符合筛选条件的错题。")
        return

    # ---- Enhance entries with question data --------------------------------
    # Batch-fetch questions to avoid N+1 queries
    qid_set: set[str] = {e["question_id"] for e in filtered if e.get("question_id")}
    q_map: dict[str, dict] = {}
    for qid in qid_set:
        q_row = _run_async(db.get_question(qid))
        if q_row:
            q_map[qid] = q_row

    # ---- Group by KP ------------------------------------------------------
    groups: dict[str, list[dict]] = {}
    for entry in filtered:
        kid = entry.get("kp_id", "") or "__unknown__"
        groups.setdefault(kid, []).append(entry)

    # Sort groups by number of wrong questions (desc)
    sorted_groups = sorted(groups.items(), key=lambda kv: len(kv[1]), reverse=True)

    # ---- Render groups ----------------------------------------------------
    for kid, entries in sorted_groups:
        kp_title = kp_title_map.get(kid, "未知知识点")
        n = len(entries)
        with st.expander(f"📌 {kp_title}（{n} 道错题）"):
            for idx, entry in enumerate(entries):
                qid = entry.get("question_id", "")
                q_row = q_map.get(qid, {})

                # --- Question stem ------------------------------------------
                stem = q_row.get("stem", "（题目已删除）")
                st.markdown(f"**Q{idx + 1}.** {stem}")

                # --- Answer comparison --------------------------------------
                col_wrong, col_correct = st.columns(2)
                with col_wrong:
                    st.markdown("❌ **你的答案:**")
                    wrong_ans = entry.get("wrong_answer") or "（未作答）"
                    try:
                        parsed = json.loads(wrong_ans)
                        st.json(parsed)
                    except (json.JSONDecodeError, TypeError):
                        st.text(wrong_ans)
                with col_correct:
                    st.markdown("✅ **正确答案:**")
                    correct_ans = entry.get("correct_answer", "（无）")
                    try:
                        parsed = json.loads(correct_ans)
                        st.json(parsed)
                    except (json.JSONDecodeError, TypeError):
                        st.text(correct_ans)

                # --- Explanation --------------------------------------------
                explanation = q_row.get("explanation", "")
                if explanation:
                    st.info(explanation)

                # --- Metadata -----------------------------------------------
                meta_cols = st.columns(4)
                with meta_cols[0]:
                    st.caption(f"📊 犯错次数: **{entry.get('attempt_count', 1)}**")
                with meta_cols[1]:
                    st.caption(
                        f"📅 {entry.get('created_at', '')[:10] if entry.get('created_at') else '—'}"
                    )
                with meta_cols[2]:
                    status = entry.get("resolution_status", "unresolved")
                    status_label = {
                        "unresolved": "🔴 未解决",
                        "reviewing": "🟡 复习中",
                        "resolved": "🟢 已解决",
                    }.get(status, status)
                    st.caption(status_label)
                with meta_cols[3]:
                    st.caption(
                        f"KP: `{kid[:8]}…`" if kid != "__unknown__" else "KP: —"
                    )

                # --- Actions -------------------------------------------------
                wrong_id = entry.get("wrong_id", "")
                col_redo, col_socratic = st.columns(2)
                with col_redo:
                    unknown_kp = kid == "__unknown__"
                    if st.button(
                        "🔄 重新作答",
                        key=f"redo_{wrong_id or idx}",
                        help=(
                            "该错题未关联知识点，无法生成针对性练习。"
                            if unknown_kp
                            else "在答题页中重新作答该知识点相关题目。"
                        ),
                        disabled=unknown_kp,
                    ):
                        _do_redo_from_wrong_book(kp_id=kid)
                        st.rerun()
                with col_socratic:
                    socratic_active = (
                        st.session_state.get(_S_SOCRATIC_ACTIVE) == wrong_id
                    )
                    if st.button(
                        "🔇 关闭追问" if socratic_active else "🗨 苏格拉底追问",
                        key=f"socratic_{wrong_id or idx}",
                        help=(
                            "关闭当前苏格拉底对话"
                            if socratic_active
                            else "由 AI 进行苏格拉底式引导追问，帮助你自主发现正确答案。"
                        ),
                    ):
                        if socratic_active:
                            _clear_socratic()
                        else:
                            _do_start_socratic(kp_id=kid, wrong_id=wrong_id)
                        st.rerun()

                # --- Socratic dialogue UI ---------------------------------
                if st.session_state.get(_S_SOCRATIC_ACTIVE) == wrong_id:
                    _render_socratic_dialogue(wrong_id)

                st.markdown("---")


def _do_redo_from_wrong_book(kp_id: str) -> None:
    """Generate a new quiz for the given KP and switch to quiz tab."""
    quiz_engine: QuizEngine | None = st.session_state.get(_S_QUIZ_ENGINE)
    if quiz_engine is None:
        st.warning("⚠️ QuizEngine 不可用。")
        return

    with st.spinner(f"🎲 正在为知识点生成题目…"):
        try:
            questions = _run_async(
                quiz_engine.generate_questions(
                    kp_ids=[kp_id],
                    count=3,
                )
            )
        except Exception as exc:
            st.error(f"题目生成失败: {exc}")
            return

    st.session_state[_S_QUESTIONS] = questions
    st.session_state[_S_QUIZ_SUBMITTED] = False
    st.session_state[_S_ATTEMPTS] = None
    st.success(
        f"✅ 已生成 {len(questions)} 道题目，请切换到 **「📝 练习答题」** 标签页作答。"
    )


# ===================================================================
# Entry point
# ===================================================================

if __name__ == "__main__":
    main()
