# 🎓 超级私教 (Super Tutor)

<br>

> **上传资料 → 提取知识点 → 定位薄弱项 → 精准练习 → 错题追问 —— 一个完整的自学闭环。**
>
> 基于 LLM 的深度学习辅助工具，围绕知识点构建从诊断到复习的完整学习链路。

[![Python](https://img.shields.io/badge/Python-3.11+-blue)](https://www.python.org/)
[![Streamlit](https://img.shields.io/badge/Streamlit-1.35+-red)](https://streamlit.io/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-green)](https://fastapi.tiangolo.com/)
[![License](https://img.shields.io/badge/License-MIT-yellow)](LICENSE)
[![Status](https://img.shields.io/badge/Status-v3.0-brightgreen)]()
[![Tests](https://img.shields.io/badge/Tests-148/148%20passed-brightgreen)]()

---

## 做了什么

你扔给它一本教材 PDF，它自动完成：

```
📄 上传资料           🔬 诊断评估           ✏️ 练习答题           📕 错题本
┌──────────┐         ┌──────────┐         ┌──────────┐         ┌──────────┐
│ PDF 上传  │  ───→  │ 全知识点  │  ───→  │ 6 种题型  │  ───→  │ 按知识点  │
│ 文本粘贴  │        │ 摸底测验  │        │ 自动批改  │        │ 分组展示  │
│ LLM 解析  │        │ 前后联动  │        │ 错题入库  │        │ 苏格拉底  │
│ 依赖识别  │        │ 掌握度图  │        │ 详细解析  │        │ 式追问    │
└──────────┘         └──────────┘         └──────────┘         └──────────┘
```

### 核心功能

| 功能 | 说明 |
|------|------|
| 📚 资料上传 | PDF / 文本上传，LLM 自动提取知识点并识别前驱→后继依赖关系 |
| 🔬 诊断评估 | 全知识点覆盖的摸底测验，3 条前置依赖规则精确定位薄弱环节 |
| 📋 学习计划 | 拓扑排序 + 优先级公式，薄弱点多排、已掌握少排 |
| ✏️ 练习答题 | 6 种题型（选择/判断/填空/简答/论述/编程），注入前驱知识点上下文 |
| 📕 错题本 | 按知识点分组，支持查看解析、重新作答、苏格拉底式追问 |
| 🗨 苏格拉底追问 | L1→L2→L3 三层递进引导，不直接给答案，引导自主发现 |

---

## 技术架构

```
app.py (Streamlit 前端)
    │
    ├── engine/
    │   ├── knowledge_engine.py    # 知识点解析 + 前后依赖
    │   ├── assessment_engine.py   # 诊断评估 + 前置规则
    │   ├── quiz_engine.py         # 出题 + 程序/LLM混合批改
    │   ├── plan_engine.py         # 拓扑排序 + 学习计划
    │   └── socratic_engine.py     # 苏格拉底追问 (L1→L2→L3)
    │
    ├── core/
    │   ├── database.py            # SQLite 6 表
    │   ├── llm_client.py          # DeepSeek API
    │   └── exceptions.py          # 异常定义
    │
    └── models/                    # Pydantic 数据模型
```

- **前端**: Streamlit 单页应用，一个 `streamlit run` 启动
- **后端**: FastAPI（可选 API 扩展）
- **LLM**: DeepSeek API
- **数据库**: SQLite 6 表（materials / knowledge_points / questions / quiz_attempts / wrong_questions / study_plans）
- **无状态机**: 页面按钮驱动用户流程，去掉旧 Orchestrator 状态机 (~2,200 行)

---

## 快速开始

### 前置要求

- Python 3.11+
- DeepSeek API Key

### 安装

```bash
pip install -r requirements.txt
```

### 配置

通过环境变量设置 API Key：

```bash
# Windows PowerShell
$env:TUTOR_API_KEY="sk-your-key-here"
$env:TUTOR_API_BASE_URL="https://api.deepseek.com"

# Linux / macOS
export TUTOR_API_KEY="sk-your-key-here"
export TUTOR_API_BASE_URL="https://api.deepseek.com"
```

### 启动

```bash
streamlit run app.py
# → http://localhost:8501
```

或双击 `run.bat`（Windows）。

---

## 测试

```bash
python -m pytest tests/ -v
# 148 passed
```

---

## 项目结构

```
super-tutor-agent/
├── app.py                         # Streamlit 主入口
├── requirements.txt               # Python 依赖
├── run.bat                        # Windows 一键启动
├── README.md
│
├── super_tutor/
│   ├── config.py                  # 配置管理
│   ├── main.py                    # FastAPI 入口（可选）
│   ├── core/
│   │   ├── database.py            # 数据库层（6 表）
│   │   ├── llm_client.py          # LLM 客户端
│   │   └── exceptions.py          # 异常类
│   ├── engine/                    # 业务引擎层
│   │   ├── knowledge_engine.py
│   │   ├── assessment_engine.py
│   │   ├── quiz_engine.py
│   │   ├── plan_engine.py
│   │   └── socratic_engine.py
│   ├── models/                    # Pydantic 数据模型
│   │   ├── enums.py
│   │   ├── knowledge.py
│   │   ├── quiz.py
│   │   ├── assessment.py
│   │   ├── plan.py
│   │   └── socratic.py
│   ├── routes/                    # FastAPI 路由
│   └── prompts/                   # LLM Prompt 模板
│       ├── parse_knowledge.md
│       ├── assessment.md
│       ├── quiz_gen.md
│       ├── grade.md
│       └── socratic.md
│
├── tests/                         # 148 个测试
│   ├── conftest.py
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
    ├── architecture.md            # 技术架构文档
    └── plan.md                    # 实施计划
```

---

## 文档

| 文档 | 说明 |
|------|------|
| [requirements.md](docs/requirements.md) | 产品需求规格说明书：功能需求、非功能需求、风险登记册 |
| [architecture.md](docs/architecture.md) | 技术架构文档：数据库设计、引擎设计、API 规范 |
| [plan.md](docs/plan.md) | v3.0 实施计划 |

---

## License

MIT © 2026 xiangyuli494-cmyk
