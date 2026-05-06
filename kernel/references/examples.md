# Skill 设计示例

本文档提供不同类型 Skill 的设计样例，用于帮助生产者避免生成空心 Skill，同时保持资源设计灵活。

---

## 1. 故事生成 Skill

### 用户需求

> 帮我做一个能写故事的 Skill。

### 需要先追问

```text
好的，我先确认一个关键信息：这个故事 Skill 最常见的使用场景是什么？请给我一个用户可能会说的真实例子。
```

如果用户只需要故事正文，可能结构：

```text
story-writer/
├── SKILL.md
└── references/
    ├── story-structure.md
    ├── style-guide.md
    └── examples.md
```

如果用户需要生成 Word 文件，可能结构：

```text
story-docx-writer/
├── SKILL.md
├── references/
│   ├── story-structure.md
│   ├── style-guide.md
│   └── examples.md
├── assets/
│   └── docx-style-notes.md
├── scripts/
│   └── export_docx.py
└── requirements.txt
```

如果宿主支持写作模型，`SKILL.md` 可写：

```text
如果宿主提供 writing 能力，优先调用写作模型生成故事正文；否则由当前模型直接生成。
```

脚本可负责：

- 生成 Word 文件
- 渲染故事模板
- 保存文件
- 返回文件路径
- 调用 LLM API 生成故事正文

脚本不应负责：

- 用固定开头结尾拼接故事
- 用随机片段伪装高质量创作
- 只返回“故事：用户输入”

---

## 2. 公文生成 Skill

### 用户需求

> 帮我做一个能根据要点生成正式公文的 Skill。

### 需要先追问

```text
好的，我先确认一个关键信息：这个公文 Skill 最常见的使用场景是什么？例如通知、请示、报告、函、会议纪要，还是其他？
```

可能结构：

```text
official-document-writer/
├── SKILL.md
├── references/
│   ├── document-types.md
│   ├── official-style.md
│   └── examples.md
└── assets/
    ├── notice-template.md
    ├── request-template.md
    └── report-template.md
```

如果用户要求导出 Word 文件，可增加：

```text
scripts/render_docx.py
requirements.txt
```

脚本职责：

- 校验字段
- 选择模板
- 渲染文档
- 保存文件

脚本不应：

- 只返回 `公文概要：用户输入`
- 凭空编造事实
- 和 `SKILL.md` 的调用方式不一致

---

## 3. 代码生成 Skill

### 用户需求

> 做一个生成 Flask 接口代码的 Skill。

### 可能结构

```text
flask-api-generator/
├── SKILL.md
├── references/
│   ├── project-style.md
│   └── api-patterns.md
├── assets/
│   └── route-template.py
└── scripts/
    └── run_tests.py
```

能力分工：

- 调度模型：澄清接口需求。
- coding 能力：生成代码。
- scripts/run_tests.py：运行测试。
- verification 能力：解释测试结果并提出修改建议。

---

## 4. 图像生成 Skill

### 用户需求

> 做一个根据文字需求生成插画提示词并调用画图模型的 Skill。

### 可能结构

```text
illustration-generator/
├── SKILL.md
├── references/
│   ├── style-guide.md
│   └── prompt-patterns.md
└── assets/
    └── style-examples.json
```

如果宿主提供 `image_generation` 能力：

```text
调用图像生成能力输出图片。
```

如果宿主没有该能力：

```text
输出高质量图像生成提示词，不假装已经生成图片。
```

---

## 5. JSON 转 YAML Skill

### 用户需求

> 做一个把 JSON 转成 YAML 的 Skill。

### 推荐结构

```text
json-to-yaml/
├── SKILL.md
├── scripts/
│   └── main.py
└── requirements.txt
```

### scripts/main.py 应实现

- 从 stdin 或参数读取 JSON。
- 校验 JSON 合法性。
- 转换为 YAML。
- 中文报错或清晰报错。
- 输出 YAML。

### SKILL.md 应包含

- 使用方式。
- 输入要求。
- 输出格式。
- 错误处理。
- 示例命令。

---

## 6. CSV 清洗 Skill

### 用户需求

> 做一个清洗 CSV 表格的 Skill。

### 推荐结构

```text
csv-cleaner/
├── SKILL.md
├── scripts/
│   └── main.py
└── references/
    └── cleaning-rules.md
```

### scripts/main.py 应实现

- 读取 CSV。
- 检查列名。
- 去除空行。
- 处理缺失值。
- 输出清洗后的 CSV。
- 清晰报错。

---

## 7. 合同审查 Skill

### 用户需求

> 做一个合同风险审查 Skill。

### 可能结构

```text
contract-risk-reviewer/
├── SKILL.md
└── references/
    ├── risk-types.md
    ├── review-checklist.md
    └── examples.md
```

可选：

```text
assets/review-report-template.md
```

能力分工：

- 当前模型：解析合同文本和用户目标。
- legal-review 或 reasoning 能力：审查条款风险。
- verification 能力：检查输出是否引用了原文条款。
- references：提供风险类型、审查清单和示例。

---

## 8. 错误示例

### 错误做法

用户说：

```text
做一个写故事 Skill。
```

直接输出：

```text
以下是一个用于生成故事的 Skill 的设计文档及实现代码。
```

这是错误的。应该先追问具体使用场景。

---

### 错误做法

生成：

```python
def generate_story(data: str) -> str:
    return f"故事：{data}"
```

这是错误的。脚本只是包装输入。

---

### 错误做法

`SKILL.md` 写：

```text
运行 python scripts/main.py
```

但没有创建 `scripts/main.py`。

这是错误的。脚本路径必须真实存在。

---

## 9. 最小验收样例写法

每个 Skill 蓝图都应包含一个最小验收样例。

示例：

```text
输入：
请帮我写一个关于勇敢小狐狸的童话，面向 8 岁儿童，约 500 字。

预期输出：
一篇完整童话，包含开端、冲突、行动、转折和结尾，语言适合儿童阅读，主题突出勇气。
```

不要只写：

```text
输入：示例输入
输出：示例输出
```

---

## 10. 最终提醒

生成 Skill 时始终问自己：

1. 这个 Skill 是否真的让 Agent 获得了能力？
2. 是否只是创建了一个能跑但无用的脚本？
3. 是否应该把领域规则放到 references？
4. 是否应该把模板放到 assets？
5. 脚本是否有真实职责？
6. 是否需要专用模型能力？
7. 是否有能力不可用时的降级方案？
8. 用户真实使用时，这个 Skill 能否完成任务？