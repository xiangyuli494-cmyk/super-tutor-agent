# 超级私教 (Super Tutor) — 技术架构文档

**文档编号：** STA-ARCH-2026-001
**版本：** v1.1
**状态：** 与代码同步
**密级：** 内部

---

## 第 1 章 · 系统架构

### 1.1 技术选型

| 层级 | 技术 | 版本要求 | 选型理由 |
|------|------|---------|---------|
| **后端框架** | FastAPI | ≥ 0.115 | 异步支持好、自动 OpenAPI 文档、生态成熟 |
| **ASGI 服务器** | Uvicorn | ≥ 0.30 | FastAPI 官方推荐 |
| **LLM 接入** | OpenAI SDK (兼容模式) | ≥ 1.0 | 兼容 DeepSeek API，社区成熟 |
| **关系数据库** | SQLite + aiosqlite | ≥ 0.20 | 零配置嵌入式，个人用户场景无需 PostgreSQL |
| **向量检索** | sqlite-vec | ≥ 0.1 | SQLite 原生向量扩展，零运维 |
| **PDF 解析** | PyMuPDF | ≥ 1.24 | 文本提取精度最高，支持页码索引 |
| **数据校验** | Pydantic | ≥ 2.0 | FastAPI 原生集成 |
| **前端框架** | React + TypeScript | 18.x | 生态最丰富 |
| **样式方案** | Tailwind CSS | 3.x | 快速 UI 开发 |
| **状态管理** | Zustand | 4.x | 轻量、无 boilerplate |
| **测试** | pytest + pytest-asyncio | ≥ 8.0 | Python 标准测试框架 |

### 1.2 系统架构图

```
┌──────────────────────────────────────────────────────────────┐
│                   React 前端 (SPA)                           │
│  仪表盘  │  资料上传  │  答题页  │  复习计划  │  错题本      │
└─────────────────────────┬────────────────────────────────────┘
                          │ HTTP REST (JSON)
┌─────────────────────────▼────────────────────────────────────┐
│               FastAPI 后端引擎                                │
│                                                               │
│  ┌──────────────────────────────────────────────────────┐    │
│  │            Orchestrator 流水线引擎                     │    │
│  │   IDLE → PARSING → QUIZ_GEN → EVALUATING → PLANNING  │    │
│  └───────┬───────────────┬────────────────┬─────────────┘    │
│          │               │                │                   │
│  ┌───────▼──────┐ ┌──────▼──────┐ ┌──────▼──────┐           │
│  │  Tutor 角色  │ │ Assistant   │ │ Evaluator   │           │
│  │  (解析+规划)  │ │ 角色 (出题)  │ │ 角色 (批改)  │           │
│  └───────┬──────┘ └──────┬──────┘ └──────┬──────┘           │
│          │               │               │                   │
│  ┌───────▼───────────────▼───────────────▼──────┐           │
│  │  Core Layer: LLMClient / Database / RoleMgr  │           │
│  └──────────────────┬───────────────────────────┘           │
│                     │                                        │
│  ┌──────────────────▼──────────────────────────┐            │
│  │  SQLite + sqlite-vec (向量检索 + CRUD)      │            │
│  └─────────────────────────────────────────────┘            │
└──────────────────────────────────────────────────────────────┘
```

### 1.3 部署拓扑

本项目为**单机单用户**架构，不依赖外部服务：

```
User Browser (localhost:5173)
       │
       ▼
FastAPI Server (127.0.0.1:8765)
       │
       ├── SQLite DB (local .db file)
       ├── DeepSeek API (external, via HTTPS)
       └── Uploaded PDFs (local filesystem)
```

---

## 第 2 章 · 项目结构

```
super-tutor-agent/
├── super_tutor/           # Python 后端 (8,000+ 行)
│   ├── __init__.py        # 版本号声明 (v0.3.0)
│   ├── main.py            # FastAPI 入口 + lifespan
│   ├── config.py          # 配置管理（settings.json + env）
│   ├── core/
│   │   ├── orchestrator.py           # LLM 调用+序列化+状态机 (837 行)
│   │   ├── orchestrator_phases.py    # 四阶段 Mixin 实现 (751 行)
│   │   ├── orchestrator_prompts.py   # LLM Prompt 构建函数 (182 行)
│   │   ├── orchestrator_utils.py     # JSON 解析/模型水合/图谱 (257 行)
│   │   ├── database.py    # SQLite + sqlite-vec (14 表, 2,000 行)
│   │   ├── llm_client.py  # DeepSeek API 封装 + CLI 回退
│   │   ├── role_manager.py # 角色系统提示词加载
│   │   ├── token_tracker.py # Token 预算管控
│   │   ├── cli_backend.py  # Claude CLI 回退后端
│   │   └── exceptions.py   # 自定义异常体系
│   ├── models/            # Pydantic 模型 + 枚举 (8 文件)
│   ├── routes/            # FastAPI 路由层 (6 文件)
│   └── prompts/           # AI 角色系统提示词 (.md)
├── frontend/              # React SPA (22 文件)
│   └── src/
│       ├── api/           # API 客户端 + TypeScript 类型
│       ├── components/    # 可复用组件 (6 个)
│       ├── pages/         # 页面组件 (5 个)
│       └── store/         # Zustand 状态管理 (2 个)
├── tests/                 # pytest 测试套件 (5 文件, 18 用例)
├── docs/
│   ├── requirements.md    # 产品需求规格说明书
│   └── architecture.md    # 本文件
└── requirements.txt
```

