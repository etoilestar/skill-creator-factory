# Skill Creator Factory

> 基于本地大模型的 AI Skill 创建与管理平台

一个全栈 Web 应用，让你通过对话式 AI 引导快速设计、调试和管理 Claude Skill（`SKILL.md` 指令集），并直接在沙盒中验证效果——全程运行在你自己的机器上，无需云端 API Key。

---

## ✨ 功能特性

| 功能 | 说明 |
|------|------|
| **Creator 模式** | 由 `kernel/SKILL.md` 驱动的 AI 助手，通过结构化 SOP 引导你完成 Skill 需求挖掘、架构蓝图、工程实现、测试迭代全流程 |
| **Sandbox 模式** | 将任意已保存的 Skill 加载为系统提示词，立即在对话中验证效果 |
| **Skills 库管理** | 内置浏览器：列表预览、全文查看、在线编辑、一键删除 |
| **流式输出** | 后端通过 SSE 实时推送 LLM token，前端逐字呈现，无等待感 |
| **本地 LLM 优先** | 兼容任何支持 OpenAI `/v1` 接口的本地后端（Ollama、LM Studio 等） |
| **Docker 一键部署** | 单条命令启动完整服务栈 |

---

## 🏗️ 架构概览

```
skill-creator-factory/
├── backend/                  # FastAPI 后端（Python 3.11+）
│   ├── main.py               # 应用入口，CORS 配置
│   ├── config.py             # 环境变量 & 路径配置（pydantic-settings）
│   ├── routers/
│   │   ├── chat.py           # POST /api/chat/creator & /api/chat/sandbox/{name}
│   │   ├── skills.py         # CRUD /api/skills
│   │   └── health.py         # GET /api/health
│   └── services/
│       ├── kernel_loader.py  # 加载 kernel/SKILL.md 或 skills/{name}/SKILL.md
│       ├── llm_proxy.py      # 流式代理至 Ollama/LM Studio（OpenAI 兼容）
│       └── skill_manager.py  # Skill 目录 CRUD + YAML frontmatter 解析
│
├── frontend/                 # Vue 3 + Vite 前端
│   └── src/
│       ├── views/
│       │   ├── CreatorView.vue   # Creator 对话界面
│       │   ├── SandboxView.vue   # Sandbox 对话界面
│       │   └── SkillsView.vue    # Skills 库管理界面
│       └── composables/          # useChat / useSkills 封装
│
├── kernel/                   # Skill 创建引擎（只读）
│   ├── SKILL.md              # Creator 模式系统提示词（5 阶段 SOP）
│   ├── references/           # 最佳实践、交互指南、输出模式等参考文档
│   └── scripts/
│       ├── init_skill.py     # 脚手架：从模板初始化新 Skill 目录
│       ├── package_skill.py  # 打包：将 Skill 目录压缩为 .skill 文件
│       └── quick_validate.py # 验证：检查 frontmatter 规范
│
├── skills/                   # 用户 Skill 库（读写，默认为空）
└── docker-compose.yml
```

**数据流**

```
浏览器
  │ POST /api/chat/creator
  │   └─► FastAPI → kernel/SKILL.md (system prompt) → LLM (Ollama/LM Studio)
  │         └─► SSE stream → 浏览器实时渲染
  │
  │ POST /api/chat/sandbox/{skill_name}
  │   └─► FastAPI → skills/{name}/SKILL.md (system prompt) → LLM
  │
  │ GET/POST/DELETE /api/skills
        └─► FastAPI → skills/ 目录 CRUD
```

---

## 🚀 快速开始

### 前置条件

