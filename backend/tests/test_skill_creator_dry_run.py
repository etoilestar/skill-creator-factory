import json
from pathlib import Path

from backend.services.skill_contract import WorkflowContract
from backend.services.skill_creator_dry_run import run_creator_workflow_dry_run


def test_creator_workflow_dry_run(tmp_path: Path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()

    (scripts / "make.py").write_text(
        "import json, sys\n"
        "p=json.loads(sys.argv[1])\n"
        "print(json.dumps({'items':[{'title':'A'},{'title':'B'}]}, ensure_ascii=False))\n",
        encoding="utf-8",
    )
    (scripts / "use.py").write_text(
        "import json, sys\n"
        "p=json.loads(sys.argv[1])\n"
        "print(json.dumps({'path':'out_'+p['title']+'.txt'}, ensure_ascii=False))\n",
        encoding="utf-8",
    )

    contract = WorkflowContract.from_raw({
        "skill_name": "demo",
        "steps": [
            {
                "id": "make",
                "script_path": "scripts/make.py",
                "inputs": {"topic": {"type": "string"}},
                "outputs": {
                    "items": {
                        "type": "array",
                        "min_items": 1,
                        "items": {"type": "object", "required": ["title"]},
                    }
                },
                "command_template": "python scripts/make.py '{\"topic\":\"{{topic}}\"}'",
            },
            {
                "id": "use",
                "script_path": "scripts/use.py",
                "foreach": {"collection": "make.items", "item_name": "item"},
                "inputs": {"title": {"type": "string", "source": "item.title"}},
                "outputs": {"path": {"type": "string"}},
                "command_template": "python scripts/use.py '{\"title\":\"{{title}}\"}'",
            },
        ],
    })

    result = run_creator_workflow_dry_run(skill_dir=tmp_path, contract=contract, sample_input={"topic": "x"})
    assert result.ok
    assert len(result.traces) == 3
