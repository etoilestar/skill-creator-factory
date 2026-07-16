from pathlib import Path
import json
import sys
from types import SimpleNamespace

from backend.services.creator import e2e
from backend.services.creator.common import E2EWorkflowCommand


def test_e2e_uses_real_subprocess_traceback_instead_of_static_import_guard(tmp_path, monkeypatch):
    skill_dir = tmp_path / "real-subprocess"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "scripts" / "run.py").write_text(
        "from definitely_missing_creator_package import run\nrun()\n",
        encoding="utf-8",
    )
    payload = {"user_request": "hello"}
    raw_payload = json.dumps(payload)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: demo\ndescription: Demo skill\n---\n# Demo\n```bash\npython scripts/run.py '{raw_payload}'\n```\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        e2e,
        "_skill_plan_entry_for_file",
        lambda **kwargs: SimpleNamespace(
            runtime="python",
            language="python",
            role="generic_script",
            file_type="script",
            file_kind="script",
            path="scripts/run.py",
            inputs=["user_request"],
            outputs=["text"],
            dependencies=[],
            required_capabilities=[],
            runtime_contract={"stdout": ["text"]},
            artifact_contract={},
            artifacts=[],
        ),
    )

    errors = e2e._run_skill_workflow_e2e_once("real-subprocess", source_skill_dir=skill_dir)

    joined = "\n".join(errors)
    assert "ModuleNotFoundError" in joined
    assert "script_exit" in joined or "return_code" in joined
    assert "runtime_import_guard_failed" not in joined


def test_execute_python_command_still_returns_real_process_error(tmp_path):
    skill_dir = tmp_path / "direct-subprocess"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "scripts" / "run.py").write_text(
        "from definitely_missing_creator_package import run\nrun()\n",
        encoding="utf-8",
    )
    command = E2EWorkflowCommand(
        1,
        "SKILL.md",
        "scripts/run.py",
        "python scripts/run.py '{}'",
        "python",
        {},
    )

    proc = e2e._execute_e2e_python_command(
        command=command,
        trial_skill_dir=skill_dir,
        rendered_payload={},
        venv_python=Path(sys.executable),
    )

    assert proc.returncode != 0
    assert "ModuleNotFoundError" in proc.stderr
