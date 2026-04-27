"""Read-only loader for kernel/SKILL.md template."""
from pathlib import Path
import frontmatter

from .config import KERNEL_PATH


def load_kernel_template() -> str:
    """Return the raw Markdown text of kernel/SKILL.md."""
    skill_md = Path(KERNEL_PATH) / "SKILL.md"
    return skill_md.read_text(encoding="utf-8")


def get_skill_steps() -> list[dict]:
    """Return the 5 SOP phases extracted from kernel/SKILL.md."""
    return [
        {
            "phase": 1,
            "name": "Discovery",
            "label": "深度需求挖掘",
            "steps": [
                {"id": "core_io", "label": "核心 I/O 洞察", "question": "你希望 Claude 帮你做什么事情？", "options": ["处理文件 (比如 PDF、Excel、图片等)", "帮我写东西 (比如文档、代码、报告)", "连接某个服务 (比如发消息、查数据)", "其他 (我来描述)"]},
                {"id": "deep_insight", "label": "深度洞察", "question": "你觉得这个 Skill 做得好，最重要的是什么？", "options": ["速度快 - 能快速完成任务", "质量高 - 输出结果要精准", "操作简单 - 越少步骤越好", "其他 (我来说)"]},
                {"id": "usage_context", "label": "使用场景", "question": "这个功能你大概会怎么用？", "options": ["经常用 - 每天或每周都会用到", "偶尔用 - 有需要时才用", "自己用 - 只有我一个人用", "给别人用 - 团队或其他人也会用"]},
                {"id": "scope", "label": "作用域确认", "question": "这个 Skill 你想在哪里用？", "options": ["只在当前这个项目用", "所有项目都能用"]},
            ],
        },
        {
            "phase": 2,
            "name": "Blueprint",
            "label": "技能架构蓝图",
            "steps": [
                {"id": "skill_name", "label": "技能名称", "question": "请输入技能名称（小写字母+数字+连字符，最多64字符）：", "options": []},
                {"id": "skill_description", "label": "技能描述", "question": "请描述这个技能的功能和触发场景（最多1024字符）：", "options": []},
                {"id": "trigger_words", "label": "触发词", "question": "用户说什么话会触发这个 Skill？", "options": []},
                {"id": "confirm_blueprint", "label": "确认蓝图", "question": "请确认以上信息是否正确？", "options": ["对，开始做吧", "大体对，但有些地方要改", "不对，我重新说一下"]},
            ],
        },
        {
            "phase": 3,
            "name": "Implementation",
            "label": "工程化实现",
            "steps": [
                {"id": "input_format", "label": "输入格式", "question": "用户会提供什么作为输入？", "options": ["文本描述", "文件（PDF/Excel/图片等）", "链接/URL", "其他"]},
                {"id": "output_format", "label": "输出格式", "question": "期望得到什么输出？", "options": ["结构化文本（列表/表格）", "自由文本（段落）", "代码", "文件"]},
                {"id": "extra_resources", "label": "附加资源", "question": "你有什么现成的资源需要包含到这个 Skill 里吗？", "options": ["有代码/脚本 (如 Python 脚本、Shell 脚本)", "有文档/说明 (如 API 文档、使用指南)", "有模板/素材 (如 logo、模板文件)", "没有，只需要 SKILL.md 就够了"]},
            ],
        },
        {
            "phase": 4,
            "name": "Validation",
            "label": "测试与迭代",
            "steps": [
                {"id": "test_prompt", "label": "测试提问", "question": "我们来测试一下这个 Skill。你平时会怎么向 Claude 提出这类请求？", "options": ["我来说一个典型的请求", "帮我想几个测试用例", "跳过测试"]},
                {"id": "test_result", "label": "测试结果", "question": "测试结果怎么样？", "options": ["很好，完成了", "有点问题，我说一下", "完全不对，重新来"]},
            ],
        },
        {
            "phase": 5,
            "name": "Packaging",
            "label": "打包与分发",
            "steps": [
                {"id": "confirm_package", "label": "打包确认", "question": "你想将这个 Skill 打包为可下载的 ZIP 文件吗？", "options": ["是的，帮我打包", "暂时不需要"]},
            ],
        },
    ]


def get_required_fields() -> list[str]:
    """Return required fields for a valid skill."""
    return ["name", "description", "trigger", "input", "output"]


def get_kernel_metadata() -> dict:
    """Parse and return YAML frontmatter of kernel/SKILL.md."""
    skill_md = Path(KERNEL_PATH) / "SKILL.md"
    post = frontmatter.load(str(skill_md))
    return dict(post.metadata)
