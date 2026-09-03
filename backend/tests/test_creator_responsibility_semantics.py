from backend.services.creator.common import FunctionItem, build_function_execution_context
from backend.services.skill_plan import normalize_structured_function_items


def _item(**extra):
    return {
        "target_file": "scripts/run.py",
        "role": "processor",
        "purpose": "Perform the confirmed transformation",
        "inputs": ["source"],
        "outputs": ["result"],
        "required_capabilities": [],
        "constraints": [],
        **extra,
    }


def test_structured_function_item_preserves_execution_semantics():
    semantics = {
        "capabilities": ["parse the declared input"],
        "constraints": ["preserve the confirmed behavior"],
        "expected_behaviors": ["produce the declared result"],
        "verification_points": ["verify responsibility coverage"],
    }

    normalized = normalize_structured_function_items(
        [_item(responsibility_semantics=semantics)]
    )

    assert normalized[0]["responsibility_semantics"] == semantics


def test_execution_context_exposes_semantics_without_inference():
    semantics = {
        "capabilities": ["consume input"],
        "constraints": ["do not invent dependencies"],
        "expected_behaviors": ["return output"],
        "verification_points": ["check output contract"],
    }
    graph = {
        "requirements": [_item(responsibility_semantics=semantics)],
        "dataflow_edges": [],
    }

    context = build_function_execution_context(
        graph=graph,
        target_file="scripts/run.py",
        current_file_tool_binding={},
    )

    assert context["function_item"]["responsibility_semantics"] == semantics


def test_legacy_function_item_gets_empty_semantic_contract():
    item = FunctionItem(**_item())

    assert item.responsibility_semantics.model_dump() == {
        "capabilities": [],
        "constraints": [],
        "expected_behaviors": [],
        "verification_points": [],
    }
