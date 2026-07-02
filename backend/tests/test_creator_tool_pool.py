from backend.services.creator.tool_pool_builder import build_tool_pool


def test_build_tool_pool_file_binding_only_allowed_tools():
    pool = build_tool_pool(skill_name='x', file_specs=[{'path': 'scripts/a.py', 'role': 'generic_script', 'inputs': ['a.pdf', 'b.md']}])
    binding = pool.file_bindings[0]
    assert 'unified_file_text_read' in binding.allowed_tool_ids
    assert 'read_file_text' in binding.allowed_helper_imports
    assert 'read_pdf_text' not in binding.allowed_helper_imports
