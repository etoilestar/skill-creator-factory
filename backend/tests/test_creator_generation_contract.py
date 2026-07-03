from backend.services.creator.generation import _script_local_contract_payload
from backend.services.creator_tool_registry import (
    ToolCapability,
    ToolFunctionManifest,
    clear_registered_tool_capabilities,
    register_tool_capability,
)
from backend.services.skill_plan import SkillPlanEntry


def _entry(**kw):
    data = dict(
        path="scripts/main.py",
        role="generic_script",
        file_type="script",
        purpose="test",
        runtime="python",
        language="python",
        inputs=["payload"],
        outputs=["result"],
        dependencies=[],
        required_capabilities=[],
    )
    data.update(kw)
    return SkillPlanEntry(**data)


def test_script_local_contract_merges_structured_tool_binding_with_explicit_values():
    clear_registered_tool_capabilities()
    register_tool_capability(ToolCapability(
        name="custom_lookup",
        display_name="Custom Lookup",
        category="retrieval",
        roles=["generic_script"],
        dependencies=[{"package": "rich", "imports": ["rich"]}],
        functions=[ToolFunctionManifest(
            function_name="lookup_value",
            import_path="backend.services.runtime_tools.custom_tools.lookup",
            short_description="Lookup a value.",
            when_to_use="Use for lookup.",
            signature="lookup_value(query: str) -> dict",
            input_schema={"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}}},
            output_schema={"type": "object", "required": ["source_value"], "properties": {"source_value": {"type": "string"}}},
            required_capabilities=["custom_lookup"],
        )],
    ))
    try:
        entry = _entry(
            raw_capability_hints=["custom_lookup"],
            runtime_contract={
                "tool_binding_summary": {
                    "primary_tool_ids": ["explicit.primary"],
                    "allowed_import_paths": ["explicit.path"],
                    "allowed_function_imports": ["explicit_fn"],
                    "allowed_helper_imports": ["explicit_helper"],
                    "dependencies": ["explicit_dep"],
                }
            },
        )
        payload = _script_local_contract_payload(
            file_path="scripts/main.py",
            purpose="test",
            plan_entry=entry,
            stdout_schema={"type": "object", "required": ["result"], "properties": {"result": {"type": "string"}}},
        )

        binding = payload["current_file_tool_binding"]
        assert binding["primary_tool_ids"] == ["explicit.primary", "custom_lookup.lookup_value", "script_argv_guard"]
        assert binding["allowed_import_paths"] == ["explicit.path", "backend.services.runtime_tools.custom_tools.lookup"]
        assert binding["allowed_function_imports"] == [
            "explicit_fn",
            "lookup_value",
            "backend.services.runtime_tools.custom_tools.lookup.lookup_value",
        ]
        assert binding["dependencies"] == ["explicit_dep", "rich"]
        assert payload["allowed_helper_imports"] == ["explicit_helper", "strict_json_argv_guard"]
        assert payload["available_tools"] == payload["implementation_resolution"]["available_tools"]
        assert "backend.services.runtime_tools.custom_tools.lookup.lookup_value" in payload["implementation_resolution"]["allowed_imports"]
    finally:
        clear_registered_tool_capabilities()


def test_script_local_contract_autofills_runtime_helper_and_argv_guard_when_binding_empty():
    clear_registered_tool_capabilities()
    register_tool_capability(ToolCapability(
        name="runtime_lookup",
        display_name="Runtime Lookup",
        category="retrieval",
        roles=["generic_script"],
        functions=[ToolFunctionManifest(
            function_name="lookup_value",
            import_path="backend.services.runtime_tools",
            short_description="Lookup a value.",
            when_to_use="Use for lookup.",
            signature="lookup_value(query: str) -> dict",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            required_capabilities=["runtime_lookup"],
        )],
    ))
    try:
        entry = _entry(
            raw_capability_hints=["runtime_lookup"],
            runtime_contract={"tool_binding_summary": {"allowed_helper_imports": []}},
        )
        payload = _script_local_contract_payload(
            file_path="scripts/main.py",
            purpose="test",
            plan_entry=entry,
            stdout_schema={"type": "object", "required": ["result"], "properties": {"result": {"type": "string"}}},
        )

        binding = payload["current_file_tool_binding"]
        assert binding["allowed_helper_imports"] == ["lookup_value", "strict_json_argv_guard"]
        assert binding["allowed_import_paths"] == ["backend.services.runtime_tools"]
        assert binding["allowed_function_imports"] == ["lookup_value", "backend.services.runtime_tools.lookup_value"]
        assert "runtime_lookup.lookup_value" in binding["primary_tool_ids"]
        assert "script_argv_guard" in binding["primary_tool_ids"]
    finally:
        clear_registered_tool_capabilities()
