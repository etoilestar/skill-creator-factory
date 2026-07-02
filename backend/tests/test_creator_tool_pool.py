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

from backend.services.creator.tool_pool_store import save_tool_pool, load_tool_pool, get_file_binding
from backend.services.creator.generation import _build_script_generate_file_prompt_variant


def test_tool_pool_saved_and_file_binding_summary_has_custom_import_fields(tmp_path):
    pool = build_tool_pool(skill_name='x', user_request='把 PDF 转成 Markdown，保留结构', file_specs=[{'path': 'scripts/a.py', 'role': 'generic_script', 'inputs': ['a.pdf'], 'outputs': ['markdown']}])
    save_tool_pool(tmp_path, pool)
    loaded = load_tool_pool(tmp_path)
    binding = get_file_binding(loaded, 'scripts/a.py')
    summary = binding.model_dump(mode='json')
    for key in ['allowed_tool_ids', 'primary_tool_ids', 'secondary_tool_ids', 'allowed_helper_imports', 'allowed_import_paths', 'allowed_function_imports', 'scored_tools', 'matched_features_by_tool', 'denied_helper_imports']:
        assert key in summary
    assert 'backend.services.runtime_tools.custom_tools.pdf_to_md_mineru' in summary['allowed_import_paths']


def test_generation_prompt_includes_custom_import_path_and_function_name():
    entry = {
        'path': 'scripts/a.py',
        'role': 'generic_script',
        'runtime': 'python',
        'language': 'python',
        'inputs': ['input_path'],
        'outputs': ['markdown'],
        'tool_binding_summary': {
            'primary_tool_ids': ['pdf_to_md_mineru'],
            'secondary_tool_ids': ['unified_file_text_read'],
            'allowed_import_paths': ['backend.services.runtime_tools.custom_tools.pdf_to_md_mineru'],
            'allowed_function_imports': ['pdf_to_md_mineru'],
            'allowed_helper_imports': ['strict_json_argv_guard', 'read_file_text'],
            'scored_tools': [{'tool_id': 'pdf_to_md_mineru', 'score': 222}],
        },
    }
    messages = _build_script_generate_file_prompt_variant(file_path='scripts/a.py', skill_name='x', purpose='把 PDF 转成 Markdown，保留结构', blueprint_text='', role='generic_script', skill_plan_entry=entry, variant='standard')
    text = '\n'.join(str(m.get('content') or '') for m in messages)
    assert 'backend.services.runtime_tools.custom_tools.pdf_to_md_mineru' in text
    assert 'pdf_to_md_mineru' in text
    assert 'allowed_function_imports' in text
