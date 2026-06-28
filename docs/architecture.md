# Super Tutor — 技术架构文档

**版本：** v3.2
**状态：** 已实现
**最后更新：** 2026-06-26

配套文档：[产品需求规格说明书](requirements.md)

---

## 第 1 章 · 项目结构

```
super-tutor-agent/
├── app.py                         # Streamlit 前端入口（~1970 行）
├── requirements.txt               # Python 依赖
├── README.md
│
├── super_tutor/
│   ├── __init__.py
│   ├── config.py                  # 配置管理（4 字段）
│   │
│   ├── core/
│   │   ├── __init__.py
│   │   ├── database.py            # SQLite 6 表 + 25+ CRUD 方法
│   │   ├── llm_client.py          # DeepSeek API 客户端（OpenAI SDK 封装）
│   │   └── exceptions.py          # 异常类（TutorError / LLMError / MaterialError）
│   │
│   ├── engine/
│   │   ├── __init__.py
│   │   ├── knowledge_engine.py    # 知识点解析 + 前驱/后继关系管理
│   │   ├── assessment_engine.py   # 诊断评估 + 3 条前置规则
│   │   ├── quiz_engine.py         # 出题 + 程序/LLM 混合批改 + 错题入库
│   │   ├── plan_engine.py         # 拓扑排序 + 优先级公式 + 学习计划
│   │   └── socratic_engine.py     # 苏格拉底追问（L1→L2→L3）
│   │
│   ├── models/
│   │   ├── __init__.py            # 统一导出
│   │   ├── enums.py               # DifficultyLevel + QuestionType
│   │   ├── knowledge.py           # KnowledgePoint
│   │   ├── quiz.py                # Question + QuizAttempt
│   │   ├── assessment.py          # AssessmentReport + KPAssessmentResult
│   │   ├── mastery.py             # ReviewItem
│   │   ├── plan.py                # StudyPlan
│   │   └── socratic.py            # SocraticTurn + helpers
│   │
│   └── prompts/                   # LLM Prompt 模板
│       ├── parse_knowledge.md     # 知识点解析
│       ├── assessment.md          # 诊断评估出题
│       ├── quiz_gen.md            # 练习出题
│       ├── grade.md               # 批改
│       └── socratic.md            # 苏格拉底追问
│
├── tests/                         # 测试（8 文件）
│   ├── conftest.py                # fixtures: test_db, test_db_path
│   ├── test_knowledge_engine.py
│   ├── test_assessment.py
│   ├── test_quiz_engine.py
│   ├── test_plan.py
│   ├── test_socratic.py
│   ├── test_materials.py
│   ├── test_quizzes.py
│   └── test_dashboard.py
│
└── docs/
    ├── requirements.md            # 产品需求规格说明书
    ├── architecture.md            # 本文档
    └── plan.md                    # 实施计划（历史记录）
```

---

## 第 2 章 · 数据库设计

**文件：** `super_tutor/core/database.py`
**引擎：** SQLite + aiosqlite 异步 I/O，WAL 模式

### 2.1 表总览（6 表）

| 表名 | 用途 | 核心列 |
|------|------|--------|
| `materials` | 学习材料 | material_id, title, content, course_type, status |
| `knowledge_points` | 知识点（核心实体） | kp_id, prerequisite_ids, successor_ids, mastery_level |
| `questions` | 题库 | question_id, kp_id, type, stem, correct_answer |
| `quiz_attempts` | 作答记录 | attempt_id, student_id, question_id, kp_id, is_correct |
| `wrong_questions` | 错题本 | wrong_id, student_id, question_id, kp_id, resolution_status |
| `study_plans` | 学习计划 | plan_id, student_id, kp_sequence |

### 2.2 materials — 学习材料

```sql
CREATE TABLE IF NOT EXISTS materials (
    material_id  TEXT PRIMARY KEY,
    title        TEXT    NOT NULL,
    content      TEXT    NOT NULL DEFAULT '',
    course_type  TEXT    NOT NULL DEFAULT '',
    status       TEXT    NOT NULL DEFAULT 'draft',   -- draft / processing / ready / error
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL
);
```

CRUD：`create_material` / `get_material` / `update_material`

