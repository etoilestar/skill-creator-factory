from backend.services.creator.runtime_import_guard import guard_runtime_imports


def test_import_guard_blocks_invented_helpers():
    result = guard_runtime_imports('from backend.services.runtime_tools import read_pdf_text\n', 'scripts/a.py', {'allowed_helper_imports': ['extract_pdf_text']})
    assert not result.success
    assert result.error_type == 'generated_unknown_runtime_tool_import'
    assert 'read_pdf_text' in result.missing_imports
    assert result.suggested_replacements == []


def test_import_guard_blocks_star_and_pool_forbidden():
    result = guard_runtime_imports('from backend.services.runtime_tools import *\n', 'scripts/a.py', {'allowed_helper_imports': ['extract_pdf_text']})
    assert not result.success
    assert '*' in result.forbidden_imports


def test_import_guard_allows_bound_helper():
    result = guard_runtime_imports('from backend.services.runtime_tools import read_file_text\n', 'scripts/a.py', {'allowed_helper_imports': ['read_file_text']})
    assert result.success


def test_import_guard_blocks_mixed_bound_and_unbound_runtime_helpers():
    result = guard_runtime_imports(
        'from backend.services.runtime_tools import read_file_text, read_pdf_text\n',
        'scripts/a.py',
        {'allowed_helper_imports': ['read_file_text']},
    )
    assert not result.success
    assert result.error_type == 'generated_unknown_runtime_tool_import'
    assert 'read_pdf_text' in result.missing_imports


def test_import_guard_allows_bound_custom_tool():
    src = 'from backend.services.runtime_tools.custom_tools.pdf_to_md_mineru import pdf_to_md_mineru\n'
    result = guard_runtime_imports(src, 'scripts/a.py', {'allowed_import_paths': ['backend.services.runtime_tools.custom_tools.pdf_to_md_mineru'], 'allowed_function_imports': ['pdf_to_md_mineru']})
    assert result.success


def test_import_guard_allows_custom_tool_full_function_path_and_diagnoses_same_module_unbound_function():
    allowed = 'backend.services.runtime_tools.custom_tools.lookup.lookup_value'
    ok = guard_runtime_imports(
        'from backend.services.runtime_tools.custom_tools.lookup import lookup_value\n',
        'scripts/a.py',
        {
            'allowed_import_paths': ['backend.services.runtime_tools.custom_tools.lookup'],
            'allowed_function_imports': [allowed],
        },
    )
    assert ok.success

    bad = guard_runtime_imports(
        'from backend.services.runtime_tools.custom_tools.lookup import lookup_value, other_value\n',
        'scripts/a.py',
        {
            'allowed_import_paths': ['backend.services.runtime_tools.custom_tools.lookup'],
            'allowed_function_imports': [allowed],
        },
    )
    assert bad.success
    assert 'backend.services.runtime_tools.custom_tools.lookup.other_value' in bad.forbidden_imports
    assert any('observed import not present' in warning for warning in bad.warnings)


def test_import_guard_diagnoses_unbound_custom_tool_but_blocks_wildcard():
    src = 'from backend.services.runtime_tools.custom_tools.pdf_to_md_mineru import pdf_to_md_mineru\n'
    result = guard_runtime_imports(src, 'scripts/a.py', {'allowed_import_paths': [], 'allowed_function_imports': []})
    assert result.success
    assert 'backend.services.runtime_tools.custom_tools.pdf_to_md_mineru.pdf_to_md_mineru' in result.forbidden_imports
    wildcard = guard_runtime_imports('from backend.services.runtime_tools.custom_tools.pdf_to_md_mineru import *\n', 'scripts/a.py', {'allowed_import_paths': ['backend.services.runtime_tools.custom_tools.pdf_to_md_mineru'], 'allowed_function_imports': ['pdf_to_md_mineru']})
    assert not wildcard.success
    assert wildcard.error_type == 'generated_custom_tool_wildcard_import'


def test_import_guard_reports_pool_external_custom_tool_as_diagnostic():
    src = 'from backend.services.runtime_tools.custom_tools.https_google_serper_dev_search import https_google_serper_dev_search\n'
    result = guard_runtime_imports(src, 'scripts/a.py', {'allowed_import_paths': ['backend.services.runtime_tools.custom_tools.pdf_to_md_mineru'], 'allowed_function_imports': ['pdf_to_md_mineru']})
    assert result.success
    assert 'backend.services.runtime_tools.custom_tools.https_google_serper_dev_search.https_google_serper_dev_search' in result.forbidden_imports


def test_import_guard_reports_unbound_read_file_text_without_hard_failure():
    result = guard_runtime_imports(
        'from backend.services.runtime_tools import read_file_text\n',
        'scripts/a.py',
        {'allowed_helper_imports': []},
    )
    assert result.success
    assert 'read_file_text' in result.forbidden_imports
    assert any('observed import not present' in warning for warning in result.warnings)


def test_import_guard_allows_open_for_txt_without_bound_helper():
    src = """
def read_txt(path):
    with open(path, 'r', encoding='utf-8') as handle:
        return handle.read()
"""
    result = guard_runtime_imports(src, 'scripts/a.py', {'allowed_helper_imports': []})
    assert result.success


def test_import_guard_allows_docx_stdlib_fallback_without_bound_helper():
    src = """
import zipfile
import xml.etree.ElementTree as ET

def read_docx(path):
    with zipfile.ZipFile(path) as zf:
        xml = zf.read('word/document.xml')
    root = ET.fromstring(xml)
    return ''.join(node.text or '' for node in root.iter())
"""
    result = guard_runtime_imports(src, 'scripts/a.py', {'allowed_helper_imports': []})
    assert result.success


def test_unbound_runtime_helper_is_diagnostic_not_business_failure():
    result = guard_runtime_imports(
        'from backend.services.runtime_tools import read_file_text\n',
        'scripts/a.py',
        {'allowed_helper_imports': []},
    )
    assert result.success
    assert result.error_type == ''
    assert 'read_file_text' in result.forbidden_imports
    assert any('observed import not present in Current File ToolPool contract' in warning for warning in result.warnings)


def test_import_guard_allows_any_bound_import_path_and_function_name():
    src = 'from backend.services.skill_runtime import arbitrary_callable\n'
    result = guard_runtime_imports(
        src,
        'scripts/a.py',
        {
            'allowed_import_paths': ['backend.services.skill_runtime'],
            'allowed_function_imports': ['arbitrary_callable'],
        },
    )
    assert result.success


def test_import_guard_has_no_business_helper_whitelist_or_replacement_table():
    import backend.services.creator.runtime_import_guard as module

    assert not hasattr(module, '_ALLOWED_SKILL_RUNTIME_HELPERS')
    assert not hasattr(module, 'SUGGESTED_REPLACEMENTS')


def test_arbitrary_platform_import_not_hard_killed_by_business_name():
    src = 'from backend.services.skill_runtime import arbitrary_callable\n'
    result = guard_runtime_imports(src, 'scripts/a.py', {'allowed_import_paths': [], 'allowed_function_imports': []})
    assert result.success
    assert result.error_type == ''
