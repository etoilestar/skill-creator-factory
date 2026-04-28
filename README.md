# Skill Creator Factory 🧩

> 通过引导式对话，一步步将你的想法打包成可复用的 Claude Skill。

[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-green.svg)](https://fastapi.tiangolo.com/)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](kernel/LICENSE.txt)

---

## 目录

- [项目简介](#项目简介)
- [功能特性](#功能特性)
- [目录结构](#目录结构)
- [快速开始](#快速开始)
  - [方式一：Docker 一键启动（推荐）](#方式一docker-一键启动推荐)
  - [方式二：本地开发启动](#方式二本地开发启动)
- [环境变量](#环境变量)
- [API 接口](#api-接口)
- [后端模块说明](#后端模块说明)
- [Kernel 说明](#kernel-说明)
- [开发指南](#开发指南)
- [技术栈](#技术栈)

---

## 项目简介

Skill Creator Factory 是一个**引导式 Skill 创作平台**。它通过 5 阶段对话流程（SOP），帮助用户系统地设计、描述并打包一个完整的 Claude Skill：

1. **Discovery** — 深度挖掘需求，明确 I/O 和使用场景
2. **Blueprint** — 确定技能名称、描述、触发词
3. **Implementation** — 定义输入格式、输出格式和附加资源
4. **Validation** — 测试与迭代
5. **Packaging** — 生成 ZIP 并下载

最终产物是一个结构完整的 `skill-data/{name}/` 目录，包含 `SKILL.md`、复制自 kernel 的脚本和参考文档，可直接导入 Claude。

---

## 功能特性

- 🗨️ **引导式对话界面** — 步骤进度条 + 选项按钮 + 自由文本输入
- 📦 **一键打包下载** — 生成标准 ZIP，包含完整 Skill 目录结构
- 🔒 **安全路径处理** — 会话文件采用 SHA-256 哈希命名；ZIP 打包通过文件系统扫描而非路径拼接实现，防止路径穿越攻击
- ⚙️ **可配置 LLM 后端** — 通过 `.env` 接入本地 Ollama / LM Studio
- 🐳 **Docker 一键部署** — backend（uvicorn）+ frontend（nginx）开箱即用
- 🧪 **完整单元测试** — 13 个测试覆盖全部核心模块

---

## 目录结构

```
skill-creator-factory/
├── kernel/                        # 只读内核（SOP 模板 + 脚本 + 参考文档）
│   ├── SKILL.md                   # Skill 创建 SOP 模板（4 阶段流程）
│   ├── scripts/
│   │   ├── init_skill.py
│   │   ├── package_skill.py
│   │   └── quick_validate.py
│   └── references/
│       ├── best-practices.md
│       ├── interaction-guide.md
│       ├── output-patterns.md
│       └── workflows.md
│
├── backend/                       # FastAPI 后端
│   ├── main.py                    # 应用入口（CORS、路由注册）
│   ├── requirements.txt
│   ├── routers/
│   │   └── skill.py               # API 路由（5 个端点）
│   ├── modules/
│   │   ├── config.py              # 全局配置（读取 .env）
│   │   ├── skill_kernel_loader.py # 加载并解析 kernel/SKILL.md
│   │   ├── state_machine.py       # 对话状态机（纯内存）
│   │   ├── llm_client.py          # Ollama HTTP 封装
│   │   ├── prompt_generator.py    # 根据步骤生成问题+选项
│   │   ├── user_input_handler.py  # 输入清洗 + 校验
│   │   ├── data_store.py          # JSON 文件持久化
│   │   ├── skill_file_generator.py# 生成 skill 目录 + SKILL.md
│   │   └── packager.py            # ZIP 打包
│   └── tests/
│       └── test_modules.py        # 单元测试（13 个用例）
│
├── frontend/
│   └── index.html                 # 单页 HTML（内联 CSS + JS，无框架）
│
├── skill-data/                    # 运行时数据（gitignored）
│   ├── .sessions/                 # 会话 JSON 文件
│   ├── .packages/                 # 打包的 ZIP 文件
│   └── {skill-name}/              # 生成的 Skill 目录
│
├── .env.example                   # 环境变量模板
├── .gitignore
├── Dockerfile.backend
├── Dockerfile.frontend
└── docker-compose.yml
```

---

## 快速开始

### 前置要求

- [Docker](https://docs.docker.com/get-docker/) + [Docker Compose](https://docs.docker.com/compose/install/)（方式一）
- 或 Python 3.11+（方式二）
- （可选）[Ollama](https://ollama.com/) — 若需要使用 LLM 辅助功能

---

### 方式一：Docker 一键启动（推荐）

```bash
# 1. 克隆项目
git clone https://github.com/etoilestar/skill-creator-factory.git
cd skill-creator-factory

# 2. 复制并按需修改环境变量
cp .env.example .env

# 3. 启动所有服务
docker-compose up -d --build

# 4. 打开浏览器
open http://localhost
```

- 前端界面：`http://localhost`
- 后端 API：`http://localhost:8000`
- 健康检查：`http://localhost:8000/health`

停止服务：

```bash
docker-compose down
```

---

### 方式二：本地开发启动

**后端**

```bash
# 安装依赖
pip install -r backend/requirements.txt

# 复制环境变量
cp .env.example .env

# 启动开发服务器（热重载）
uvicorn backend.main:app --reload --port 8000
```

**前端**

直接用浏览器打开 `frontend/index.html`，或通过任意静态文件服务器托管：

```bash
# 使用 Python 内置服务器（在 frontend/ 目录内）
cd frontend
python -m http.server 3000
# 打开 http://localhost:3000
```

> **注意**：前端默认使用相对路径调用 API（`/api/...`），因此直接打开 HTML 文件时需确保后端运行在同一 origin，或配置反向代理。

---

## 环境变量

复制 `.env.example` 为 `.env` 并按需修改：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `KERNEL_PATH` | `kernel` | kernel 目录路径（相对于项目根目录） |
| `SKILL_DATA_PATH` | `skill-data` | 技能数据输出目录 |
| `LLM_HOST` | `localhost` | Ollama 服务主机 |
| `LLM_PORT` | `11434` | Ollama 服务端口 |
| `LLM_MODEL` | `llama3` | 使用的模型名称 |
| `LOG_LEVEL` | `INFO` | 日志级别 |
| `BACKEND_PORT` | `8000` | 后端监听端口 |
| `CORS_ORIGINS` | `http://localhost,http://localhost:80` | 允许的 CORS 来源（逗号分隔）。**生产环境请勿设置为 `*`** |

---

## API 接口

所有接口前缀为 `/api`。完整交互文档见 `http://localhost:8000/docs`（Swagger UI）。

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/health` | 健康检查，返回 `{"status": "ok"}` |
| `POST` | `/api/start` | 创建新会话，返回 `{session_id, first_prompt}` |
| `POST` | `/api/chat` | 接收用户输入，校验后推进步骤，返回下一个问题 |
| `POST` | `/api/generate` | 根据已收集数据生成 Skill 目录和 `SKILL.md` |
| `GET` | `/api/package/{skill_name}` | 打包并下载 Skill ZIP 文件 |
| `GET` | `/api/status/{session_id}` | 查询当前会话阶段和步骤状态 |
| `GET` | `/api/skills` | 列出所有已生成的 Skill |

**示例：完整创建流程**

```bash
# 1. 开始会话
curl -X POST http://localhost:8000/api/start

# 2. 逐步提交用户输入
curl -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"session_id": "<sid>", "field_name": "core_io", "value": "帮我写东西"}'

# 3. 生成技能文件
curl -X POST http://localhost:8000/api/generate \
  -H "Content-Type: application/json" \
  -d '{"session_id": "<sid>"}'

# 4. 下载 ZIP
curl -OJ http://localhost:8000/api/package/my-skill
```

---

## 后端模块说明

| 模块 | 文件 | 职责 |
|------|------|------|
| config | `modules/config.py` | 读取 `.env`，暴露类型安全的配置常量 |
| skill_kernel_loader | `modules/skill_kernel_loader.py` | 解析 `kernel/SKILL.md`，返回 5 阶段 SOP 结构 |
| state_machine | `modules/state_machine.py` | 纯内存会话状态机，跟踪阶段/步骤进度 |
| llm_client | `modules/llm_client.py` | Ollama HTTP 封装，30s 超时 + 2 次重试 |
| prompt_generator | `modules/prompt_generator.py` | 根据当前步骤生成 `{question, options, step}`，不调用 LLM |
| user_input_handler | `modules/user_input_handler.py` | 清洗控制字符、校验 skill_id 格式、检查必填项 |
| data_store | `modules/data_store.py` | JSON 文件持久化，文件名为 session_id 的 SHA-256 哈希 |
| skill_file_generator | `modules/skill_file_generator.py` | 生成 `skill-data/{name}/`，用 `yaml.safe_dump` 写 SKILL.md frontmatter |
| packager | `modules/packager.py` | 通过 `iterdir()` 扫描目录打包 ZIP，防止路径穿越 |

---

## Kernel 说明

`kernel/` 目录是**只读**的核心模板库，在运行时通过 Docker 卷以只读模式挂载（`./kernel:/app/kernel:ro`）。

- **`SKILL.md`** — 完整的 Skill 创建 SOP，含 5 阶段引导流程，用于驱动对话逻辑
- **`scripts/`** — 辅助脚本，生成 Skill 时会被复制到 `skill-data/{name}/scripts/`
- **`references/`** — 最佳实践参考文档，同样会被复制到生成的 Skill 目录中

> ⚠️ 请勿直接修改 `kernel/` 内容，所有写操作均输出到 `skill-data/`。

---

## 开发指南

**运行测试**

```bash
pip install -r backend/requirements.txt
python -m pytest backend/tests/test_modules.py -v
```

当前覆盖 13 个用例，涵盖 config、kernel_loader、state_machine、user_input_handler、data_store、prompt_generator、skill_file_generator、packager 全部核心模块。

**Skill 名称规则**

Skill 名称（`name` 字段）须满足：
- 只含小写字母、数字、连字符（`-`）
- 首尾不能是连字符
- 最长 64 字符

示例合法名称：`my-skill`、`pdf-summary-v2`、`code-reviewer`

**生成的 Skill 目录结构**

```
skill-data/{skill-name}/
├── SKILL.md           # 含 YAML frontmatter 的技能描述文件
├── scripts/           # 从 kernel/scripts/ 复制的辅助脚本
├── references/        # 从 kernel/references/ 复制的参考文档
└── assets/            # 用户附加资源（可选）
```

---

## 技术栈

| 层 | 技术 |
|----|------|
| 后端框架 | [FastAPI](https://fastapi.tiangolo.com/) + [uvicorn](https://www.uvicorn.org/) |
| 数据校验 | [Pydantic v2](https://docs.pydantic.dev/) |
| 配置管理 | [python-dotenv](https://github.com/theskumar/python-dotenv) |
| Markdown/YAML | [python-frontmatter](https://github.com/eyeseast/python-frontmatter) + [PyYAML](https://pyyaml.org/) |
| HTTP 客户端 | [httpx](https://www.python-httpx.org/) |
| 前端 | 原生 HTML5 + CSS3 + JavaScript（无框架） |
| 静态托管 | [nginx:alpine](https://hub.docker.com/_/nginx) |
| 容器化 | [Docker](https://www.docker.com/) + [Docker Compose](https://docs.docker.com/compose/) |
| 测试 | [pytest](https://pytest.org/) |