---

## 第 3 章 · 数据模型

### 3.1 实体关系图

```
Material (1) ────< (N) KnowledgeChunk ────< (N) KnowledgeNode
                                                 │
                                          (N) ───┼─── (N)
                                                 │
                                          KnowledgeEdge
                                                 │
Question (N) >──── (N) KnowledgeNode             │
   │                                              │
   │ (1)                                         │
   ▼                                             │
QuizSession (1) ──< (N) QuizAttempt ────> MisconceptionTag
                          │
                          │ (1)
                          ▼
                   MasteryRecord ──> KnowledgeNode (1)
                          │
                          │ (N)
                          ▼
                      StudyPlan (1) ──< (N) ReviewItem
                          │
                          ▼
                   StudentProfile (1)
```

### 3.2 核心模型摘要

> 完整定义见 `super_tutor/models/` 下的 Pydantic 模型文件。

#### Material（学习材料）

| 字段 | 类型 | 必填 | 说明 |
|------|------|:---:|------|
| `material_id` | UUID | 是 | 主键 |
| `title` | str(256) | 是 | 材料标题 |
| `subject` | str | 否 | 学科 |
| `source_type` | enum | 是 | pdf_upload / url / manual |
| `total_pages` | int | 否 | 总页数 |

#### KnowledgeChunk（知识片段）

| 字段 | 类型 | 必填 | 说明 |
|------|------|:---:|------|
| `chunk_id` | UUID | 是 | 主键 |
| `material_id` | FK | 是 | 外键 → Material |
| `content` | str | 是 | 原文 |
| `summary_256` | str(256) | 是 | 摘要（用于向量化索引） |
| `topic` | str | 否 | 主题标签 |
| `difficulty` | enum | 否 | beginner/easy/medium/hard/expert |
| `keywords` | list[str] | 否 | 检索关键词 |

#### Question（题目）

| 字段 | 类型 | 必填 | 说明 |
|------|------|:---:|------|
| `question_id` | UUID | 是 | 主键 |
| `type` | enum | 是 | 7 种题型 |
| `stem` | str | 是 | 题干（Markdown） |
| `options` | list[dict] | 否 | 选项（选择题/匹配题） |
| `correct_answer` | any | 是 | 正确答案 |
| `explanation` | str | 是 | 详细解析 |
| `hints` | list[str] | 否 | 渐进式提示 |
| `difficulty` | enum | 是 | 难度等级 |
| `knowledge_node_ids` | list[UUID] | 否 | 考查的知识节点 |
| `points` | float | 否 | 分值（默认 1.0） |
| `estimated_seconds` | int | 否 | 预计耗时（默认 120s） |

#### QuizAttempt（作答记录）

| 字段 | 类型 | 必填 | 说明 |
|------|------|:---:|------|
| `attempt_id` | UUID | 是 | 主键 |
| `session_id` | FK | 是 | 外键 → QuizSession |
| `question_id` | FK | 是 | 外键 → Question |
| `student_answer` | any | 是 | 学生提交的答案 |
| `is_correct` | bool | 否 | 批改结果 |
| `score` | float | 否 | 得分 |
| `misconception_ids` | list[UUID] | 否 | 诊断出的错误概念 |

#### MisconceptionTag（迷思概念标签）

| 字段 | 类型 | 必填 | 说明 |
|------|------|:---:|------|
| `tag_id` | UUID | 是 | 主键 |
| `label` | str(128) | 是 | 标签名 |
| `category` | enum | 是 | 7 种错误类别 |
| `severity` | enum | 否 | minor / moderate / critical |
| `remediation_hint` | str | 是 | 补救建议 |

#### MasteryRecord（掌握度记录 / 认知孪生）

