from backend.services.creator.tool_pool_explorer import explore_tool_pool


def test_explorer_prefers_unified_for_multi_format():
    result = explore_tool_pool(file_specs=[{'path': 'scripts/a.py', 'inputs': ['a.pdf', 'b.xlsx']}])
    assert [r.candidate_tool_id for r in result.candidate_tool_requests] == ['unified_file_text_read']
    assert 'read_pdf_text' not in str(result.model_dump())
