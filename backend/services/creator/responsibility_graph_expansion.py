"""Goal-driven ResponsibilityGraph expansion over immutable FunctionItems.

The model only selects opaque identifiers. Endpoint domains, edge materialization,
transactional validation, and activation state are owned by the backend.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..skill_plan import (
    GraphValidationError,
    normalize_structured_function_items,
    validate_structured_responsibility_edge_transport,
)

logger = logging.getLogger(__name__)
PLATFORM_INPUT_NODE = "platform_input_node"
PLATFORM_OUTPUT_NODE = "platform_output_node"
_EDGE_PURPOSE = "Bind a declared source endpoint to a required target endpoint."
ModelCall = Callable[[list[dict[str, str]], str], Awaitable[str]]


class ResponsibilityGraphExpansionError(GraphValidationError):
    """A machine-readable terminal, binding, or graph validation failure."""


@dataclass
class GraphExpansionState:
    """The single authority for a goal-driven graph expansion."""

    active_nodes: set[str] = field(default_factory=set)
    committed_edges: list[dict] = field(default_factory=list)
    frontier: deque[dict] = field(default_factory=deque)
    resolved_obligation_ids: set[str] = field(default_factory=set)
    enqueued_inputs: set[tuple[str, str]] = field(default_factory=set)
    activation_order: list[str] = field(default_factory=list)
    next_obligation_number: int = 1
    terminal_binding_ids: list[str] = field(default_factory=list)
    inactive_function_items: list[str] = field(default_factory=list)


def _boundary(platform_contract: dict[str, Any]) -> dict[str, Any]:
    value = platform_contract.get("platform_skill_boundary", platform_contract)
    return value if isinstance(value, dict) else {}


def _port(value: Any) -> tuple[str, str, dict[str, Any]]:
    if isinstance(value, dict):
        port_id = str(value.get("port_id") or value.get("id") or value.get("name") or "").strip()
        description = str(value.get("description") or "")
        contract = value.get("contract") if isinstance(value.get("contract"), dict) else {}
        return port_id, description, dict(contract)
    return str(value or "").strip(), "", {}


def _final_output_fields(platform_contract: dict[str, Any]) -> list[Any]:
    boundary = _boundary(platform_contract)
    required = boundary.get("required_final_output_fields")
    if isinstance(required, list):
        return list(required)
    fields = boundary.get("final_output_fields")
    return list(fields) if isinstance(fields, list) else []


def _edge(from_node: str, from_output: str, to_node: str, to_input: str) -> dict:
    return {
        "from_node": from_node,
        "from_output": from_output,
        "to_node": to_node,
        "to_input": to_input,
        "purpose": _EDGE_PURPOSE,
        "constraints": [],
    }


def build_terminal_binding_candidates(
    *, function_items: list[dict], platform_contract: dict,
) -> list[dict]:
    """Build the declared FunctionItem-output × platform-terminal product."""
    items = normalize_structured_function_items(function_items, source="graph_expansion")
    boundary = _boundary(platform_contract)
    required_targets = boundary.get("required_final_output_fields")
    has_explicit_required_targets = isinstance(required_targets, list)
    targets = _final_output_fields(platform_contract)
    candidates: list[dict] = []
    for item in items:
        for raw_output in item.get("outputs") or []:
            output_name, description, output_contract = _port(raw_output)
            if not output_name:
                continue
            for raw_target in targets:
                target_name, target_description, target_contract = _port(raw_target)
                if not target_name or _types_conflict(output_contract, target_contract):
                    continue
                candidates.append({
                    "binding_id": f"T{len(candidates) + 1:04d}",
                    "source": {"node_id": item["target_file"], "port_id": output_name},
                    "target": {"node_id": PLATFORM_OUTPUT_NODE, "port_id": target_name},
                    "target_required": has_explicit_required_targets,
                    "edge": _edge(str(item["target_file"]), output_name, PLATFORM_OUTPUT_NODE, target_name),
                    "source_context": {
                        "node_purpose": item.get("purpose", ""),
                        "output_name": output_name,
                        "output_description": description,
                        "output_contract": output_contract,
                    },
                    "target_context": {
                        "platform_output_field": target_name,
                        "output_description": target_description,
                        "output_contract": target_contract,
                    },
                })
    return candidates


def _parse_json_object(text: str, *, code: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ResponsibilityGraphExpansionError("model response must be strict JSON", code=code) from exc
    if not isinstance(value, dict):
        raise ResponsibilityGraphExpansionError("model response must be a JSON object", code=code)
    return value


def validate_terminal_selection_protocol(*, candidates: list[dict], response: Any) -> list[str]:
    if not isinstance(response, dict) or set(response) != {"terminal_binding_ids"}:
        raise ResponsibilityGraphExpansionError("terminal response must contain only terminal_binding_ids", code="invalid_terminal_selection_protocol")
    ids = response.get("terminal_binding_ids")
    if not isinstance(ids, list) or not ids or not all(isinstance(value, str) for value in ids):
        raise ResponsibilityGraphExpansionError("terminal_binding_ids must be a non-empty string list", code="invalid_terminal_selection_protocol")
    if len(ids) != len(set(ids)):
        raise ResponsibilityGraphExpansionError("terminal_binding_ids must not contain duplicates", code="invalid_terminal_selection_protocol")
    registry = {candidate["binding_id"]: candidate for candidate in candidates}
    unknown = [value for value in ids if value not in registry]
    if unknown:
        raise ResponsibilityGraphExpansionError("terminal binding is outside the candidate domain", code="invalid_terminal_selection_protocol", details={"unknown_binding_ids": unknown})
    target_slots = [registry[value]["target"]["port_id"] for value in ids]
    if len(target_slots) != len(set(target_slots)):
        raise ResponsibilityGraphExpansionError("a platform output slot may have only one source", code="invalid_terminal_selection_protocol")
    required_slots = {
        candidate["target"]["port_id"]
        for candidate in candidates
        if candidate.get("target_required")
    }
    missing_required_slots = sorted(required_slots - set(target_slots))
    if missing_required_slots:
        raise ResponsibilityGraphExpansionError(
            "terminal selection does not cover every required platform output",
            code="missing_required_terminal_binding",
            details={"missing_required_final_output_fields": missing_required_slots},
        )
    return ids


async def select_terminal_bindings(
    *, candidates: list[dict], goal_context: dict, planner_model: str,
    model_call: ModelCall,
) -> list[str]:
    """Ask the planner for the smallest goal-satisfying terminal set."""
    if not candidates:
        raise ResponsibilityGraphExpansionError("no terminal binding candidate exists", code="unresolved_terminal_binding", details={"issue_type": "unresolved_terminal_binding"})
    public_candidates = [
        {"binding_id": item["binding_id"], "source_context": item["source_context"], "target_context": item["target_context"]}
        for item in candidates
    ]
    prompt = (
        "Select the minimum terminal binding set that satisfies the user's final delivery goal. "
        "The backend owns every legal endpoint and binding. Return strict JSON containing only "
        "terminal_binding_ids. Do not return nodes, ports, edges, explanations, or IDs outside the candidates."
    )
    payload: dict[str, Any] = {
        "goal_context": goal_context,
        "terminal_candidates": public_candidates,
    }
    for attempt in range(2):
        text = await model_call([
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ], planner_model)
        try:
            return validate_terminal_selection_protocol(
                candidates=candidates,
                response=_parse_json_object(text, code="invalid_terminal_selection_protocol"),
            )
        except ValueError as exc:
            if attempt:
                raise ResponsibilityGraphExpansionError(
                    "terminal selection failed after one retry",
                    code="terminal_selection_failed",
                    details={"validation_error": str(exc)},
                ) from exc
            payload["protocol_error"] = {
                "code": getattr(exc, "code", "invalid_terminal_selection_protocol"),
                "details": getattr(exc, "details", {}),
            }
    raise AssertionError("unreachable terminal selection loop")


def enqueue_required_inputs_for_node(
    *, node_id: str, function_items: list[dict], state: GraphExpansionState,
) -> None:
    """Append unresolved non-default inputs of one newly active node."""
    if node_id not in state.active_nodes:
        return
    items = normalize_structured_function_items(function_items, source="graph_expansion")
    item = next((value for value in items if value["target_file"] == node_id), None)
    if item is None:
        return
    defaults = item.get("default_values") or {}
    incoming = {(edge["to_node"], edge["to_input"]) for edge in state.committed_edges}
    for input_name in item.get("inputs") or []:
        key = (node_id, str(input_name))
        if input_name in defaults or key in incoming or key in state.enqueued_inputs:
            continue
        state.frontier.append({
            "obligation_id": f"O{state.next_obligation_number:04d}",
            "target": {"node_id": node_id, "port_id": str(input_name)},
            "target_context": {"node_purpose": item.get("purpose", ""), "input_name": str(input_name), "input_contract": {}},
        })
        state.next_obligation_number += 1
        state.enqueued_inputs.add(key)


def initialize_state_from_terminals(
    *, terminal_ids: list[str], candidates: list[dict], function_items: list[dict],
) -> GraphExpansionState:
    registry = {candidate["binding_id"]: candidate for candidate in candidates}
    state = GraphExpansionState(terminal_binding_ids=list(terminal_ids))
    for binding_id in terminal_ids:
        candidate = registry[binding_id]
        node_id = candidate["source"]["node_id"]
        state.committed_edges.append(dict(candidate["edge"]))
        if node_id not in state.active_nodes:
            state.active_nodes.add(node_id)
            state.activation_order.append(node_id)
            enqueue_required_inputs_for_node(node_id=node_id, function_items=function_items, state=state)
    return state


def _would_cycle(edges: list[dict], source: str, target: str) -> bool:
    if source == PLATFORM_INPUT_NODE or target == PLATFORM_OUTPUT_NODE:
        return False
    adjacency: dict[str, set[str]] = {}
    for edge in edges:
        adjacency.setdefault(str(edge["from_node"]), set()).add(str(edge["to_node"]))
    pending, seen = [target], set()
    while pending:
        node = pending.pop()
        if node == source:
            return True
        if node not in seen:
            seen.add(node)
            pending.extend(adjacency.get(node, ()))
    return False


def _definite_type(contract: dict[str, Any]) -> str | None:
    value = contract.get("type") if isinstance(contract, dict) else None
    if isinstance(value, str) and value.strip().lower() not in {"", "unknown", "any", "object"}:
        return value.strip().lower()
    return None


def _types_conflict(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_type, right_type = _definite_type(left), _definite_type(right)
    return bool(left_type and right_type and left_type != right_type)


def build_binding_candidates_for_obligation(
    *, obligation: dict, function_items: list[dict], platform_contract: dict,
    state: GraphExpansionState,
) -> list[dict]:
    """Build backend-owned complete edges for one current frontier input."""
    items = normalize_structured_function_items(function_items, source="graph_expansion")
    item_by_node = {str(item["target_file"]): item for item in items}
    target = obligation.get("target") or {}
    target_node, target_port = str(target.get("node_id") or ""), str(target.get("port_id") or "")
    if target_node not in state.active_nodes or target_node not in item_by_node or target_port not in item_by_node[target_node]["inputs"]:
        return []
    target_contract = (obligation.get("target_context") or {}).get("input_contract") or {}
    sources: list[tuple[str, str, dict[str, Any]]] = []
    for raw_port in _boundary(platform_contract).get("input_envelope_fields") or []:
        port_id, description, contract = _port(raw_port)
        sources.append((PLATFORM_INPUT_NODE, port_id, {
            "platform_input_field": port_id,
            "output_description": description,
            "output_contract": contract,
        }))
    for item in items:
        for raw_port in item.get("outputs") or []:
            port_id, description, contract = _port(raw_port)
            sources.append((str(item["target_file"]), port_id, {
                "node_purpose": item.get("purpose", ""), "output_name": port_id,
                "output_description": description, "output_contract": contract,
            }))
    candidates: list[dict] = []
    for source_node, source_port, source_context in sources:
        if not source_port or source_node == target_node:
            continue
        if _would_cycle(state.committed_edges, source_node, target_node):
            continue
        if _types_conflict(source_context.get("output_contract") or {}, target_contract):
            continue
        candidates.append({
            "candidate_id": f"{obligation['obligation_id']}-C{len(candidates) + 1:04d}",
            "edge": _edge(source_node, source_port, target_node, target_port),
            "source_context": source_context,
            "target_context": dict(obligation.get("target_context") or {}),
        })
    return candidates


def validate_binding_selection_protocol(*, obligation: dict, candidates: list[dict], response: Any) -> str:
    if not isinstance(response, dict) or set(response) != {"selection"} or not isinstance(response["selection"], dict):
        raise ResponsibilityGraphExpansionError("binding response must contain only selection", code="invalid_binding_selection_protocol")
    selection = response["selection"]
    if set(selection) != {"obligation_id", "candidate_id"} or selection.get("obligation_id") != obligation.get("obligation_id"):
        raise ResponsibilityGraphExpansionError("selection must identify only the current obligation and candidate", code="invalid_binding_selection_protocol")
    candidate_id = selection.get("candidate_id")
    if candidate_id not in {candidate["candidate_id"] for candidate in candidates}:
        raise ResponsibilityGraphExpansionError("candidate_id is outside the legal candidate domain", code="invalid_binding_selection_protocol", details={"candidate_id": candidate_id})
    return str(candidate_id)


async def _select_binding_candidate(
    *, obligation: dict, candidates: list[dict], goal_context: dict,
    committed_edges: list[dict], planner_model: str, model_call: ModelCall,
    validation_issue: dict[str, Any] | None = None,
) -> str:
    public_candidates = [
        {"candidate_id": item["candidate_id"], "source_context": item["source_context"], "target_context": item["target_context"]}
        for item in candidates
    ]
    payload: dict[str, Any] = {
        "goal_context": goal_context,
        "current_partial_graph": {"committed_edges": committed_edges},
        "obligation": obligation,
        "binding_candidates": public_candidates,
    }
    if validation_issue:
        payload.update(validation_issue)
    prompt = (
        "Select one semantic source for only the current input obligation. The backend has fixed all legal candidates. "
        "Return strict JSON containing only selection with obligation_id and candidate_id. Do not create or modify "
        "nodes, ports, edges, candidates, or committed structure; do not return explanations."
    )
    text = await model_call([
        {"role": "system", "content": prompt},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ], planner_model)
    return validate_binding_selection_protocol(
        obligation=obligation, candidates=candidates,
        response=_parse_json_object(text, code="invalid_binding_selection_protocol"),
    )


def _validate_transaction(candidate_graph: list[dict], function_items: list[dict]) -> None:
    validate_structured_responsibility_edge_transport(candidate_graph, function_items=function_items, source="graph_expansion")
    for edge in candidate_graph:
        others = [value for value in candidate_graph if value is not edge]
        if _would_cycle(others, str(edge["from_node"]), str(edge["to_node"])):
            raise ResponsibilityGraphExpansionError("responsibility graph contains a directed cycle", code="responsibility_graph_cycle")
    incoming: set[tuple[str, str]] = set()
    for edge in candidate_graph:
        if edge["to_node"] == PLATFORM_OUTPUT_NODE:
            continue
        key = (str(edge["to_node"]), str(edge["to_input"]))
        if key in incoming:
            raise ResponsibilityGraphExpansionError("input has duplicate provenance", code="duplicate_input_provenance")
        incoming.add(key)


def _finalize_graph(
    *, state: GraphExpansionState, function_items: list[dict], platform_contract: dict,
    terminal_candidates: list[dict],
) -> list[dict]:
    _validate_transaction(state.committed_edges, function_items)
    terminal_registry = {candidate["binding_id"]: candidate["edge"] for candidate in terminal_candidates}
    expected_terminals = [terminal_registry[value] for value in state.terminal_binding_ids]
    actual_terminals = [edge for edge in state.committed_edges if edge["to_node"] == PLATFORM_OUTPUT_NODE]
    if actual_terminals != expected_terminals or not actual_terminals:
        raise ResponsibilityGraphExpansionError("final terminal set changed during expansion", code="invalid_terminal_closure")
    items = {item["target_file"]: item for item in normalize_structured_function_items(function_items, source="graph_expansion")}
    incoming = {(edge["to_node"], edge["to_input"]) for edge in state.committed_edges}
    unresolved = [
        (node, input_name)
        for node in state.activation_order
        for input_name in items[node]["inputs"]
        if input_name not in (items[node].get("default_values") or {}) and (node, input_name) not in incoming
    ]
    if unresolved:
        raise ResponsibilityGraphExpansionError("active graph has unresolved required inputs", code="unresolved_binding_obligation", details={"unresolved": unresolved})
    reverse: dict[str, set[str]] = {}
    for edge in state.committed_edges:
        reverse.setdefault(str(edge["from_node"]), set()).add(str(edge["to_node"]))
    for node in state.active_nodes:
        pending, seen, reachable = [node], set(), False
        while pending:
            current = pending.pop()
            if current == PLATFORM_OUTPUT_NODE:
                reachable = True
                break
            if current not in seen:
                seen.add(current)
                pending.extend(reverse.get(current, ()))
        if not reachable:
            raise ResponsibilityGraphExpansionError("active node cannot reach a platform terminal", code="inactive_graph_component", details={"target": node})
    state.inactive_function_items = sorted(set(items) - state.active_nodes)
    return list(state.committed_edges)


async def expand_responsibility_graph(
    *, function_items: list[dict], platform_contract: dict, planner_model: str,
    goal_context: dict | None = None, model_call: ModelCall | None = None,
) -> list[dict]:
    """Select terminals, then close activated inputs one at a time in reverse."""
    if model_call is None:
        from ..creator_model_profiles import complete_creator_role_once

        async def model_call(messages: list[dict[str, str]], model: str) -> str:
            return await complete_creator_role_once(messages, "planner", fallback_model=model)

    normalized = normalize_structured_function_items(function_items, source="graph_expansion")
    context = dict(goal_context or {})
    terminal_candidates = build_terminal_binding_candidates(function_items=normalized, platform_contract=platform_contract)
    logger.info("[Creator][graph_expansion] terminal_candidate_count=%d", len(terminal_candidates))
    model_call_count = 0
    selection_retry_count = 0

    async def counted_model_call(messages: list[dict[str, str]], model: str) -> str:
        nonlocal model_call_count
        model_call_count += 1
        return await model_call(messages, model)

    try:
        calls_before_terminal_selection = model_call_count
        try:
            terminal_ids = await select_terminal_bindings(
                candidates=terminal_candidates,
                goal_context=context,
                planner_model=planner_model,
                model_call=counted_model_call,
            )
        finally:
            selection_retry_count += max(
                0, model_call_count - calls_before_terminal_selection - 1
            )
        logger.info("[Creator][graph_expansion] selected_terminal_binding_ids=%s", terminal_ids)
        state = initialize_state_from_terminals(terminal_ids=terminal_ids, candidates=terminal_candidates, function_items=normalized)
        logger.info("[Creator][graph_expansion] active_node_count=%d", len(state.active_nodes))
        logger.info("[Creator][graph_expansion] frontier_size=%d", len(state.frontier))
        while state.frontier:
            obligation = state.frontier.popleft()
            logger.info("[Creator][graph_expansion] resolving_obligation_id=%s", obligation["obligation_id"])
            candidates = build_binding_candidates_for_obligation(
                obligation=obligation, function_items=normalized,
                platform_contract=platform_contract, state=state,
            )
            logger.info("[Creator][graph_expansion] candidate_count=%d", len(candidates))
            if not candidates:
                details = {"issue_type": "unresolved_binding_obligation", "obligation_id": obligation["obligation_id"], "target_node": obligation["target"]["node_id"], "target_port": obligation["target"]["port_id"], "reason": "no structurally legal source exists"}
                raise ResponsibilityGraphExpansionError(json.dumps(details, ensure_ascii=False), code="unresolved_binding_obligation", details=details)
            issue = None
            candidate_id = ""
            for attempt in range(2):
                try:
                    candidate_id = await _select_binding_candidate(
                        obligation=obligation, candidates=candidates, goal_context=context,
                        committed_edges=state.committed_edges, planner_model=planner_model,
                        model_call=counted_model_call, validation_issue=issue,
                    )
                    candidate = next(value for value in candidates if value["candidate_id"] == candidate_id)
                    _validate_transaction(state.committed_edges + [candidate["edge"]], normalized)
                except ValueError as exc:
                    # A protocol or structural failure is local to this input;
                    # committed state remains untouched until validation passes.
                    if attempt:
                        raise ResponsibilityGraphExpansionError("current obligation failed after one retry", code="graph_expansion_selection_failed", details={"obligation_id": obligation["obligation_id"], "validation_error": str(exc)}) from exc
                    selection_retry_count += 1
                    issue = {
                        "previous_candidate_id": candidate_id,
                        "validation_issue": {"code": getattr(exc, "code", "invalid_edge_transport"), "affected_obligation_id": obligation["obligation_id"]},
                    }
                    continue
                state.committed_edges.append(dict(candidate["edge"]))
                state.resolved_obligation_ids.add(obligation["obligation_id"])
                source_node = candidate["edge"]["from_node"]
                if source_node != PLATFORM_INPUT_NODE and source_node not in state.active_nodes:
                    state.active_nodes.add(source_node)
                    state.activation_order.append(source_node)
                    enqueue_required_inputs_for_node(node_id=source_node, function_items=normalized, state=state)
                    logger.info("[Creator][graph_expansion] activated_node=%s", source_node)
                break
            logger.info("[Creator][graph_expansion] committed_edge_count=%d", len(state.committed_edges))
            logger.info("[Creator][graph_expansion] active_node_count=%d", len(state.active_nodes))
            logger.info("[Creator][graph_expansion] frontier_size=%d", len(state.frontier))
        edges = _finalize_graph(state=state, function_items=normalized, platform_contract=platform_contract, terminal_candidates=terminal_candidates)
        diagnostic = {"issue_type": "inactive_frozen_function_items", "targets": state.inactive_function_items, "reason": "Frozen FunctionItems were not selected on any path to a required terminal."}
        logger.info("[Creator][graph_expansion] inactive_function_items=%s", json.dumps(diagnostic, ensure_ascii=False))
        if state.inactive_function_items:
            raise ResponsibilityGraphExpansionError(
                json.dumps(diagnostic, ensure_ascii=False),
                code="inactive_frozen_function_items",
                details=diagnostic,
            )
        logger.info("[Creator][graph_expansion] model_call_count=%d", model_call_count)
        logger.info("[Creator][graph_expansion] selection_retry_count=%d", selection_retry_count)
        logger.info("[Creator][graph_expansion] graph_valid=true")
        return edges
    except Exception:
        logger.info("[Creator][graph_expansion] model_call_count=%d", model_call_count)
        logger.info("[Creator][graph_expansion] selection_retry_count=%d", selection_retry_count)
        logger.info("[Creator][graph_expansion] graph_valid=false")
        raise