### 2.3 knowledge_points — 知识点（核心）

```sql
CREATE TABLE IF NOT EXISTS knowledge_points (
    kp_id            TEXT PRIMARY KEY,
    material_id      TEXT    NOT NULL,
    title            TEXT    NOT NULL,
    summary          TEXT    NOT NULL DEFAULT '',
    content          TEXT    NOT NULL,
    keywords         TEXT    NOT NULL DEFAULT '[]',         -- JSON array
    difficulty       TEXT    NOT NULL DEFAULT 'medium',
    course_type      TEXT    NOT NULL DEFAULT '',
    chapter_index    INTEGER NOT NULL DEFAULT 0,
    prerequisite_ids TEXT    NOT NULL DEFAULT '[]',         -- JSON array of kp_id
    successor_ids    TEXT    NOT NULL DEFAULT '[]',         -- JSON array of kp_id
    mastery_level    REAL    NOT NULL DEFAULT 0.0,
    assessment_count INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kp_material_id ON knowledge_points(material_id);
CREATE INDEX IF NOT EXISTS idx_kp_title        ON knowledge_points(title);
CREATE INDEX IF NOT EXISTS idx_kp_difficulty   ON knowledge_points(difficulty);
```

CRUD：`insert_knowledge_point` / `get_knowledge_point` / `list_knowledge_points_by_material` / `list_knowledge_points_with_mastery` / `update_knowledge_point` / `upsert_knowledge_point_mastery`

### 2.4 questions — 题库

```sql
CREATE TABLE IF NOT EXISTS questions (
    question_id        TEXT PRIMARY KEY,
    type               TEXT    NOT NULL,            -- multiple_choice / true_false / fill_in_blank / short_answer / essay / coding
    difficulty         TEXT    NOT NULL DEFAULT 'medium',
    subject            TEXT    NOT NULL DEFAULT '',
    topic              TEXT    NOT NULL DEFAULT '',
    stem               TEXT    NOT NULL,
    options            TEXT    NOT NULL DEFAULT '[]',  -- JSON
    correct_answer     TEXT    NOT NULL,
    explanation        TEXT    NOT NULL DEFAULT '',
    kp_id              TEXT    NOT NULL DEFAULT '',
    kp_context         TEXT    NOT NULL DEFAULT '',
    estimated_seconds  INTEGER NOT NULL DEFAULT 120,
    points             REAL    NOT NULL DEFAULT 1.0,
    tags               TEXT    NOT NULL DEFAULT '[]',  -- JSON
    metadata           TEXT    NOT NULL DEFAULT '{}',  -- JSON
    created_at         TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_questions_topic      ON questions(topic);
CREATE INDEX IF NOT EXISTS idx_questions_difficulty ON questions(difficulty);
CREATE INDEX IF NOT EXISTS idx_questions_type       ON questions(type);
CREATE INDEX IF NOT EXISTS idx_questions_kp_id      ON questions(kp_id);
```

CRUD：`insert_question`（INSERT OR REPLACE） / `get_question`

### 2.5 quiz_attempts — 作答记录

```sql
CREATE TABLE IF NOT EXISTS quiz_attempts (
    attempt_id        TEXT PRIMARY KEY,
    student_id        TEXT    NOT NULL DEFAULT '',
    question_id       TEXT    NOT NULL,
    kp_id             TEXT    NOT NULL DEFAULT '',
    student_answer    TEXT,
    is_correct        INTEGER,                      -- NULL=未批改, 0=错, 1=对
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
CREATE INDEX IF NOT EXISTS idx_attempts_student_id   ON quiz_attempts(student_id);
CREATE INDEX IF NOT EXISTS idx_attempts_question_id  ON quiz_attempts(question_id);
CREATE INDEX IF NOT EXISTS idx_attempts_is_correct   ON quiz_attempts(is_correct);
CREATE INDEX IF NOT EXISTS idx_attempts_kp_id        ON quiz_attempts(kp_id);
```

CRUD：`insert_attempt`（INSERT OR REPLACE） / `list_attempts_by_student`（支持 is_correct + kp_id 过滤 + 分页）

### 2.6 wrong_questions — 错题本

