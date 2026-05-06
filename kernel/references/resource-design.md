# Skill 资源设计指南

本文档用于判断一个 Skill 应该如何分配 `SKILL.md`、`references/`、`assets/`、`scripts/`、依赖文件和配置说明的职责。

---

## 1. 总原则

Skill 包不是越复杂越好，也不是越少文件越好。

正确原则是：

> 核心流程放在 SKILL.md，长知识放 references，固定模板放 assets，确定性执行放 scripts，依赖和配置必须声明清楚。

不要根据任务类别机械决定资源类型。

不要默认必须生成脚本。

不要默认禁止生成脚本。

资源选择由具体 workflow 决定。

---

## 2. SKILL.md 放什么

`SKILL.md` 是 Agent 执行 Skill 时最先读取的正文。

它应该放：

- 这个 Skill 做什么
- 用户需要提供什么
- Agent 应该按什么步骤处理
- 哪些步骤由当前模型完成
- 哪些步骤调用专用模型、脚本、外部接口或工具
- 需要时去哪里读取 references/assets/scripts
- 输出应该是什么格式
- 缺少信息时如何追问
- 外部能力不可用时如何降级

不应该放：

- 大量长示例
- 完整行业规范
- 大量模板正文
- 大段代码
- 多个不常用变体的详细规则

---

## 3. references/ 放什么

`references/` 用于放较长、较细、可按需读取的知识。

适合放：

- 领域规则
- 写作规范
- 术语说明
- API 文档摘要
- 数据库 schema
- 示例输入输出
- 常见错误
- 多任务变体
- 复杂判断标准
- workflow 细节
- 模型调度说明
- 质量检查规则

示例：

```text
references/story-structure.md
references/style-guide.md
references/examples.md
references/schema.md
references/api-notes.md
```

---

## 4. assets/ 放什么

`assets/` 用于放模板、配置、静态资源和样例文件。

适合放：

- Jinja 模板
- Markdown 模板
- Word 模板
- HTML 模板
- JSON 模板
- YAML 配置
- 样例输入文件
- 样例输出文件
- 固定格式骨架
- 图片、图标、字体

示例：

```text
assets/notice-template.md
assets/report-template.md
assets/config.yaml
assets/example-input.json
assets/template.docx
assets/template.html
```

---

## 5. scripts/ 放什么

`scripts/` 用于放可以真实运行的代码。

适合放：

- 数据清洗
- 文件格式转换
- 表格处理
- 字段校验
- 模板渲染
- 批量文件操作
- 调用外部 API
- 调用本地模型或外部模型
- 生成固定结构数据
- 生成实际文件并返回路径
- 执行测试
- 调用数据库
- 调用检索服务

`scripts/` 可以用于任何类型的 Skill，但必须有明确职责和可测试命令。

---

## 6. 依赖文件放什么

如果脚本需要第三方依赖，应创建或说明依赖文件。

常见依赖文件：

```text
requirements.txt
package.json
go.mod
environment.yml
```

如果依赖很少，也可以在 `SKILL.md` 的“依赖与配置”中说明。

要求：

1. 不写无用依赖。
2. 不声明不存在的库。
3. 如果依赖可能无法安装，说明降级方案。
4. 不把 API Key、Token、密码写进依赖或配置文件。

---

## 7. 按 workflow 判断资源

### 只需要模型直接生成

如果任务只需要模型根据用户输入直接生成结果，通常只需要：

```text
skill-name/
└── SKILL.md
```

可选：

```text
references/examples.md
```

### 需要领域规则或长示例

如果任务需要领域规则、风格规范、长示例、判断标准，建议：

```text
skill-name/
├── SKILL.md
└── references/
    ├── domain-rules.md
    └── examples.md
```

### 需要模板或固定输出骨架

如果任务需要固定模板、样例文件、配置或格式骨架，建议：

```text
skill-name/
├── SKILL.md
├── references/
│   └── rules.md
└── assets/
    └── template.md
```

### 需要真实执行或文件输出

如果任务需要真实执行、处理文件、批量处理、调用 API、生成文件，建议：

```text
skill-name/
├── SKILL.md
├── scripts/
│   └── main.py
└── references/
    └── usage-notes.md
```

可选：

```text
assets/template.docx
requirements.txt
```

### 需要多模型协作

如果任务需要不同模型能力协作，建议：

```text
skill-name/
├── SKILL.md
├── references/
│   └── model-routing.md
├── assets/
│   └── template.md
└── scripts/
    └── postprocess.py
```

`SKILL.md` 中必须说明能力调度和降级方案。

---

## 8. 混合任务设计

混合任务同时包含模型生成和可执行后处理。

示例：

```text
用户输入主题
→ writing 能力生成故事正文
→ 脚本保存为 Word 文件
```

关键要求：

1. `SKILL.md` 必须说明模型负责什么。
2. `SKILL.md` 必须说明脚本负责什么。
3. 脚本调用方式必须真实可运行。
4. 如果脚本需要正文作为输入，必须说明正文如何传入脚本。
5. 如果脚本自己调用 LLM，必须说明所需配置。
6. 不要让脚本假装完成未实现的模型能力。

---

## 9. 判断口诀

创建 Skill 时使用这个判断：

```text
需要模型理解、判断、创作 → SKILL.md / references
需要固定模板、样式、骨架 → assets
需要稳定执行、批处理、转换、校验、文件输出 → scripts
需要第三方库 → 依赖文件或依赖说明
需要外部接口 → 配置说明和环境变量
需要专用模型 → 能力调度说明和降级方案
```

不要把所有能力都塞进脚本。

不要把所有规则都塞进 SKILL.md。

不要创建没有被使用的资源文件。