# Super Tutor — 产品需求规格说明书

**版本：** v3.2
**状态：** 已实现
**最后更新：** 2026-06-26

---

## 术语对照

| 缩写 | 全称 | 说明 |
|------|------|------|
| KP | Knowledge Point | 知识点，系统的最小学习单元 |
| LLM | Large Language Model | 大语言模型（DeepSeek） |
| DAG | Directed Acyclic Graph | 有向无环图，描述知识点的前驱/后继依赖 |

---

## 第 1 章 · 产品概述

### 1.1 产品定位

Super Tutor 是一个**基于 LLM 的智能教学系统**。用户上传教材 → 系统自动提取知识点 → 诊断评估 → 个性化学习计划 → 多题型练习 → 错题追问，形成完整的自学闭环。

### 1.2 目标用户

- **大学生**：期末备考，需系统化复习教材
- **自学者**：想系统学一门新领域，需要结构化理解和测验

### 1.3 设计原则

1. **单页应用**：一个 `streamlit run app.py` 启动全部功能
2. **知识点为核心**：所有功能围绕 KP 展开
3. **按钮驱动**：无状态机，页面按钮串联用户流程
4. **前驱联动**：KP 间的前驱/后继关系贯穿评估和出题
5. **错题即入口**：错题本不仅记录，更是苏格拉底追问的起点

### 1.4 技术栈

| 层 | 技术 |
|----|------|
| 前端 | Streamlit 1.35+ |
| 业务逻辑 | 5 个无状态 Engine |
| 数据库 | SQLite（6 表），aiosqlite 异步 I/O |
| LLM | DeepSeek API（OpenAI SDK 兼容接口） |
| PDF 解析 | PyPDF2 |
| 数据模型 | Pydantic 2.0+ |

---

## 第 2 章 · 功能需求

### 整体流程

```
上传教材 → AI 提取知识点 → 诊断评估 → 学习计划 → 练习答题 → 错题本 → 苏格拉底追问
```

### F1 · 课程类型选择

10 种预设课程类型：`physics`, `mathematics`, `chemistry`, `biology`, `computer_science`, `history`, `literature`, `english`, `economics`, `other`。

通过 `st.selectbox` 选择，影响后续 LLM 解析策略。

### F2 · 教材上传与知识点解析

**输入方式：**
- PDF 上传（PyPDF2 提取纯文本）
- 文本直接粘贴（支持 Markdown）

**解析流程（KnowledgeEngine.parse）：**
1. 加载 prompt 模板 `prompts/parse_knowledge.md`
2. 调用 LLM 将内容拆分为知识点列表
3. 每个 KP 包含：title、summary（≤256 字符）、content、keywords、difficulty（5 级）、prerequisite_indices
4. 解析 LLM 返回的 JSON，为每个 KP 生成 UUID
5. 根据 `prerequisite_indices` 建立双向关系（prerequisite_ids ↔ successor_ids）
6. 批量写入 `knowledge_points` 表

**知识点难度等级（DifficultyLevel）：** beginner | easy | medium | hard | expert

**前端展示：** `st.dataframe` 表格，含主题、摘要、难度、关键词、前驱/后继关系。

### F3 · 诊断评估

**流程（AssessmentEngine）：**
1. **出题（generate）**：对所有 KP 按拓扑排序（Kahn 算法），生成 15-30 道诊断性题目
   - 每 KP 至少 1 题，前驱 KP 先于后继 KP
   - Prompt：`prompts/assessment.md`
2. **批改（grade）**：逐题批改，按 KP 聚合正确率
3. **前置规则校准（apply_prerequisite_rules）** — 三条规则：

| 规则 | 触发条件 | 效果 |
|------|---------|------|
| 规则1 — 置信度折扣 | 前驱 KP 掌握度 ≤ 0.5 | 后继 KP 置信度 × 0.7，掌握度重新计算 |
| 规则2 — 需复习 | 后继正确（≥0.6）但前驱错误（<0.5） | 标记前驱为 `need_review` |
| 规则3 — 需重学 | ≥3 个直接后继全部答错 | 标记前驱为 `need_relearn`，掌握度折半 |

4. **掌握度状态分类**：mastered（≥0.8）、learning（0.5-0.8）、need_review、need_relearn（≤0.3 或触发规则）

**输出：** `AssessmentReport`，含总体正确率、薄弱/强项 KP 列表、掌握度分布、建议学习顺序（拓扑排序）。

### F4 · 学习计划

**流程（PlanEngine.generate）：**
1. 从 DB 获取 KP，Kahn 拓扑排序
2. 优先级公式：`(1 - mastery) × (1 + successor_count / total_kps)`
3. 根据掌握度分配活动类型：

