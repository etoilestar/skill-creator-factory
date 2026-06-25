"""各 SubAgent 类型的默认 SandboxPolicy 定义。

每个 SubAgent 类型拥有独立的权限边界和工具白名单，
确保数据查询 Agent 无法执行脚本、网络 Agent 无法写文件等。
"""

from __future__ import annotations

from .sub_agent_types import SubAgentPolicy, SubAgentType


# ---------------------------------------------------------------------------
#  各 SubAgent 类型的默认策略
# ---------------------------------------------------------------------------

_DATA_QUERY_POLICY = SubAgentPolicy(
    allow_network=False,
    allow_file_write=True,
    allow_subprocess=True,
    allow_db_write=False,
    allowed_tool_categories=["retrieval", "common"],
    allowed_tool_roles=["database_reader", "spreadsheet_reader", "generic_script"],
    max_timeout=300,
)

_CODE_SCRIPT_POLICY = SubAgentPolicy(
    allow_network=False,
    allow_file_write=True,
    allow_subprocess=True,
    allow_db_write=False,
    allowed_tool_categories=["generation", "common"],
    allowed_tool_roles=["text_generator", "generic_script", "file_output", "asset_builder"],
    max_timeout=300,
)

_NETWORK_API_POLICY = SubAgentPolicy(
    allow_network=True,
    allow_file_write=False,
    allow_subprocess=False,
    allow_db_write=False,
    allowed_tool_categories=["retrieval", "common"],
    allowed_tool_roles=["http_request", "web_search", "search_reader"],
    max_timeout=300,
)

_DOCUMENT_POLICY = SubAgentPolicy(
    allow_network=False,
    allow_file_write=True,
    allow_subprocess=True,
    allow_db_write=False,
    allowed_tool_categories=["document", "common"],
    allowed_tool_roles=[
        "pdf_builder", "pdf_parser",
        "docx_builder", "docx_parser",
        "pptx_builder", "pptx_parser",
        "html_asset_builder",
        "spreadsheet_reader",
        "generic_script",
    ],
    max_timeout=300,
)

_VALIDATION_POLICY = SubAgentPolicy(
    allow_network=False,
    allow_file_write=False,
    allow_subprocess=False,
    allow_db_write=False,
    allowed_tool_categories=["common"],
    allowed_tool_roles=["deterministic_execution"],
    max_timeout=120,
)


# ---------------------------------------------------------------------------
#  策略注册表
# ---------------------------------------------------------------------------

_SUB_AGENT_POLICIES: dict[SubAgentType, SubAgentPolicy] = {
    SubAgentType.DATA_QUERY: _DATA_QUERY_POLICY,
    SubAgentType.CODE_SCRIPT: _CODE_SCRIPT_POLICY,
    SubAgentType.NETWORK_API: _NETWORK_API_POLICY,
    SubAgentType.DOCUMENT: _DOCUMENT_POLICY,
    SubAgentType.VALIDATION: _VALIDATION_POLICY,
}


def get_default_policy(sub_agent_type: SubAgentType) -> SubAgentPolicy:
    """获取指定 SubAgent 类型的默认权限策略。

    Args:
        sub_agent_type: SubAgent 类型。

    Returns:
        该类型的默认 SubAgentPolicy 实例。
    """
    return _SUB_AGENT_POLICIES.get(sub_agent_type, SubAgentPolicy())


def get_all_policies() -> dict[SubAgentType, SubAgentPolicy]:
    """获取所有 SubAgent 类型的默认策略映射。"""
    return dict(_SUB_AGENT_POLICIES)


def describe_policy(policy: SubAgentPolicy) -> str:
    """生成人类可读的策略描述，用于注入 SubAgent 系统提示词。"""
    parts = []
    if policy.allow_subprocess:
        parts.append("允许执行命令/脚本")
    else:
        parts.append("禁止执行命令/脚本")

    if policy.allow_file_write:
        parts.append("允许写入文件/创建目录")
    else:
        parts.append("禁止写入文件/创建目录")

    if policy.allow_network:
        parts.append("允许网络访问")
    else:
        parts.append("禁止网络访问")

    if policy.allow_db_write:
        parts.append("允许数据库写操作(DDL/DML)")
    else:
        parts.append("禁止数据库写操作")

    if policy.allowed_tool_categories:
        parts.append(f"允许的工具类别: {', '.join(policy.allowed_tool_categories)}")

    return "；".join(parts)