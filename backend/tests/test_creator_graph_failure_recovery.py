import pytest

from backend.services.creator.api import (
    _build_responsibility_graph_construction_context,
    _graph_failure_fingerprint,
    _graph_issue_from_validation_error,
    _should_escalate_graph_failure,
)
from backend.services.skill_plan import GraphValidationError, validate_structured_responsibility_edge_transport


def _unresolved_issue():
    return {
        "category": "unresolved_input_provenance",
        "target_file": "scripts/b.py",
        "target_input": "x",
        "reason": "wording is deliberately ignored",
    }


def test_graph_failure_fingerprint_uses_only_structural_facts():
    first = _graph_failure_fingerprint(_unresolved_issue())
    changed_message = {**_unresolved_issue(), "reason": "different LLM prose", "purpose": "ignored"}

    assert first == "unresolved_input_provenance|target_file|scripts/b.py|target_input|x"
    assert _graph_failure_fingerprint(changed_message) == first


def test_invalid_endpoint_is_graph_local_and_does_not_escalate():
    issue = _graph_issue_from_validation_error(GraphValidationError(
        "wording A", code="invalid_graph_endpoint",
        details={"edge_index": 0, "to_node": "platform_output_contract"},
    ))
    fingerprint = _graph_failure_fingerprint(issue)

    assert issue["category"] == "invalid_graph_endpoint"
    assert not _should_escalate_graph_failure([fingerprint] * 3, [issue])

    changed_message = _graph_issue_from_validation_error(GraphValidationError(
        "wording B", code="invalid_graph_endpoint",
        details={"edge_index": 0, "to_node": "platform_output_contract"},
    ))
    assert _graph_failure_fingerprint(changed_message) == fingerprint


def test_unresolved_input_waits_for_repair_and_regeneration_before_escalation():
    issue = _unresolved_issue()
    fingerprint = _graph_failure_fingerprint(issue)

    assert not _should_escalate_graph_failure([fingerprint], [issue])
    assert not _should_escalate_graph_failure([fingerprint, fingerprint], [issue])
    assert _should_escalate_graph_failure([fingerprint] * 3, [issue])


def test_changed_blocking_fingerprint_is_progress_not_blueprint_escalation():
    issue = _unresolved_issue()
    fingerprints = [
        _graph_failure_fingerprint(issue),
        _graph_failure_fingerprint(issue),
        _graph_failure_fingerprint({**issue, "target_input": "y"}),
    ]

    assert not _should_escalate_graph_failure(fingerprints, [{**issue, "target_input": "y"}])


def test_graph_construction_context_exposes_exact_backend_endpoint_domain():
    context = _build_responsibility_graph_construction_context(
        frozen_blueprint_text="unused when frozen items are supplied",
        allowed_function_item_targets=["scripts/a.py"],
        function_items=[{
            "target_file": "scripts/a.py",
            "role": "worker",
            "purpose": "generic",
            "inputs": ["source"],
            "outputs": ["result"],
            "constraints": [],
            "required_capabilities": [],
        }],
    )

    assert context["allowed_nodes"] == [
        "platform_input_node", "scripts/a.py", "platform_output_node"
    ]
    assert context["allowed_outputs_by_node"]["scripts/a.py"] == ["result"]
    assert context["allowed_inputs_by_node"]["scripts/a.py"] == ["source"]


def test_unknown_platform_node_is_rejected_without_fuzzy_correction():
    items = [{
        "target_file": "scripts/a.py",
        "role": "worker",
        "purpose": "generic",
        "inputs": ["source"],
        "outputs": ["result"],
        "constraints": [],
        "required_capabilities": [],
    }]
    edges = [{
        "from_node": "scripts/a.py",
        "from_output": "result",
        "to_node": "platform_output_contract",
        "to_input": "artifacts",
        "purpose": "transport",
        "constraints": [],
    }]

    with pytest.raises(ValueError, match="non-FunctionItem target endpoint"):
        validate_structured_responsibility_edge_transport(edges, function_items=items)