```sql
CREATE TABLE IF NOT EXISTS wrong_questions (
    wrong_id          TEXT PRIMARY KEY,
    student_id        TEXT    NOT NULL,
    question_id       TEXT    NOT NULL,
    kp_id             TEXT    NOT NULL DEFAULT '',
    wrong_answer      TEXT,
    correct_answer    TEXT    NOT NULL,
    attempt_count     INTEGER NOT NULL DEFAULT 1,
    resolution_status TEXT    NOT NULL DEFAULT 'unresolved',  -- unresolved / reviewing / resolved
    note              TEXT    NOT NULL DEFAULT '',
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wrong_student_id   ON wrong_questions(student_id);
CREATE INDEX IF NOT EXISTS idx_wrong_question_id  ON wrong_questions(question_id);
CREATE INDEX IF NOT EXISTS idx_wrong_kp_id        ON wrong_questions(kp_id);
CREATE INDEX IF NOT EXISTS idx_wrong_resolution   ON wrong_questions(resolution_status);
```

去重策略：同一 student_id + question_id → 递增 attempt_count，不新建行。

CRUD：`insert_wrong_question` / `get_wrong_question` / `get_wrong_question_by_student_and_question` / `list_wrong_questions_by_student`（支持 resolution_status 过滤 + 分页） / `update_wrong_question`

### 2.7 study_plans — 学习计划

```sql
CREATE TABLE IF NOT EXISTS study_plans (
    plan_id      TEXT PRIMARY KEY,
    student_id   TEXT    NOT NULL,
    title        TEXT    NOT NULL DEFAULT '',
    description  TEXT    NOT NULL DEFAULT '',
    goal         TEXT    NOT NULL DEFAULT '',
    start_date   TEXT    NOT NULL,
    end_date     TEXT,
    status       TEXT    NOT NULL DEFAULT 'active',
    kp_sequence  TEXT    NOT NULL DEFAULT '[]',       -- JSON: [{kp_id, title, order, priority_score, mastery, activity_type, estimated_minutes, scheduled_date, completed, ...}]
    metadata     TEXT    NOT NULL DEFAULT '{}',
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_study_plans_student_id ON study_plans(student_id);
```

CRUD：`create_study_plan` / `get_study_plan`

---

## 第 3 章 · 配置管理

**文件：** `super_tutor/config.py`

```python
@dataclass
class TutorConfig:
    api_key: str = ""
    api_base_url: str = "https://api.deepseek.com"
    db_path: str = "~/.super-tutor/super_tutor.db"
    model: str = "deepseek-chat"
```

加载优先级：**环境变量 > `~/.super-tutor/settings.json` > 默认值**

| 环境变量 | 映射字段 |
|---------|---------|
| `TUTOR_API_KEY` | api_key |
| `TUTOR_API_BASE_URL` | api_base_url |
| `TUTOR_DB_PATH` | db_path |
| `TUTOR_MODEL` | model |

`LLMClient` 额外从环境变量读取 `TUTOR_MAX_RETRIES`（默认 3）。

---

## 第 4 章 · 数据模型

### 4.1 枚举（`models/enums.py`）

```python
class DifficultyLevel(str, Enum):
    BEGINNER = "beginner"
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"
    EXPERT = "expert"

class QuestionType(str, Enum):
    MULTIPLE_CHOICE = "multiple_choice"
    TRUE_FALSE = "true_false"
    FILL_IN_BLANK = "fill_in_blank"
    SHORT_ANSWER = "short_answer"
    ESSAY = "essay"
    CODING = "coding"
```

### 4.2 Pydantic 模型

| 模型 | 文件 | 对应表 | 核心字段 |
|------|------|--------|---------|
| `KnowledgePoint` | knowledge.py | knowledge_points | kp_id, title, content, prerequisite_ids, successor_ids, mastery_level |
| `Question` | quiz.py | questions | question_id, type, stem, correct_answer, kp_id, hints |
| `QuizAttempt` | quiz.py | quiz_attempts | attempt_id, question_id, student_answer, is_correct, kp_id |
| `KPAssessmentResult` | assessment.py | —（内存） | kp_id, accuracy, initial_mastery, adjusted_mastery, status |
| `AssessmentReport` | assessment.py | —（内存） | kp_results, accuracy, weak_kps, strong_kps, rules_applied |
| `ReviewItem` | mastery.py | study_plans.kp_sequence | knowledge_node_id, scheduled_date, activity_type, estimated_minutes |
| `StudyPlan` | plan.py | study_plans | plan_id, kp_sequence, schedule: list[ReviewItem], progress |
| `SocraticTurn` | socratic.py | —（session_state） | turn_id, level, teacher_message, expected_concepts, resolved |