- [Docker & Docker Compose](https://docs.docker.com/get-docker/)（推荐）或 Python 3.11+ / Node.js 18+
- 本地运行 [Ollama](https://ollama.ai) 或 [LM Studio](https://lmstudio.ai)，并已拉取至少一个模型

### 方式一：Docker（推荐）

```bash
# 1. 克隆仓库
git clone https://github.com/etoilestar/skill-creator-factory.git
cd skill-creator-factory

# 2. （可选）复制并修改环境变量
cp backend/.env.example .env
# 编辑 .env：设置 LLM_BASE_URL 和 DEFAULT_MODEL

# 3. 启动
docker-compose up --build
```

| 服务 | 地址 |
|------|------|
| 前端 | http://localhost:5173 |
| 后端 API | http://localhost:8000 |
| API 文档 | http://localhost:8000/docs |

### 方式二：本地开发

**后端**

```bash
cd backend
pip install -r requirements.txt

# 配置环境变量
cp .env.example .env   # 按需修改 LLM_BASE_URL / DEFAULT_MODEL

# 启动（在仓库根目录执行）
uvicorn backend.main:app --reload
```

**前端**

```bash
cd frontend
npm install
npm run dev
```

---

## ⚙️ 配置

环境变量可在 `backend/.env` 中设置（Docker 模式在根目录 `.env` 或 `docker-compose.yml` 中覆盖）：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `LLM_BASE_URL` | `http://localhost:11434` | 本地 LLM 后端地址。Ollama 默认 `:11434`，LM Studio 默认 `:1234` |
| `DEFAULT_MODEL` | `llama3.2` | 默认使用的模型名称，须在你的 LLM 后端中已加载 |

**Docker 环境下访问宿主机 LLM**：`docker-compose.yml` 已预配置 `host.docker.internal` 解析，Linux 下同样有效。

---

## 📖 使用指南

### Creator 模式

1. 打开 **Creator** 页面
2. 告诉 AI 你想创建什么 Skill（例如："帮我做一个分析 Excel 报表的 Skill"）
3. AI 会依照 5 阶段 SOP 逐步引导：
   - **Phase 1** 深度需求挖掘（I/O 定义、技术方案、作用域）
   - **Phase 2** 架构蓝图确认
   - **Phase 3** 工程化实现（生成 SKILL.md 及资源文件）
   - **Phase 4** 测试与迭代
   - **Phase 5** 打包与分发
4. 将 AI 生成的 `SKILL.md` 内容复制到 **Skills 库** 中保存

### Sandbox 模式

1. 在 **Skills 库** 中至少保存一个 Skill
2. 打开 **Sandbox** 页面，从下拉列表选择 Skill
3. 直接与该 Skill 对话，验证触发词、输出格式是否符合预期

### Skills 库管理

- **新建**：点击「+ 新建 Skill」，填写名称并编写 `SKILL.md` 内容
- **编辑**：选中 Skill 后点击「编辑」
- **删除**：选中 Skill 后点击「删除」，二次确认后不可恢复

---

## 🛠️ Skill 规范

每个 Skill 是一个目录，核心文件为 `SKILL.md`：

```
skills/{skill-name}/
├── SKILL.md        ← 必须
├── scripts/        ← 可选：可执行脚本（Python/Bash）
├── references/     ← 可选：参考文档（按需加载进上下文）
└── assets/         ← 可选：模板/素材（用于输出，不注入上下文）
```

**`SKILL.md` 最小结构**

```markdown
---
name: my-skill-name          # 小写字母 + 数字 + 连字符，最多 64 字符
description: 一句话说明做什么、何时触发。# 最多 1024 字符
---

# My Skill

具体指令内容…
```

**命令行工具**（位于 `kernel/scripts/`）

```bash
# 初始化新 Skill（含模板文件）
python kernel/scripts/init_skill.py <skill-name> --path skills/

# 验证 Skill frontmatter 是否合规
python kernel/scripts/quick_validate.py skills/<skill-name>

# 打包为可分发的 .skill 文件
python kernel/scripts/package_skill.py skills/<skill-name> [output-dir]
```

---

## 🔌 API 参考

后端运行后，完整交互文档见 **http://localhost:8000/docs**（Swagger UI）。

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/health` | 健康检查 + LLM 连接状态 |
| `POST` | `/api/chat/creator` | Creator 模式流式对话（SSE） |
| `POST` | `/api/chat/sandbox/{skill_name}` | Sandbox 模式流式对话（SSE） |
| `GET` | `/api/skills` | 获取所有 Skill 列表 |
| `POST` | `/api/skills` | 创建/覆盖一个 Skill |
| `GET` | `/api/skills/{skill_name}` | 获取单个 Skill 详情 |
| `DELETE` | `/api/skills/{skill_name}` | 删除一个 Skill |

**流式响应格式**（SSE）

```
data: {"content": "token..."}
data: {"content": "token..."}
data: [DONE]
```

错误时返回：

```
data: {"error": "可读的错误信息"}
```

---

## 🤝 贡献

欢迎提交 Issue 和 Pull Request。

1. Fork 本仓库
2. 创建功能分支：`git checkout -b feat/your-feature`
3. 提交变更并推送
4. 发起 Pull Request

---

## 📄 许可证

本项目内核（`kernel/`）遵循 [`kernel/LICENSE.txt`](kernel/LICENSE.txt) 中的许可条款。