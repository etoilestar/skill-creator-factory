import inspect

from backend.services.creator import e2e


def test_e2e_runs_import_guard_before_python_script_execution():
    source = inspect.getsource(e2e._run_skill_workflow_e2e_once)
    guard_pos = source.index('guard_runtime_imports(content, command.script_path, file_binding)')
    run_pos = source.index('_execute_e2e_python_command(')
    assert guard_pos < run_pos
    assert 'runtime_import_guard_failed' in source
