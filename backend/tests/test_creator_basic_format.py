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


def test_e2e_repair_basic_format_return_protocol_is_dict_source():
    from pathlib import Path
    src = Path('backend/services/creator/e2e.py').read_text(encoding='utf-8')
    assert 'return E2EValidationResult(success=False, errors=[last_failure]' not in src
    assert '"error_type": "basic_format_failed"' in src
    assert '"next_failure": [last_failure]' in src


def test_e2e_repair_stays_localized_source():
    from pathlib import Path
    src = Path('backend/services/creator/e2e.py').read_text(encoding='utf-8')
    assert 'use_full_rewrite = False' in src
    assert 'repair_attempt_counts.get(repair_key, 0) >= 2' not in src
    assert '"repair_mode": "localized_patch"' in src


def test_first_round_basic_format_stage_error_is_coarse():
    from backend.services.creator.api import _post_patch_basic_format_stage_error
    err = _post_patch_basic_format_stage_error('scripts/a.py', 'def broken(:\n    pass\n')
    assert err is not None
    assert err.source == 'basic_format'
    assert err.layer == 'python_compile_error'
    assert 'argv' in err.detail
