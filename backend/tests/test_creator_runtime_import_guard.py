from backend.services.creator.runtime_import_guard import guard_runtime_imports


def test_import_guard_blocks_invented_helpers():
    result = guard_runtime_imports('from backend.services.runtime_tools import read_pdf_text\n', 'scripts/a.py', {'allowed_helper_imports': ['extract_pdf_text']})
    assert not result.success
    assert result.error_type == 'generated_unknown_runtime_tool_import'
    assert 'read_pdf_text' in result.missing_imports
    assert 'extract_pdf_text' in result.suggested_replacements


def test_import_guard_blocks_star_and_pool_forbidden():
    result = guard_runtime_imports('from backend.services.runtime_tools import *\n', 'scripts/a.py', {'allowed_helper_imports': ['extract_pdf_text']})
    assert not result.success
    assert '*' in result.forbidden_imports


def test_import_guard_allows_bound_helper():
    result = guard_runtime_imports('from backend.services.runtime_tools import read_file_text\n', 'scripts/a.py', {'allowed_helper_imports': ['read_file_text']})
    assert result.success


def test_import_guard_allows_bound_custom_tool():
    src = 'from backend.services.runtime_tools.custom_tools.pdf_to_md_mineru import pdf_to_md_mineru\n'
    result = guard_runtime_imports(src, 'scripts/a.py', {'allowed_import_paths': ['backend.services.runtime_tools.custom_tools.pdf_to_md_mineru'], 'allowed_function_imports': ['pdf_to_md_mineru']})
    assert result.success


def test_import_guard_blocks_unbound_custom_tool_and_wildcard():
    src = 'from backend.services.runtime_tools.custom_tools.pdf_to_md_mineru import pdf_to_md_mineru\n'
    result = guard_runtime_imports(src, 'scripts/a.py', {'allowed_import_paths': [], 'allowed_function_imports': []})
    assert not result.success
    assert result.error_type == 'generated_pool_forbidden_custom_tool_import'
    wildcard = guard_runtime_imports('from backend.services.runtime_tools.custom_tools.pdf_to_md_mineru import *\n', 'scripts/a.py', {'allowed_import_paths': ['backend.services.runtime_tools.custom_tools.pdf_to_md_mineru'], 'allowed_function_imports': ['pdf_to_md_mineru']})
    assert not wildcard.success
    assert wildcard.error_type == 'generated_custom_tool_wildcard_import'


def test_import_guard_blocks_pool_external_custom_tool():
    src = 'from backend.services.runtime_tools.custom_tools.https_google_serper_dev_search import https_google_serper_dev_search\n'
    result = guard_runtime_imports(src, 'scripts/a.py', {'allowed_import_paths': ['backend.services.runtime_tools.custom_tools.pdf_to_md_mineru'], 'allowed_function_imports': ['pdf_to_md_mineru']})
    assert not result.success
    assert result.error_type == 'generated_pool_forbidden_custom_tool_import'


def test_import_guard_blocks_unbound_read_file_text():
    result = guard_runtime_imports(
        'from backend.services.runtime_tools import read_file_text\n',
        'scripts/a.py',
        {'allowed_helper_imports': []},
    )
    assert not result.success
    assert result.error_type == 'generated_pool_forbidden_import'
    assert 'read_file_text' in result.forbidden_imports


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


def test_forbidden_runtime_helper_repair_instruction_mentions_stdlib_fallback():
    result = guard_runtime_imports(
        'from backend.services.runtime_tools import read_file_text\n',
        'scripts/a.py',
        {'allowed_helper_imports': []},
    )
    assert 'standard library' in result.repair_instruction
    assert 'tool_pool_patch' in result.repair_instruction
