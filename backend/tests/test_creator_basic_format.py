from backend.services.creator.basic_format import check_patch_candidate_basic_format


def test_python_syntax_error_basic_format_only():
    failure = check_patch_candidate_basic_format('scripts/a.py', 'def broken(:\n    pass\n')
    assert failure is not None
    assert failure.coarse_failure_kind == 'python_compile_error'
    assert '工具' in failure.instruction


def test_unbound_runtime_import_is_not_basic_format_failure():
    src = 'from backend.services.runtime_tools import made_up_helper\n\ndef main():\n    return made_up_helper()\n'
    assert check_patch_candidate_basic_format('scripts/a.py', src) is None


def test_argv_mismatch_is_not_basic_format_failure():
    src = 'import sys\nprint({"unexpected": sys.argv[1] if len(sys.argv)>1 else ""})\n'
    assert check_patch_candidate_basic_format('scripts/a.py', src) is None


def test_markdown_unclosed_fence_basic_format_failure():
    failure = check_patch_candidate_basic_format('references/a.md', '# Title\n```python\nprint(1)\n')
    assert failure is not None
    assert failure.coarse_failure_kind == 'markdown_basic_format_error'
