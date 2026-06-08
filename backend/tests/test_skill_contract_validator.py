from backend.services.skill_contract import WorkflowContract
from backend.services.skill_contract_validator import validate_workflow_contract, validate_stdout_against_output_schema


def test_contract_validates_foreach_array_output():
    contract = WorkflowContract.from_raw({
        "skill_name": "demo",
        "steps": [
            {
                "id": "make_items",
                "script_path": "scripts/make_items.py",
                "role": "text_generator",
                "inputs": {"topic": {"type": "string"}},
                "outputs": {
                    "items": {
                        "type": "array",
                        "min_items": 1,
                        "items": {"type": "object", "required": ["title"]}
                    }
                },
                "command_template": "python scripts/make_items.py '{\"topic\":\"{{topic}}\"}'"
            },
            {
                "id": "use_item",
                "script_path": "scripts/use_item.py",
                "role": "generic_script",
                "foreach": {"collection": "make_items.items", "item_name": "item"},
                "inputs": {"title": {"type": "string", "source": "item.title"}},
                "outputs": {"result": {"type": "string"}},
                "command_template": "python scripts/use_item.py '{\"title\":\"{{title}}\"}'"
            }
        ]
    })
    issues = validate_workflow_contract(contract)
    assert [x for x in issues if x.severity == "error"] == []


def test_stdout_schema_rejects_empty_foreach_output():
    step = WorkflowContract.from_raw({
        "skill_name": "demo",
        "steps": [{
            "id": "make_items",
            "script_path": "scripts/make_items.py",
            "outputs": {"items": {"type": "array", "min_items": 1}}
        }]
    }).steps[0]
    issues = validate_stdout_against_output_schema({"items": []}, step)
    assert any(i.code == "stdout_array_too_short" for i in issues)
