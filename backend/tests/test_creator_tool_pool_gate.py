from backend.services.creator.tool_pool_gate import gate_tool_request


def test_gate_allows_registered_matching_tool():
    event = gate_tool_request({'target_file': 'scripts/a.py', 'candidate_tool_id': 'unified_file_text_read'}, file_role='generic_script')
    assert event.decision == 'allow'
    assert event.allowed_helper_imports == ['read_file_text']


def test_gate_rejects_missing_tool():
    event = gate_tool_request({'target_file': 'scripts/a.py', 'candidate_tool_id': 'read_pdf_text'}, file_role='generic_script')
    assert event.decision == 'not_found'


def test_gate_rejects_reference_runtime_tool():
    event = gate_tool_request({'target_file': 'references/a.md', 'candidate_tool_id': 'unified_file_text_read'}, file_role='reference')
    assert event.decision == 'blocked_by_policy'