---

## 第 5 章 · LLM 客户端

**文件：** `super_tutor/core/llm_client.py`

```python
class LLMClient:
    def __init__(self):
        # 从环境变量读取: TUTOR_API_KEY, TUTOR_API_BASE_URL, TUTOR_MODEL, TUTOR_MAX_RETRIES
        self._client = AsyncOpenAI(api_key=..., base_url=...)

    async def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 4096,
        timeout: int = 120,
    ) -> str:
        # 3 次指数退避重试（1s → 2s → 4s）
        # 空内容 → 直接抛出 LLMError（不重试）
        # Timeout → 重试
```

重试策略：初始尝试 + 3 次重试，指数退避。`LLMError`（空内容）不重试，立即传播。

---

## 第 6 章 · 业务引擎层

5 个无状态 Engine，构造函数接收 `Database`，除 `PlanEngine` 外均需 `LLMClient`。

### 6.1 KnowledgeEngine — 知识点解析

```python
class KnowledgeEngine:
    def __init__(self, db: Database, llm_client: LLMClient, parse_prompt_path: str | None = None):
        ...

    async def parse(self, content: str, course_type: str, material_id: str) -> list[KnowledgePoint]:
        """1. 加载 parse_knowledge.md prompt
           2. 调用 LLM 拆分知识点
           3. 解析 JSON，生成 UUID
           4. 根据 prerequisite_indices 建立双向关系
           5. 批量写入 knowledge_points 表"""

    async def get_by_material(self, material_id: str) -> list[KnowledgePoint]: ...
    async def get_prerequisites(self, kp_id: str) -> list[KnowledgePoint]: ...
    async def get_successors(self, kp_id: str) -> list[KnowledgePoint]: ...
    async def update_mastery(self, kp_id: str, score: float) -> None: ...
```

### 6.2 AssessmentEngine — 诊断评估

```python
class AssessmentEngine:
    def __init__(self, db: Database, llm_client: LLMClient,
                 knowledge_engine: KnowledgeEngine | None = None,
                 quiz_engine: QuizEngine | None = None): ...

    async def generate(self, kp_ids: list[str], student_id: str = "",
                       question_count: int = 15) -> list[Question]:
        """1. 获取 KP 数据，拓扑排序
           2. 分配每 KP 题目数（min 1 each）
           3. 调用 LLM 生成诊断性题目（prompts/assessment.md）
           4. 解析 JSON 并持久化"""

    async def grade(self, questions: list[Question], student_answers: list[dict],
                    student_id: str = "") -> AssessmentReport:
        """1. 委托 QuizEngine 逐题批改
           2. 错题自动入库
           3. 按 KP 聚合正确率
           4. 应用 3 条前置规则
           5. 生成评估报告"""

    def apply_prerequisite_rules(self, report: AssessmentReport) -> None:
        """规则1: 置信度折扣 (×0.7)
           规则2: need_review 标记
           规则3: need_relearn + 掌握度折半"""
```

### 6.3 QuizEngine — 出题与批改

```python
class QuizEngine:
    def __init__(self, db: Database, llm_client: LLMClient,
                 knowledge_engine: KnowledgeEngine): ...

    async def generate_questions(self, kp_ids: list[str], count: int = 5,
                                  difficulty: str | None = None,
                                  types: list[str] | None = None) -> list[Question]:
        """1. 从 DB 获取 KP + 前驱摘要
           2. 构建上下文 prompt（prompts/quiz_gen.md）
           3. 调用 LLM 生成题目
           4. 解析 JSON 并持久化到 questions 表"""

    async def grade_answers(self, questions: list[Question],
                            student_answers: list[dict[str, Any]],
                            student_id: str = "") -> list[QuizAttempt]:
        """1. 分流：选择题/判断题 → 程序化批改（_grade_programmatic）
           2. 填空/简答/论述/编程 → LLM 批改（prompts/grade.md）
           3. 持久化 attempts"""

    async def add_to_wrong_book(self, attempt: QuizAttempt,
                                 question: Question | None = None) -> dict:
        """正确跳过；
           同(student, question)已存在 → 递增 attempt_count；
           否则 → 新建 wrong_questions 行"""
```

