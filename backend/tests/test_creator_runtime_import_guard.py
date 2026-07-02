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
