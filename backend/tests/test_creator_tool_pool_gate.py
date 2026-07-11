from backend.services.creator.tool_pool_gate import gate_tool_request


def test_gate_allows_registered_matching_tool():
    event = gate_tool_request({'target_file': 'scripts/a.py', 'candidate_tool_id': 'unified_file_text_read'}, file_role='generic_script')
    assert event.decision == 'allow'
    assert event.allowed_helper_imports == ['read_file_text']


def test_gate_allows_custom_tool_import_path_and_function():
    event = gate_tool_request({'target_file': 'scripts/a.py', 'candidate_tool_id': 'pdf_to_md_mineru'}, file_role='generic_script')
    assert event.decision == 'allow'
    assert event.allowed_import_paths == ['backend.services.runtime_tools.custom_tools.pdf_to_md_mineru']
    assert event.allowed_function_imports == ['pdf_to_md_mineru']


def test_gate_rejects_missing_tool():
    event = gate_tool_request({'target_file': 'scripts/a.py', 'candidate_tool_id': 'read_pdf_text'}, file_role='generic_script')
    assert event.decision == 'not_found'


def test_gate_file_role_does_not_change_tool_decision():
    script_event = gate_tool_request({'target_file': 'scripts/a.py', 'candidate_tool_id': 'unified_file_text_read'}, file_role='generic_script')
    reference_event = gate_tool_request({'target_file': 'references/a.md', 'candidate_tool_id': 'unified_file_text_read'}, file_role='reference')
    image_event = gate_tool_request({'target_file': 'scripts/a.py', 'candidate_tool_id': 'unified_file_text_read'}, file_role='image_generator')
    assert script_event.decision == reference_event.decision == image_event.decision == 'allow'


def test_gate_has_no_helper_replacement_or_role_hardcoding_tables():
    import backend.services.creator.tool_pool_gate as module

    assert not hasattr(module, 'SUGGESTED')
    assert not hasattr(module, 'RESOURCE_ROLES')
    assert not hasattr(module, '_declared_tool_ids')