**程序化批改细节（`_grade_programmatic`）：**
- 选择题：`student_answer.strip().upper()` vs `correct_answer.strip().upper()`
- 判断题：支持 true/false/1/0/yes/no/对/错/正确/错误 → bool 比对

### 6.4 PlanEngine — 学习计划

```python
class PlanEngine:
    def __init__(self, db: Database): ...

    async def generate(self, kp_ids: list[str], mastery_map: dict[str, float],
                       student_id: str = "", plan_title: str = "",
                       plan_goal: str = "", start_date: str = "") -> StudyPlan:
        """1. 从 DB 获取 KP
           2. Kahn 拓扑排序
           3. 优先级: (1 - mastery) × (1 + successor_count / total_kps)
           4. 活动类型: learn_new (<0.3) / review (0.3-0.5) / practice (0.5-0.8) / quiz (≥0.8)
           5. 时长估算: base_difficulty × (0.5 + mastery_gap)，钳位 10-120 min
           6. 每日一个 KP 排期
           7. 持久化到 study_plans 表"""

    @staticmethod
    def topological_sort(kps: list[dict]) -> list[str]:
        """Kahn 算法，处理环（剩余节点追加末尾）"""
```

### 6.5 SocraticEngine — 苏格拉底追问

```python
class SocraticEngine:
    def __init__(self, db: Database, llm_client: LLMClient,
                 prompt_path: str | None = None): ...

    async def start_dialogue(self, kp_id: str, wrong_question_id: str) -> SocraticTurn:
        """1. 从 DB 获取 KP 内容和错题信息
           2. 构建 prompt（prompts/socratic.md）
           3. 始终从 L1_GUIDING 开始"""

    async def continue_dialogue(self, history: list[dict[str, Any]],
                                 user_response: str) -> SocraticTurn:
        """1. 检测"显示答案"请求 → 软确认后再展示
           2. 检测 ≥6 轮对话 → 强制 SHOW_ANSWER
           3. 调用 LLM 判断升级/降级/解决
           4. 返回下一轮 SocraticTurn"""
```

层级状态机：
```
L1_GUIDING → L2_HINTING → L3_NEAR_ANSWER → RESOLVED
     ↓            ↓              ↓
     └────────────┴──────────────┴──→ SHOW_ANSWER
```

对话状态仅存 `st.session_state`，不持久化。

---

## 第 7 章 · 前端架构

**文件：** `app.py`
**框架：** Streamlit 单页应用，`layout="wide"`

### 7.1 session_state 键（20 个）

```python
_S_DB = "tutor_db"                          # Database 实例
_S_LLM = "tutor_llm"                        # LLMClient 实例
_S_ENGINE = "tutor_engine"                  # KnowledgeEngine 实例
_S_KPS = "tutor_knowledge_points"           # 解析后的知识点列表
_S_MATERIAL_ID = "tutor_material_id"        # 当前材料 ID
_S_PARSE_ERROR = "tutor_parse_error"        # 解析错误信息
_S_QUIZ_ENGINE = "tutor_quiz_engine"        # QuizEngine 实例
_S_QUIZ_MODE = "tutor_quiz_mode"            # 是否进入答题模式
_S_QUESTIONS = "tutor_questions"            # 当前题目列表
_S_ATTEMPTS = "tutor_attempts"              # 批改结果
_S_QUIZ_SUBMITTED = "tutor_quiz_submitted"  # 是否已提交
_S_ASSESSMENT_ENGINE = "tutor_assessment_engine"
_S_ASSESSMENT_QUESTIONS = "tutor_assessment_questions"
_S_ASSESSMENT_REPORT = "tutor_assessment_report"
_S_ASSESSMENT_SUBMITTED = "tutor_assessment_submitted"
_S_PLAN = "tutor_plan"                      # 当前学习计划
_S_PLAN_ACTIVE_KP = "tutor_plan_active_kp"  # 计划中正在学习的 KP
_S_SOCRATIC_ENGINE = "tutor_socratic_engine"
_S_SOCRATIC_ACTIVE = "tutor_socratic_active"
_S_SOCRATIC_HISTORY = "tutor_socratic_history"
_S_SOCRATIC_TURN = "tutor_socratic_turn"
```

