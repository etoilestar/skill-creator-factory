from backend.services.creator.tool_pool_builder import build_tool_pool
from backend.services.creator.tool_pool_models import ToolPoolModel, ToolPoolTool
from backend.services.creator.tool_pool_store import save_tool_pool, load_tool_pool, get_file_binding
from backend.services.creator.generation import _build_script_generate_file_prompt_variant


def _allowed_pool() -> ToolPoolModel:
    return ToolPoolModel(
        skill_name='x',
        tools=[
            ToolPoolTool(
                tool_id='unified_file_text_read',
                status='allowed',
                allowed_helper_imports=['read_file_text'],
                allowed_import_paths=['backend.services.runtime_tools'],
                allowed_function_imports=['read_file_text'],
            ),
            ToolPoolTool(
                tool_id='pdf_to_md_mineru',
                status='allowed',
                allowed_import_paths=['backend.services.runtime_tools.custom_tools.pdf_to_md_mineru'],
                allowed_function_imports=['pdf_to_md_mineru'],
            ),
            ToolPoolTool(
                tool_id='removed_tool',
                status='removed',
                allowed_helper_imports=['extract_pdf_text'],
                allowed_import_paths=['backend.services.runtime_tools'],
                allowed_function_imports=['extract_pdf_text'],
            ),
        ],
    )


def test_build_tool_pool_keeps_skill_wide_tools_and_drops_file_bindings():
    pool = build_tool_pool(skill_name='x', current_tool_pool=_allowed_pool())
    assert pool.file_bindings == []
    assert [tool.tool_id for tool in pool.tools] == ['unified_file_text_read', 'pdf_to_md_mineru', 'removed_tool']


def test_get_file_binding_projects_only_allowed_tools_from_available_tools():
    binding = get_file_binding(_allowed_pool(), 'scripts/a.py')
    assert 'unified_file_text_read' in binding.allowed_tool_ids
    assert 'pdf_to_md_mineru' in binding.allowed_tool_ids
    assert 'removed_tool' not in [tool['tool_id'] for tool in binding.available_tools]
    assert 'read_file_text' in binding.allowed_helper_imports
    assert 'extract_pdf_text' not in binding.allowed_helper_imports
    assert 'backend.services.runtime_tools.custom_tools.pdf_to_md_mineru' in binding.allowed_import_paths
    assert any(
        tool['tool_id'] == 'pdf_to_md_mineru'
        and tool['function_name'] == 'pdf_to_md_mineru'
        and tool['import_path'] == 'backend.services.runtime_tools.custom_tools.pdf_to_md_mineru'
        for tool in binding.available_tools
    )


def test_tool_pool_saved_and_file_binding_summary_has_available_tools(tmp_path):
    save_tool_pool(tmp_path, _allowed_pool())
    loaded = load_tool_pool(tmp_path)
    binding = get_file_binding(loaded, 'scripts/a.py')
    summary = binding.model_dump(mode='json')
    for key in ['available_tools', 'allowed_tool_ids', 'primary_tool_ids', 'secondary_tool_ids', 'allowed_helper_imports', 'allowed_import_paths', 'allowed_function_imports', 'scored_tools', 'matched_features_by_tool', 'denied_helper_imports']:
        assert key in summary
    assert 'backend.services.runtime_tools.custom_tools.pdf_to_md_mineru' in summary['allowed_import_paths']
    assert summary['allowed_import_paths'] == sorted(set(tool['import_path'] for tool in summary['available_tools']))


def test_generation_prompt_includes_available_tool_import_path_and_function_name():
    entry = {
        'path': 'scripts/a.py',
        'role': 'generic_script',
        'runtime': 'python',
        'language': 'python',
        'inputs': ['input_path'],
        'outputs': ['markdown'],
        'tool_binding_summary': {
            'primary_tool_ids': ['pdf_to_md_mineru'],
            'available_tools': [{
                'tool_id': 'pdf_to_md_mineru',
                'function_name': 'pdf_to_md_mineru',
                'import_path': 'backend.services.runtime_tools.custom_tools.pdf_to_md_mineru',
                'input_schema': {},
                'output_schema': {},
            }],
            'scored_tools': [{'tool_id': 'pdf_to_md_mineru', 'score': 222}],
        },
    }
    messages = _build_script_generate_file_prompt_variant(file_path='scripts/a.py', skill_name='x', purpose='把 PDF 转成 Markdown，保留结构', blueprint_text='', role='generic_script', skill_plan_entry=entry, variant='standard')
    text = '\n'.join(str(m.get('content') or '') for m in messages)
    assert 'current_file_tool_binding.available_tools' in text
    assert 'backend.services.runtime_tools.custom_tools.pdf_to_md_mineru' in text
    assert 'pdf_to_md_mineru' in text
