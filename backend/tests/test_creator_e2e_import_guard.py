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
    assert "environment_dependency_prepare_failed" in joined
    assert "definitely_missing_creator_package" in joined
    assert "E2E_REPAIR_TARGET=runtime_environment" in joined
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


def test_creator_e2e_subprocess_consumes_real_local_fixture_but_keeps_api_trial(tmp_path):
    skill_dir = tmp_path / "local-fixture"
    (skill_dir / "scripts").mkdir(parents=True)
    fixture = skill_dir / ".creator_e2e" / "samples" / "input.csv"
    fixture.parent.mkdir(parents=True)
    fixture.write_text("value,score\n1,10\n2,\n3,30\n", encoding="utf-8")
    (skill_dir / "scripts" / "run.py").write_text(
        """import json
import os
import sys
from backend.services.runtime_tools import api_get, read_csv

payload = json.loads(sys.argv[1])
result = read_csv(payload["path"])
print(json.dumps({
    "columns": result["columns"],
    "rows": result["rows"],
    "skill_trial": os.environ.get("SKILL_TRIAL_RUN"),
    "real_local_fixtures": os.environ.get("CREATOR_E2E_REAL_LOCAL_FIXTURES"),
    "api_mock": api_get("https://example.com")["json"]["mock"],
}))
""",
        encoding="utf-8",
    )
    command = E2EWorkflowCommand(
        1, "SKILL.md", "scripts/run.py", "python scripts/run.py ...", "python", {"path": str(fixture)},
    )

    proc = e2e._execute_e2e_python_command(
        command=command,
        trial_skill_dir=skill_dir,
        rendered_payload={"path": str(fixture)},
        venv_python=Path(sys.executable),
    )

    assert proc.returncode == 0, proc.stderr
    output = json.loads(proc.stdout)
    assert output["columns"] == ["value", "score"]
    assert output["rows"] == [
        {"value": "1", "score": "10"},
        {"value": "2", "score": ""},
        {"value": "3", "score": "30"},
    ]
    assert {"A": "mock", "B": "value"} not in output["rows"]
    assert output["skill_trial"] == "1"
    assert output["real_local_fixtures"] == "1"
    assert output["api_mock"] is True