### 7.2 页面标签流

```
📥 导入教材（上传 PDF / 粘贴文本）
    ↓ 解析后
📋 知识点列表（st.dataframe）
    ↓ 确认
[Tab: 📝 练习答题 | 📖 错题本 | 🔬 诊断评估 | 📅 学习计划]
```

### 7.3 习题渲染映射

| 题型 | Widget | key 前缀 |
|------|--------|---------|
| multiple_choice | `st.radio` | quiz_q_{n} / assess_q_{n} |
| true_false | `st.radio(["对","错"])` | quiz_q_{n} / assess_q_{n} |
| fill_in_blank | `st.text_input` | quiz_q_{n} / assess_q_{n} |
| short_answer | `st.text_area` (100px) | quiz_q_{n} / assess_q_{n} |
| essay | `st.text_area` (200px) | quiz_q_{n} / assess_q_{n} |
| coding | `st.text_area` (150px) | quiz_q_{n} / assess_q_{n} |

### 7.4 异步处理

Streamlit 不支持原生 async，使用 `_run_async()` 包装：
- 优先 `asyncio.run()`
- 若已有 event loop → 尝试 `nest_asyncio.apply()` + `run_until_complete()`

---

## 第 8 章 · Prompt 模板

| 文件 | 调用者 | Temperature | Timeout |
|------|--------|------------|---------|
| `parse_knowledge.md` | KnowledgeEngine.parse() | 0.3 | 180s |
| `assessment.md` | AssessmentEngine.generate() | 0.7 | 180s |
| `quiz_gen.md` | QuizEngine.generate_questions() | 0.7 | 180s |
| `grade.md` | QuizEngine.grade_answers() | 0.1 | 120s |
| `socratic.md` | SocraticEngine | 0.7 | 120s |

所有 Prompt 使用 `{variable}` 模板变量，运行时由 Engine 填充。LLM 返回 JSON 均经过 Markdown 代码围栏剥离（`_strip_markdown_fence`）。

---

## 第 9 章 · 异常体系

**文件：** `super_tutor/core/exceptions.py`

```
TutorError (基类)
├── LLMError        — LLM 调用失败（超时、空内容、重试耗尽）
└── MaterialError   — 教材解析失败（JSON 非法、空结果、prompt 加载失败）
```

---

## 第 10 章 · 启动方式

```bash
cd super-tutor-agent
pip install -r requirements.txt
export TUTOR_API_KEY="sk-your-key"
streamlit run app.py
# → http://localhost:8501
```

### requirements.txt

```
openai>=1.0.0          # LLM API 客户端
aiosqlite>=0.20.0      # 异步 SQLite
PyPDF2>=3.0.0          # PDF 文本提取
pydantic>=2.0.0        # 数据校验
streamlit>=1.35        # Web 前端
pytest>=8.0.0          # 测试
pytest-asyncio>=0.24.0 # 异步测试支持
```

---

## 附录 · 代码量统计

| 模块 | 行数 | 说明 |
|------|------|------|
| `app.py` | ~1,970 | Streamlit 前端 |
| `core/database.py` | ~900 | 6 表 DDL + CRUD |
| `core/llm_client.py` | ~125 | OpenAI SDK 封装 + 重试 |
| `core/exceptions.py` | ~40 | 3 个异常类 |
| `config.py` | ~90 | 4 字段配置 |
| `engine/` (5 文件) | ~2,100 | 5 个无状态 Engine |
| `models/` (7 文件) | ~580 | Pydantic 模型 + 枚举 |
| `prompts/` (5 文件) | ~250 | Prompt 模板 |
| `tests/` (9 文件) | ~1,500 | 测试 |
| **总计** | **~7,555** | |
