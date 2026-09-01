import asyncio

import pytest

from backend.services.creator.protocol import (
    ContractLifecycle,
    ContractState,
    ErrorKind,
    classify_result,
    parse_structured_output,
    request_structured_output,
    revision_event,
    revision_transitions,
    validate_phase_status,
)


@pytest.mark.parametrize("raw", [
    '{"status":"ready","value":{"x":1}}',
    '```json\n{"status":"ready","value":{"x":1}}\n```',
    '说明文字 {"status":"ready","value":{"text":"a } b"}} 后续文字',
])
def test_parse_structured_output_accepts_supported_model_shapes(raw):
    assert parse_structured_output(raw, phase="blueprint_generation")["status"] == "ready"


def test_structured_output_retries_once_then_returns_protocol_failure():
    prompts = []

    async def call(messages):
        prompts.append(messages)
        return "not json"

    result = asyncio.run(request_structured_output(call, [], phase="graph_generation"))
    assert result == {"status": "generation_failed", "phase": "graph_generation", "reason": "invalid_structured_output"}
    assert len(prompts) == 2
    assert prompts[1][-1]["content"] == "只输出符合schema的JSON，不要解释"


def test_repair_cannot_silently_return_to_clarification():
    result = validate_phase_status({"status": "needs_clarification"}, phase="blueprint_repair")
    assert result == {
        "status": "invalid_phase_transition",
        "current_phase": "blueprint_repair",
        "returned_status": "needs_clarification",
    }
    assert classify_result(result) == ErrorKind.PROTOCOL_ERROR


def test_revision_has_explicit_two_hop_route():
    event = revision_event(source_phase="blueprint_repair", target="blueprint", reason="contract mismatch")
    transitions = revision_transitions(event, contract_id="contract-1")
    assert [item["phase_after"] for item in transitions] == ["revision_requested", "blueprint_generation"]
    assert classify_result(event) == ErrorKind.REVISION_REQUIRED


def test_downstream_consumers_require_frozen_contract():
    contract = ContractLifecycle("contract-1", {"authority": "graph"})
    with pytest.raises(ValueError, match="frozen contract"):
        contract.consume("skill_generation")
    contract.review()
    assert contract.state == ContractState.REVIEWED
    contract.freeze()
    assert contract.consume("e2e_generation") == {"authority": "graph"}
    event = contract.report_problem(source_phase="e2e_generation", target="contract_graph", reason="invalid contract")
    assert event["event"] == "request_revision"
