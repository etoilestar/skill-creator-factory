from backend.services.creator.tool_pool_builder import build_tool_pool
from backend.services.creator.tool_pool_models import ToolPoolModel, ToolPoolTool
from backend.services.creator.tool_pool_store import save_tool_pool, load_tool_pool, get_file_binding
from backend.services.creator.generation import _build_script_generate_file_prompt_variant


def _allowed_pool() -> ToolPoolModel:
    return ToolPoolModel(
        skill_name='x',
        tools=[
            ToolPoolTool(
                tool_id='unified_file_text_read',
                status='allowed',
                allowed_helper_imports=['read_file_text'],
                allowed_import_paths=['backend.services.runtime_tools'],
                allowed_function_imports=['read_file_text'],
            ),
            ToolPoolTool(
                tool_id='pdf_to_md_mineru',
                status='allowed',
                allowed_import_paths=['backend.services.runtime_tools.custom_tools.pdf_to_md_mineru'],
                allowed_function_imports=['pdf_to_md_mineru'],
            ),
            ToolPoolTool(
                tool_id='removed_tool',
                status='removed',
                allowed_helper_imports=['extract_pdf_text'],
                allowed_import_paths=['backend.services.runtime_tools'],
                allowed_function_imports=['extract_pdf_text'],
            ),
        ],
    )


def test_build_tool_pool_keeps_skill_wide_tools_and_drops_file_bindings():
    pool = build_tool_pool(skill_name='x', current_tool_pool=_allowed_pool())
    assert pool.file_bindings == []
    assert [tool.tool_id for tool in pool.tools] == ['unified_file_text_read', 'pdf_to_md_mineru', 'removed_tool']


def test_get_file_binding_projects_only_allowed_tools_from_available_tools():
    binding = get_file_binding(_allowed_pool(), 'scripts/a.py')
    assert 'unified_file_text_read' in binding.allowed_tool_ids
    assert 'pdf_to_md_mineru' in binding.allowed_tool_ids
    assert 'removed_tool' not in [tool['tool_id'] for tool in binding.available_tools]
    assert 'read_file_text' in binding.allowed_helper_imports
    assert 'extract_pdf_text' not in binding.allowed_helper_imports
    assert 'backend.services.runtime_tools.custom_tools.pdf_to_md_mineru' in binding.allowed_import_paths
    assert any(
        tool['tool_id'] == 'pdf_to_md_mineru'
        and tool['function_name'] == 'pdf_to_md_mineru'
        and tool['import_path'] == 'backend.services.runtime_tools.custom_tools.pdf_to_md_mineru'
        for tool in binding.available_tools
    )


def test_tool_pool_saved_and_file_binding_summary_has_available_tools(tmp_path):
    save_tool_pool(tmp_path, _allowed_pool())
    loaded = load_tool_pool(tmp_path)
    binding = get_file_binding(loaded, 'scripts/a.py')
    summary = binding.model_dump(mode='json')
    for key in ['available_tools', 'allowed_tool_ids', 'primary_tool_ids', 'secondary_tool_ids', 'allowed_helper_imports', 'allowed_import_paths', 'allowed_function_imports', 'scored_tools', 'matched_features_by_tool', 'denied_helper_imports']:
        assert key in summary
    assert 'backend.services.runtime_tools.custom_tools.pdf_to_md_mineru' in summary['allowed_import_paths']
    assert summary['allowed_import_paths'] == sorted(set(tool['import_path'] for tool in summary['available_tools']))


def test_generation_prompt_includes_available_tool_import_path_and_function_name():
    entry = {
        'path': 'scripts/a.py',
        'role': 'generic_script',
        'runtime': 'python',
        'language': 'python',
        'inputs': ['input_path'],
        'outputs': ['markdown'],
        'tool_binding_summary': {
            'primary_tool_ids': ['pdf_to_md_mineru'],
            'available_tools': [{
                'tool_id': 'pdf_to_md_mineru',
                'function_name': 'pdf_to_md_mineru',
                'import_path': 'backend.services.runtime_tools.custom_tools.pdf_to_md_mineru',
                'input_schema': {},
                'output_schema': {},
            }],
            'scored_tools': [{'tool_id': 'pdf_to_md_mineru', 'score': 222}],
        },
    }
    messages = _build_script_generate_file_prompt_variant(file_path='scripts/a.py', skill_name='x', purpose='把 PDF 转成 Markdown，保留结构', blueprint_text='', role='generic_script', skill_plan_entry=entry, variant='standard')
    text = '\n'.join(str(m.get('content') or '') for m in messages)
    assert 'available_tools' in text
    assert 'backend.services.runtime_tools.custom_tools.pdf_to_md_mineru' in text
    assert 'pdf_to_md_mineru' in text