| 字段 | 类型 | 必填 | 说明 |
|------|------|:---:|------|
| `record_id` | UUID | 是 | 主键 |
| `student_id` | str | 是 | 学生标识 |
| `knowledge_node_id` | FK | 是 | 外键 → KnowledgeNode |
| `mastery_level` | float(0-1) | 是 | EMA 平滑后的掌握度 |
| `total_attempts` | int | 否 | 总作答次数 |
| `correct_attempts` | int | 否 | 正确次数 |
| `streak` | int | 否 | 连续正确次数 |
| `sm2_repetitions` | int | 否 | SM-2 成功记忆次数 |
| `sm2_ease_factor` | float | 否 | SM-2 EF（默认 2.5，下限 1.3） |
| `sm2_interval_days` | int | 否 | 当前复习间隔 |
| `sm2_next_review` | date | 否 | 下次复习日期 |
| `state` | enum | 否 | new / learning / reviewing / mastered / stagnated |

#### SocraticHint（苏格拉底提示）

| 字段 | 类型 | 必填 | 说明 |
|------|------|:---:|------|
| `hint_id` | UUID | 是 | 主键 |
| `question_id` | FK | 是 | 外键 → Question |
| `level` | int(1-3) | 是 | 1=笼统 / 2=方向 / 3=接近答案 |
| `content` | str | 是 | 提示正文 |
| `trigger_after_failures` | int | 否 | 累计答错 N 次后触发 |
| `difficulty_adapt` | bool | 否 | 是否根据掌握度自适应跳过 |

---

## 第 4 章 · API 接口规范

### 4.1 标准响应格式

```json
{
  "code": 0,
  "message": "ok",
  "data": { }
}
```

### 4.2 错误码定义

| 错误码 | HTTP 状态 | 含义 | 可重试 |
|--------|----------|------|:---:|
| `0` | 200 | 成功 | — |
| `1001` | 400 | 请求参数校验失败 | 否 |
| `2001` | 502 | LLM API 调用失败（超时 / 网络异常） | 是 |
| `2002` | 503 | LLM API 返回空内容 | 是 |
| `2003` | 422 | LLM JSON 输出解析失败 | 是 |
| `4001` | 400 | PDF 无可提取文本（扫描件） | 否 |
| `4002` | 502 | AI 服务暂时不可用 | 是 |
| `4003` | 422 | AI 输出格式异常 | 是 |
| `4004` | 400 | PDF 过大（> 50MB 或 > 200 页） | 否 |
| `4005` | 400 | 知识库为空，请先上传材料 | 否 |
| `5001` | 500 | 数据库异常 | 否 |
| `5002` | 500 | 内部未知错误 | 否 |

### 4.3 API 端点清单

#### 资料管理

| 方法 | 路径 | 说明 | MVP |
|:-----|------|------|:---:|
| `POST` | `/api/v1/materials/upload` | 上传文本材料 | ✅ |
| `POST` | `/api/v1/materials/upload/file` | 上传 PDF 文件 | ✅ |
| `GET` | `/api/v1/materials/{id}/status` | 获取材料状态 | ✅ |

#### 测验会话

| 方法 | 路径 | 说明 | MVP |
|:-----|------|------|:---:|
| `POST` | `/api/v1/sessions` | 创建测验会话 | ✅ |
| `GET` | `/api/v1/sessions/{id}/questions` | 获取题目列表（不含答案） | ✅ |
| `POST` | `/api/v1/sessions/{id}/answers` | 提交作答，触发批改+诊断 | ✅ |
| `GET` | `/api/v1/sessions/{id}/results` | 获取批改结果 | ✅ |
| `POST` | `/api/v1/sessions/{id}/plan` | 生成 SM-2 排期计划 | ✅ |

#### 仪表盘

| 方法 | 路径 | 说明 | MVP |
|:-----|------|------|:---:|
| `GET` | `/api/v1/students/{id}/dashboard` | 学习概览数据 | ✅ |
| `GET` | `/api/v1/students/{id}/mastery` | 掌握度明细 | ✅ |
| `GET` | `/api/v1/students/{id}/wrong-questions` | 错题本数据 | ✅ |
| `GET` | `/api/v1/students/{id}/plan/today` | 今日复习清单 | ✅ |

#### 系统

| 方法 | 路径 | 说明 | MVP |
|:-----|------|------|:---:|
| `GET` | `/api/v1/tokens/stats` | Token 用量统计 | ✅ |
| `GET` | `/api/v1/health` | 健康检查 | ✅ |

---

## 第 5 章 · 流水线工作流

### 5.1 阶段定义（PipelinePhase 枚举）

| 阶段 | 含义 | 负责角色 | 
|------|------|-----------|
| `IDLE` | 空闲，等待用户触发 | — |
| `PARSING` | 解析 PDF → 切片 → 向量化 | Tutor |
| `QUIZ_GEN` | 基于知识库生成题目 | Assistant |
| `EVALUATING` | 批改作答 + 迷思概念诊断 | Evaluator |
| `PLANNING` | 综合数据生成 SM-2 排期计划 | Tutor |

