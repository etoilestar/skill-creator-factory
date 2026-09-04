import pytest

from backend.services.creator.common import (
    graph_interface_contract,
    project_script_interface_contract,
    validate_graph_interface_projection,
)


def graph():
    return {
        "requirements": [{
            "target_file": "scripts/compare_csvs.py",
            "purpose": "Compare CSV files.",
            "inputs": [{"name": "input_files", "type": "list[file_path]", "shape": "list", "required": True, "source": "platform_input"}],
            "outputs": [{"name": "text", "type": "string", "required": True}],
        }],
        "dataflow_edges": [
            {"from_node": "platform_input_node", "from_output": "payload", "to_node": "scripts/compare_csvs.py", "to_input": "input_files", "purpose": "input", "constraints": [], "mapping": {"source": "payload", "target": "input_files", "type": "list[file_path]"}},
            {"from_node": "scripts/compare_csvs.py", "from_output": "text", "to_node": "platform_output_node", "to_input": "text", "purpose": "output", "constraints": [], "mapping": {"source": "text", "target": "text", "type": "string"}},
        ],
    }


def test_graph_contract_preserves_ports_shape_and_explicit_mapping():
    contract = graph_interface_contract(graph())
    assert contract["input_ports"] == [{"name": "input_files", "direction": "input", "type": "list[file_path]", "shape": "list", "required": True, "source": "platform_input", "consumer": "scripts/compare_csvs.py"}]
    assert contract["output_ports"] == [{"name": "text", "direction": "output", "type": "string", "shape": "scalar", "required": True, "producer": "scripts/compare_csvs.py", "terminal": True}]
    assert contract["edge_mappings"][1]["mapping"] == {"source": "text", "target": "text", "type": "string", "conversion": "identity"}


def test_all_derived_contracts_are_exact_graph_projections():
    projected = project_script_interface_contract(graph(), "scripts/compare_csvs.py")
    assert projected["command_payload"] == {"input_files": "{{input_files}}"}
    assert projected["argv_schema"]["properties"]["input_files"]["type"] == "array"
    assert projected["stdout_schema"]["required"] == ["text"]
    validate_graph_interface_projection(graph(), "scripts/compare_csvs.py", projected)
    with pytest.raises(ValueError, match="does not equal"):
        validate_graph_interface_projection(graph(), "scripts/compare_csvs.py", {**projected, "stdout_schema": {}})
