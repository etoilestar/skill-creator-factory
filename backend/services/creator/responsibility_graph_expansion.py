"""Incremental construction of ResponsibilityEdges over frozen FunctionItems.

The backend owns endpoint legality and edge materialization.  The model sees
opaque candidate identifiers and owns only the semantic choice among them.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ..skill_plan import (
    GraphValidationError,
    normalize_structured_function_items,
    structured_responsibility_graph_input_provenance_gaps,
    validate_structured_responsibility_edge_transport,
)


logger = logging.getLogger(__name__)

GRAPH_EXPANSION_BATCH_SIZE = 3
PLATFORM_INPUT_NODE = "platform_input_node"
PLATFORM_OUTPUT_NODE = "platform_output_node"


class ResponsibilityGraphExpansionError(GraphValidationError):
    """A machine-readable binding or selection protocol failure."""


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


def _required_final_outputs(platform_contract: dict[str, Any]) -> list[Any]:
    boundary = _boundary(platform_contract)
    explicit = boundary.get("required_final_output_fields")
    if isinstance(explicit, list):
        return explicit
    # The current runtime contract exposes an ordered set of alternative final
    # slots rather than an explicit required subset.  Its first canonical slot
    # is the required generic terminal until the runtime supplies a subset.
    fields = boundary.get("final_output_fields") or []
    return list(fields[:1]) if isinstance(fields, list) else []


def build_responsibility_binding_obligations(
    *, function_items: list[dict], platform_contract: dict,
) -> list[dict]:
    """Create one open obligation per non-defaulted input and required terminal."""
    normalized = normalize_structured_function_items(function_items, source="graph_expansion")
    obligations: list[dict] = []
    for item in normalized:
        defaults = item.get("default_values") or {}
        for value in item.get("inputs") or []:
            port_id, description, contract = _port(value)
            if not port_id or port_id in defaults:
                continue
            obligations.append({
                "obligation_id": f"O{len(obligations) + 1:04d}",
                "target": {"node_id": item["target_file"], "port_id": port_id},
                "target_context": {
                    "node_purpose": item.get("purpose", ""),
                    "input_description": description,
                    "input_contract": contract,
                },
            })
    for value in _required_final_outputs(platform_contract):
        port_id, description, contract = _port(value)
        if not port_id:
            continue
        obligations.append({
            "obligation_id": f"O{len(obligations) + 1:04d}",
            "target": {"node_id": PLATFORM_OUTPUT_NODE, "port_id": port_id},
            "target_context": {
                "output_description": description,
                "output_contract": contract,
            },
        })
    return obligations


def _would_cycle(edges: list[dict], source: str, target: str) -> bool:
    if source in (PLATFORM_INPUT_NODE, PLATFORM_OUTPUT_NODE) or target == PLATFORM_OUTPUT_NODE:
        return False
    adjacency: dict[str, set[str]] = {}
    for edge in edges:
        left, right = str(edge.get("from_node") or ""), str(edge.get("to_node") or "")
        adjacency.setdefault(left, set()).add(right)
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


def build_legal_sources_for_obligation(
    *, obligation: dict, function_items: list[dict], platform_contract: dict,
    committed_edges: list[dict],
) -> list[dict]:
    """Enumerate only structurally legal declared source endpoints."""
    normalized = normalize_structured_function_items(function_items, source="graph_expansion")
    target = obligation.get("target") or {}
    target_node, target_port = str(target.get("node_id") or ""), str(target.get("port_id") or "")
    item_by_node = {str(item["target_file"]): item for item in normalized}
    boundary = _boundary(platform_contract)
    valid_target = (
        target_node == PLATFORM_OUTPUT_NODE
        and target_port in {_port(value)[0] for value in _required_final_outputs(platform_contract)}
    ) or (
        target_node in item_by_node and target_port in set(item_by_node[target_node].get("inputs") or [])
    )
    if not valid_target:
        return []

    raw_sources: list[tuple[str, str, dict[str, Any]]] = []
    for value in boundary.get("input_envelope_fields") or []:
        port_id, description, contract = _port(value)
        raw_sources.append((PLATFORM_INPUT_NODE, port_id, {
            "output_description": description, "output_contract": contract,
        }))
    for item in normalized:
        for value in item.get("outputs") or []:
            port_id, description, contract = _port(value)
            raw_sources.append((str(item["target_file"]), port_id, {
                "node_purpose": item.get("purpose", ""),
                "output_description": description, "output_contract": contract,
            }))

    target_contract = (obligation.get("target_context") or {}).get("input_contract") or (obligation.get("target_context") or {}).get("output_contract") or {}
    candidates: list[dict] = []
    for source_node, source_port, source_context in raw_sources:
        if not source_port or source_node == target_node:
            continue
        if target_node == PLATFORM_OUTPUT_NODE and source_node == PLATFORM_INPUT_NODE:
            continue
        if _would_cycle(committed_edges, source_node, target_node):
            continue
        source_type = _definite_type(source_context.get("output_contract") or {})
        target_type = _definite_type(target_contract)
        if source_type and target_type and source_type != target_type:
            continue
        candidates.append({
            "source_id": f"{obligation['obligation_id']}-C{len(candidates) + 1:04d}",
            "endpoint": {"node_id": source_node, "port_id": source_port},
            "source_context": source_context,
        })
    return candidates


def validate_selection_protocol(*, obligations: list[dict], response: Any) -> list[dict]:
    """Accept exactly one opaque source selection for every supplied obligation."""
    if not isinstance(response, dict) or set(response) != {"selections"} or not isinstance(response["selections"], list):
        raise ResponsibilityGraphExpansionError("selection response must contain only selections", code="invalid_binding_selection_protocol")
    expected = {item["obligation_id"]: item for item in obligations}
    seen: set[str] = set()
    selections: list[dict] = []
    for selection in response["selections"]:
        if not isinstance(selection, dict) or set(selection) != {"obligation_id", "source_id"}:
            raise ResponsibilityGraphExpansionError("selection must contain only obligation_id and source_id", code="invalid_binding_selection_protocol")
        obligation_id = selection.get("obligation_id")
        if obligation_id not in expected or obligation_id in seen:
            raise ResponsibilityGraphExpansionError("unknown or duplicate obligation_id", code="invalid_binding_selection_protocol", details={"obligation_id": obligation_id})
        legal_ids = {source["source_id"] for source in expected[obligation_id].get("legal_sources") or []}
        if selection.get("source_id") not in legal_ids:
            raise ResponsibilityGraphExpansionError("source_id is outside the legal candidate domain", code="invalid_binding_selection_protocol", details={"obligation_id": obligation_id, "source_id": selection.get("source_id")})
        seen.add(obligation_id)
        selections.append(dict(selection))
    missing = sorted(set(expected) - seen)
    if missing:
        raise ResponsibilityGraphExpansionError("selection response omitted obligations", code="invalid_binding_selection_protocol", details={"missing_obligation_ids": missing})
    return selections


def materialize_selected_edges(*, obligations: list[dict], selections: list[dict]) -> list[dict]:
    """Deterministically expand opaque IDs into the existing edge wire schema."""
    registry = {item["obligation_id"]: item for item in obligations}
    edges: list[dict] = []
    for selection in selections:
        obligation = registry[selection["obligation_id"]]
        source = next(item for item in obligation["legal_sources"] if item["source_id"] == selection["source_id"])
        edges.append({
            "from_node": source["endpoint"]["node_id"],
            "from_output": source["endpoint"]["port_id"],
            "to_node": obligation["target"]["node_id"],
            "to_input": obligation["target"]["port_id"],
            "purpose": "Bind a declared source endpoint to a required target endpoint.",
            "constraints": [],
        })
    return edges


def _validate_acyclic(edges: list[dict]) -> None:
    for edge in edges:
        if _would_cycle([candidate for candidate in edges if candidate is not edge], str(edge["from_node"]), str(edge["to_node"])):
            raise ResponsibilityGraphExpansionError("responsibility graph contains a directed cycle", code="responsibility_graph_cycle")


async def expand_responsibility_graph(
    *, function_items: list[dict], platform_contract: dict, planner_model: str,
    model_call: Callable[[list[dict[str, str]], str], Awaitable[str]] | None = None,
) -> list[dict]:
    """Resolve obligations in bounded batches, retrying only a failing batch once."""
    if model_call is None:
        from ..creator_model_profiles import complete_creator_role_once

        async def model_call(messages: list[dict[str, str]], model: str) -> str:
            return await complete_creator_role_once(messages, "planner", fallback_model=model)

    obligations = build_responsibility_binding_obligations(function_items=function_items, platform_contract=platform_contract)
    logger.info("[Creator][graph_expansion] obligation_count=%d", len(obligations))
    committed: list[dict] = []
    model_calls = protocol_retries = 0
    for offset in range(0, len(obligations), GRAPH_EXPANSION_BATCH_SIZE):
        batch = obligations[offset:offset + GRAPH_EXPANSION_BATCH_SIZE]
        logger.info("[Creator][graph_expansion] batch_index=%d batch_size=%d", offset // GRAPH_EXPANSION_BATCH_SIZE, len(batch))
        enriched: list[dict] = []
        for obligation in batch:
            legal = build_legal_sources_for_obligation(obligation=obligation, function_items=function_items, platform_contract=platform_contract, committed_edges=committed)
            if not legal:
                details = {"issue_type": "unresolved_binding_obligation", "obligation_id": obligation["obligation_id"], "target_node": obligation["target"]["node_id"], "target_port": obligation["target"]["port_id"], "reason": "no structurally legal source exists"}
                logger.info("[Creator][graph_expansion] unresolved_count=1")
                logger.info("[Creator][graph_expansion] graph_valid=false")
                raise ResponsibilityGraphExpansionError(json.dumps(details, ensure_ascii=False), code="unresolved_binding_obligation", details=details)
            enriched.append({**obligation, "legal_sources": legal})
        logger.info("[Creator][graph_expansion] candidate_counts=%s", [len(item["legal_sources"]) for item in enriched])
        error: str | None = None
        for attempt in range(2):
            payload = {"current_partial_graph": {"nodes": [item["target_file"] for item in function_items], "committed_edges": committed}, "obligations": enriched}
            if error:
                payload["protocol_error"] = error
            prompt = """你只做当前批次的语义来源选择。后端已经确定所有合法节点、端口和候选。不要创建、重命名或修改任何结构。不要推断候选集合之外的来源。Return strict JSON containing only selections; each item must contain only obligation_id and source_id. Return every obligation exactly once, with no explanation."""
            text = await model_call([{"role": "system", "content": prompt}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}], planner_model)
            model_calls += 1
            try:
                response = json.loads(text)
                selections = validate_selection_protocol(obligations=enriched, response=response)
                proposed = materialize_selected_edges(obligations=enriched, selections=selections)
                candidate_graph = committed + proposed
                validate_structured_responsibility_edge_transport(candidate_graph, function_items=function_items, source="graph_expansion_batch")
                _validate_acyclic(candidate_graph)
                committed = candidate_graph
                break
            except (ValueError, KeyError, StopIteration, json.JSONDecodeError) as exc:
                error = f"{type(exc).__name__}: {exc}"
                if attempt == 0:
                    protocol_retries += 1
                    continue
                logger.info("[Creator][graph_expansion] model_call_count=%d", model_calls)
                logger.info("[Creator][graph_expansion] protocol_retry_count=%d", protocol_retries)
                logger.info("[Creator][graph_expansion] graph_valid=false")
                raise ResponsibilityGraphExpansionError("graph expansion batch failed after one retry", code="graph_expansion_batch_failed", details={"obligation_ids": [item["obligation_id"] for item in enriched], "validation_error": error}) from exc
        logger.info("[Creator][graph_expansion] committed_edge_count=%d", len(committed))
    validated = validate_structured_responsibility_edge_transport(committed, function_items=function_items, source="graph_expansion_final")
    _validate_acyclic(validated)
    gaps = structured_responsibility_graph_input_provenance_gaps(function_items, validated, source="graph_expansion_final")
    if gaps:
        raise ResponsibilityGraphExpansionError("final graph has unresolved input obligations", code="unresolved_binding_obligation", details={"unresolved": gaps})
    logger.info("[Creator][graph_expansion] unresolved_count=0")
    logger.info("[Creator][graph_expansion] model_call_count=%d", model_calls)
    logger.info("[Creator][graph_expansion] protocol_retry_count=%d", protocol_retries)
    logger.info("[Creator][graph_expansion] graph_valid=true")
    return validated
