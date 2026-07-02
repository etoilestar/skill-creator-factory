from backend.services.creator.tool_pool_builder import build_tool_pool


def test_build_tool_pool_file_binding_only_allowed_tools():
    pool = build_tool_pool(skill_name='x', user_request='把 PDF 转成文本', file_specs=[{'path': 'scripts/a.py', 'role': 'generic_script', 'inputs': ['a.pdf', 'b.md']}])
    binding = pool.file_bindings[0]
    assert 'pdf_to_md_mineru' in binding.allowed_tool_ids
    assert 'read_file_text' in binding.allowed_helper_imports
    assert 'read_pdf_text' not in binding.allowed_helper_imports


def test_build_tool_pool_marks_mineru_primary_for_markdown_and_custom_imports():
    pool = build_tool_pool(skill_name='x', user_request='把 PDF 转成 Markdown，保留结构', file_specs=[{'path': 'scripts/a.py', 'role': 'generic_script', 'inputs': ['a.pdf'], 'outputs': ['markdown']}])
    binding = pool.file_bindings[0]
    assert binding.primary_tool_ids[1:] == ['pdf_to_md_mineru']
    assert 'unified_file_text_read' in binding.secondary_tool_ids
    assert 'backend.services.runtime_tools.custom_tools.pdf_to_md_mineru' in binding.allowed_import_paths
    assert 'pdf_to_md_mineru' in binding.allowed_function_imports
    assert any(row['tool_id'] == 'pdf_to_md_mineru' and row['decision'] == 'allow' for row in binding.scored_tools)