暂停（`_paused: bool`）和错误（`_error_message: str | None`）不再是独立的枚举值，而是 Orchestrator 的实例字段。

### 5.2 阶段流转图

```
IDLE ──start()──▶ PARSING ──proceed()──▶ QUIZ_GEN
                                            │
                              submit_answers() + proceed()
                                            │
                                            ▼
                                      EVALUATING ──proceed()──▶ PLANNING

暂停: pause() → _paused=True  → resume() → _paused=False
错误: 异常 → _error_message 设置 → retry_step() (最多 3 次)
```

### 5.3 角色与 Prompt 映射

| 角色 | RoleManager Key | Prompt 文件 | LLM 档位 | 职责 |
|------|----------------|-------------|---------|------|
| Tutor（主导师） | `tutor` | `prompts/tutor.md` | heavy（解析）/ medium（规划） | PDF 解析 + 排期计划 |
| Assistant（助教） | `assistant` | `prompts/assistant.md` | heavy | 检索知识库 + 出题 |
| Evaluator（评估者） | `evaluator` | `prompts/evaluator.md` | medium | 批改 + 迷思概念诊断 |

---

## 第 6 章 · SM-2 算法规格

| 参数 | 初始值 | 范围 | 说明 |
|------|--------|------|------|
| EF (Ease Factor) | 2.5 | [1.3, +∞) | 难易度因子，越高表示越容易记住 |
| 首次通过间隔 | 1 天 | — | 连续正确 1 次后隔 1 天复习 |
| 二次通过间隔 | 6 天 | — | 连续正确 2 次后隔 6 天复习 |
| 后续间隔公式 | `interval × EF` | — | 第 3 次及以后按 EF 倍增 |
| 失败时重置 | interval=1, repetitions=0 | — | 答错回到初始间隔 |
| 通过阈值 | quality ≥ 3 | [0, 5] | 评分 ≥ 3 算通过 |
| 掌握度更新 (EMA) | `α=0.3, decay=0.4` | — | 正确 MA+=α(1-MA)，错误 MA*=decay |

实现位置：`super_tutor/core/orchestrator_phases.py` → `_persist_mastery_records()`

---

## 第 7 章 · 4 层 JSON 防御解析

LLM 输出的 JSON 可能包含 Markdown 围栏或格式噪音。解析器采用 4 层防御：

```
第 1 层：json.loads(完整响应)
第 2 层：正则提取 ```json ... ``` 围栏代码块
第 3 层：正则提取第一个 JSON 对象 / 数组
第 4 层：返回 []（兜底，不抛异常）
```

实现位置：`super_tutor/core/orchestrator_utils.py` → `_safe_parse_json_list()`

---

## 第 8 章 · 测试策略

### 8.1 测试金字塔

```
           ┌─────┐
           │ E2E │  2 个场景：上传→做题→排期 全链路
           ├─────┤
           │集成 │  5 个：LLM调用、数据库CRUD、向量检索、PDF解析、流水线
           ├─────┤
           │单元 │  18 个：chunker、JSON解析、SM-2算法、模型校验、异常处理
           └─────┘
```

### 8.2 关键测试用例

| 模块 | 测试用例 | 优先级 |
|------|---------|:---:|
| JSON 解析 | 4 层防御各层能正确解析对应格式 | P0 |
| JSON 解析 | 垃圾文本 → 返回空列表不抛异常 | P0 |
| SM-2 | 连续正确 3 次 → 间隔按预期增长 | P0 |
| SM-2 | 答错 1 次 → interval 重置为 1 天 | P0 |
| 模型校验 | 非法 page 范围 → ValueError | P1 |
| 流水线 | IDLE → start → PARSING → proceed → ... → PLANNING | P0 |
| 流水线 | 任意阶段 → pause → resume → 恢复正确 | P1 |
| 流水线 | 异常 → retry_step 超过 3 次 → 保持错误 | P1 |

当前测试覆盖：18 个 pytest 用例，覆盖 materials / quizzes / dashboard / tokens 四个模块。

---

## 附录 A · 参考资源

| 资源 | URL |
|------|-----|
| SuperMemo SM-2 Algorithm | https://www.supermemo.com/en/blog/application-of-a-computer-to-improve-the-results-obtained-in-working-with-the-supermemo-method |
| sqlite-vec | https://github.com/asg017/sqlite-vec |
| Bloom's Taxonomy | https://bloomstaxonomy.net/ |
| FSRS (Anki 新一代排期) | https://github.com/open-spaced-repetition/fsrs4anki |
| PyMuPDF | https://pymupdf.readthedocs.io/ |
| FastAPI | https://fastapi.tiangolo.com/ |
| Pydantic v2 | https://docs.pydantic.dev/latest/ |