def test_gate_derives_pdf_helpers_only_from_function_manifest():
    from backend.services.creator.tool_pool_gate import gate_tool_request

    event = gate_tool_request({"candidate_tool_id": "pdf_generation"})

    assert event.decision in {"allow", "require_dependency", "require_config"}
    assert "create_pdf" in event.allowed_helper_imports
    assert "create_pdf_document" in event.allowed_helper_imports
    assert "images_to_pdf" not in event.allowed_helper_imports
    assert "merge_pdfs" not in event.allowed_helper_imports
    assert "backend.services.runtime_tools" in event.allowed_import_paths
    assert "create_pdf" in event.allowed_function_imports


def test_legacy_pdf_tool_pool_helpers_do_not_leak_into_binding_or_registry_context():
    from backend.services.creator.generation import build_available_tool_context

    pool = ToolPoolModel(
        skill_name="x",
        tools=[
            ToolPoolTool(
                tool_id="pdf_generation",
                status="allowed",
                allowed_helper_imports=[
                    "create_pdf",
                    "create_pdf_document",
                    "images_to_pdf",
                    "merge_pdfs",
                ],
                allowed_import_paths=["backend.services.runtime_tools"],
                allowed_function_imports=[
                    "create_pdf",
                    "create_pdf_document",
                    "images_to_pdf",
                    "merge_pdfs",
                ],
            )
        ],
    )

    binding = get_file_binding(pool, "scripts/build_document.py")

    pdf_functions = {
        item.get("function_name")
        for item in binding.available_tools
        if item.get("tool_id") == "pdf_generation"
    }
    assert pdf_functions == {"create_pdf", "create_pdf_document"}
    assert any(
        item.get("tool_id") == "script_argv_guard"
        and item.get("function_name") == "strict_json_argv_guard"
        for item in binding.available_tools
    )

    context = build_available_tool_context(
        binding.model_dump(mode="json"),
        role="pdf_builder",
        file_path="scripts/build_document.py",
    )
    resolved_functions = {
        item.get("function_name")
        for item in context.get("resolved_tools", [])
        if item.get("capability_name") == "pdf_generation"
    }
    assert resolved_functions == {"create_pdf", "create_pdf_document"}
    assert "images_to_pdf" not in resolved_functions
    assert "merge_pdfs" not in resolved_functions


def test_custom_registry_function_manifest_still_gates_stores_and_resolves():
    from backend.services.creator.generation import build_available_tool_context
    from backend.services.creator.tool_pool_gate import gate_tool_request
    from backend.services.creator_tool_registry import (
        ToolCapability,
        ToolFunctionManifest,
        clear_registered_tool_capabilities,
        register_tool_capability,
    )

    clear_registered_tool_capabilities()
    try:
        register_tool_capability(
            ToolCapability(
                name="custom_pdf_to_md",
                display_name="Custom PDF to Markdown",
                category="registered",
                helper_imports=["legacy_helper_that_must_not_be_callable"],
                functions=[
                    ToolFunctionManifest(
                        function_name="pdf_to_md_mineru",
                        import_path="backend.services.runtime_tools.custom_tools.pdf_to_md_mineru",
                        short_description="Convert PDF to Markdown.",
                        when_to_use="Use for PDF to Markdown conversion.",
                        signature="pdf_to_md_mineru(input_path: str) -> dict",
                        input_schema={"type": "object"},
                        output_schema={"type": "object"},
                    )
                ],
            )
        )

        event = gate_tool_request({"candidate_tool_id": "custom_pdf_to_md"})
        assert event.decision == "allow"
        assert event.allowed_helper_imports == []
        assert event.allowed_import_paths == [
            "backend.services.runtime_tools.custom_tools.pdf_to_md_mineru"
        ]
        assert event.allowed_function_imports == ["pdf_to_md_mineru"]

        pool = ToolPoolModel(
            skill_name="x",
            tools=[
                ToolPoolTool(
                    tool_id="custom_pdf_to_md",
                    status="allowed",
                    allowed_helper_imports=list(event.allowed_helper_imports),
                    allowed_import_paths=list(event.allowed_import_paths),
                    allowed_function_imports=list(event.allowed_function_imports),
                )
            ],
        )
        binding = get_file_binding(pool, "scripts/custom.py")
        assert any(
            item.get("tool_id") == "custom_pdf_to_md"
            and item.get("function_name") == "pdf_to_md_mineru"
            and item.get("import_path")
            == "backend.services.runtime_tools.custom_tools.pdf_to_md_mineru"
            for item in binding.available_tools
        )

        context = build_available_tool_context(
            binding.model_dump(mode="json"),
            role="generic_script",
            file_path="scripts/custom.py",
        )
        assert any(
            item.get("capability_name") == "custom_pdf_to_md"
            and item.get("function_name") == "pdf_to_md_mineru"
            for item in context.get("resolved_tools", [])
        )
    finally:
        clear_registered_tool_capabilities()