| 掌握度 | 活动类型 |
|--------|---------|
| < 0.3 | learn_new（新学） |
| 0.3–0.5 | review（复习） |
| 0.5–0.8 | practice（练习） |
| ≥ 0.8 | quiz（测验） |

4. 根据难度和掌握度缺口估算学习时长（10–120 分钟/KP）
5. 每日排期一个 KP（`start_date + index` 天）

**输出：** `StudyPlan`，含拓扑排序的 KP 序列和 ReviewItem 排期条目，持久化到 `study_plans` 表。

### F5 · 练习答题

**出题（QuizEngine.generate_questions）：**
- 用户选择 KP（多选）、题目数量（1–20）、难度（自动/指定）、题型（多选，留空=全部）
- 出题时向 LLM 注入前驱 KP 摘要作为上下文
- Prompt：`prompts/quiz_gen.md`

**6 种题型（QuestionType）：**

| 题型 | 前端渲染 | 批改方式 |
|------|---------|---------|
| multiple_choice | `st.radio` | 程序化比对 |
| true_false | `st.radio(["对","错"])` | 程序化比对 |
| fill_in_blank | `st.text_input` | LLM 语义匹配 |
| short_answer | `st.text_area` (100px) | LLM 采分点评分 |
| essay | `st.text_area` (200px) | LLM 多维度评分 |
| coding | `st.text_area` (150px) | LLM 测试用例判定 |

**批改（QuizEngine.grade_answers）：**
- 选择题/判断题：程序化比对（支持中英文、大小写规范化）
- 其他题型：LLM 批改（Prompt: `prompts/grade.md`）
- 错题自动入库（`add_to_wrong_book`）

### F6 · 错题本

**数据来源：** `wrong_questions` 表，由 QuizEngine 和 AssessmentEngine 自动写入。

**功能：**
- 按 KP 分组展示（`st.expander` 折叠面板）
- 每道错题显示：题干、错误答案、正确答案、解析、犯错次数、收录时间
- 筛选：按知识点、按时间（全部/最近7天/最近30天）
- 重新作答：为该 KP 生成 3 道新题
- 苏格拉底追问入口

**去重逻辑：** 同一学生同一题多次错 → 递增 `attempt_count`，不重复建行。

### F7 · 苏格拉底追问

**入口：** 错题本中每道错题的 "🗨 苏格拉底追问" 按钮。

**层级（SocraticEngine）：**

| 层级 | 策略 | 说明 |
|------|------|------|
| L1_GUIDING | 笼统引导 | 开放性问题，如"这道题考察了什么概念？" |
| L2_HINTING | 具体提示 | 方向性暗示，缩小思考范围 |
| L3_NEAR_ANSWER | 接近答案 | 给出推理步骤，只留最后一步 |
| RESOLVED | 已解决 | 学生展示正确理解，给予肯定并结束 |
| SHOW_ANSWER | 显示答案 | 学生明确求助或达到最大轮数（6 轮） |

**对话状态：** 仅保存在 `st.session_state`，不持久化到数据库。

**UI：** `st.chat_message` + `st.text_area` 对话界面，支持"我知道了 ✅"和"显示答案"退出。

---

## 第 3 章 · 非功能需求

### 性能指标

| 指标 | 目标 |
|------|------|
| LLM 调用超时（parse/generate） | 180s |
| LLM 调用超时（grade/socratic） | 120s |
| 重试策略 | 3 次指数退避（1s→2s→4s） |
| 数据库模式 | WAL 模式 |
| 程序化批改响应 | 即时（无 LLM 调用） |

### 可用性

- 所有 LLM 调用有 `st.spinner` 加载状态
- 超时/失败有友好错误提示
- API Key 缺失时禁用 LLM 功能并显示警告
- 支持通过环境变量或 `~/.super-tutor/settings.json` 配置

### 数据安全

- API Key 通过环境变量 `TUTOR_API_KEY` 注入
- 前端不直接访问 LLM API（通过 Engine 层代理）
- 数据库文件存储在用户目录下

---

## 附录 · 配置项

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `TUTOR_API_KEY` | — | DeepSeek API Key（必填） |
| `TUTOR_API_BASE_URL` | `https://api.deepseek.com` | API 基础 URL |
| `TUTOR_MODEL` | `deepseek-chat` | 模型名称 |
| `TUTOR_DB_PATH` | `~/.super-tutor/super_tutor.db` | 数据库路径 |
| `TUTOR_MAX_RETRIES` | 3 | LLM 调用最大重试次数 |
