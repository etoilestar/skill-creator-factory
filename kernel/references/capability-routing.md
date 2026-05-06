# 能力调度与多模型协作指南

本文档用于指导 Skill 生产者设计“能力编排型 Skill”。Skill 不应假设当前加载它的模型必须完成所有工作。当前模型可以只是调度者、规划者、路由器或结果整合者。

---

## 1. 核心思想

一个 Skill 可以把任务拆分给不同能力完成：

- 当前调度模型：理解需求、检查缺失信息、组织流程、汇总结果。
- 写作模型：故事、公文、报告、文案、长文写作。
- 代码模型：代码生成、代码修改、代码审查、测试生成。
- 图像模型：绘图、插图、视觉风格生成。
- 视觉模型：图片理解、截图分析、图表理解。
- 检索能力：搜索、知识库查询、文档检索。
- 脚本工具：文件处理、格式转换、导出 Word/PDF、调用 API。
- 外部服务：数据库、对象存储、业务系统、第三方 API。
- 校验能力：格式检查、事实检查、代码运行、单元测试。

Skill 的职责是描述这些能力如何协作，而不是假设所有事情都由一个模型完成。

---

## 2. 能力名称建议

在 `SKILL.md` 中推荐使用抽象能力名称，而不是写死具体模型名。

推荐能力名称：

| 能力名称 | 用途 |
|---|---|
| `planning` | 任务规划、流程拆解、步骤组织 |
| `writing` | 长文写作、故事、公文、报告、文案 |
| `coding` | 代码生成、代码修改、代码审查 |
| `image_generation` | 图像生成、插图生成、视觉创作 |
| `vision` | 图像理解、截图分析、表格/图表识别 |
| `verification` | 结果检查、格式检查、事实检查、代码审查 |
| `retrieval` | 检索、知识库查询、文档搜索 |
| `document_export` | Word / PDF / HTML / Markdown 导出 |
| `data_processing` | 表格、CSV、JSON、数据库处理 |
| `translation` | 翻译、多语言转换 |
| `summarization` | 摘要、纪要、提炼要点 |
| `reasoning` | 复杂推理、审查、判断 |

宿主系统可以把这些能力映射到具体模型、工具或服务。

---

## 3. 不要写死具体模型

不推荐：

```text
必须调用 qwen-32b 写故事。
```

推荐：

```text
如果宿主提供 writing 能力，优先调用写作模型生成故事正文；否则由当前模型根据 references 中的写作流程直接生成。
```

不推荐：

```text
调用 image-model-v1 生成插图。
```

推荐：

```text
如果宿主提供 image_generation 能力，则调用图像生成工具；否则输出可用于图像生成的提示词。
```

这样生成的 Skill 更容易迁移到不同宿主和不同模型。

---

## 4. 能力调用必须有输入输出契约

如果 Skill 需要某个专用能力，必须说明：

1. 什么时候调用。
2. 输入是什么。
3. 输出是什么。
4. 结果如何被下一步使用。
5. 失败时如何降级。
6. 是否需要人工确认。

示例：

```markdown
## 能力调度说明

1. 当前调度模型先解析用户需求，提取主题、角色、风格、篇幅。
2. 如果宿主提供 `writing` 能力，将以下字段传给写作模型：
   - theme
   - characters
   - style
   - target_audience
   - length
3. 写作模型返回完整故事正文。
4. 如果用户要求导出 Word，则将故事正文交给 `scripts/export_docx.py`。
5. 如果 `writing` 能力不可用，则当前模型直接根据 `references/story-structure.md` 生成故事正文。
```

---

## 5. 不要假装调用

禁止：

- 假装已经调用图像模型。
- 假装已经调用代码模型。
- 假装已经生成文件。
- 假装已经访问外部 API。
- 假装已经运行脚本。
- 假装已经完成后台任务。

必须基于真实 observation 回答。

如果宿主没有对应能力，应说明：

```text
当前宿主未提供 image_generation 能力，因此本次只输出图像生成提示词，不生成图片文件。
```

---

## 6. 能力不可用时的降级策略

每个外部能力都应该有降级方案。

示例：

| 能力 | 降级方案 |
|---|---|
| `writing` 不可用 | 当前模型直接生成正文 |
| `coding` 不可用 | 当前模型生成代码草稿，并提示需要人工审查 |
| `image_generation` 不可用 | 输出图像提示词，不生成图片 |
| `document_export` 不可用 | 输出 Markdown，不生成 Word/PDF |
| `retrieval` 不可用 | 只基于用户提供内容回答 |
| 外部 API 不可用 | 说明失败原因，要求用户稍后重试或提供离线数据 |
| 数据库不可用 | 返回配置错误，不编造查询结果 |
| 脚本不可用 | 返回脚本缺失或依赖缺失，不假装执行 |

---

## 7. 多模型工作流示例：故事 + 插图 + Word

推荐流程：

```text
用户输入主题
→ 调度模型解析需求
→ writing 能力生成故事正文
→ image_generation 能力生成插图或提示词
→ scripts/export_docx.py 生成 Word
→ verification 能力检查故事和格式
```

推荐结构：

```text
story-picture-docx/
├── SKILL.md
├── references/
│   ├── story-structure.md
│   └── image-style-guide.md
├── assets/
│   └── docx-template-notes.md
├── scripts/
│   └── export_docx.py
└── requirements.txt
```

`SKILL.md` 应说明：

- 哪些字段由用户提供。
- 写作能力如何调用。
- 图像能力不可用时如何降级。
- Word 导出脚本如何运行。
- 生成结果如何验收。

---

## 8. 多模型工作流示例：代码生成

推荐流程：

```text
用户输入接口需求
→ 调度模型澄清需求
→ coding 能力生成代码
→ scripts/run_tests.py 执行测试
→ verification 能力解释测试结果
```

推荐结构：

```text
api-code-generator/
├── SKILL.md
├── references/
│   ├── project-style.md
│   └── api-patterns.md
├── assets/
│   └── route-template.py
├── scripts/
│   └── run_tests.py
└── requirements.txt
```

---

## 9. 多模型工作流示例：合同审查

推荐流程：

```text
用户提供合同文本
→ 调度模型识别合同类型和审查目标
→ reasoning / legal-review 能力识别风险
→ verification 能力检查审查意见是否引用原文
→ 输出风险表格和修改建议
```

推荐结构：

```text
contract-risk-reviewer/
├── SKILL.md
├── references/
│   ├── risk-types.md
│   ├── review-checklist.md
│   └── examples.md
└── assets/
    └── review-report-template.md
```

---

## 10. 能力调度检查清单

设计 Skill 时检查：

- [ ] 是否需要专用模型或外部能力？
- [ ] 是否说明了能力名称？
- [ ] 是否避免写死具体模型名？
- [ ] 是否定义了输入输出契约？
- [ ] 是否说明结果如何传给下一步？
- [ ] 是否说明失败或不可用时的降级方案？
- [ ] 是否避免假装调用？
- [ ] 是否说明最终结果如何验证？
- [ ] 是否说明用户需要提供哪些资源或配置？