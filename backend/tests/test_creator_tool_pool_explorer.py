from backend.services.creator.tool_pool_explorer import explore_tool_pool


def _scores(result):
    return {r.candidate_tool_id: r.score for r in result.candidate_tool_requests}


def test_explorer_semantic_pdf_to_text_recalls_multiple_tools():
    result = explore_tool_pool(user_request='把 PDF 转成文本', file_specs=[{'path': 'scripts/a.py', 'role': 'generic_script', 'inputs': ['report.pdf'], 'outputs': ['text']}])
    ids = {r.candidate_tool_id for r in result.candidate_tool_requests}
    assert {'pdf_to_md_mineru', 'unified_file_text_read', 'pdf_parsing'} <= ids
    assert 'read_pdf_text' not in str(result.model_dump())


def test_explorer_prefers_mineru_for_markdown_structure():
    result = explore_tool_pool(user_request='把 PDF 转成 Markdown，保留结构和版面表格', file_specs=[{'path': 'scripts/a.py', 'role': 'generic_script', 'inputs': ['report.pdf'], 'outputs': ['markdown']}])
    scores = _scores(result)
    assert scores['pdf_to_md_mineru'] > scores['unified_file_text_read']
    assert scores['pdf_to_md_mineru'] > scores['pdf_parsing']


def test_explorer_uploaded_pdf_and_candidate_tools_participate():
    result = explore_tool_pool(user_request='处理上传文件', file_specs=[{'path': 'scripts/a.py', 'role': 'generic_script'}], uploaded_files=[{'name': 'report.pdf', 'candidate_tools': ['vision_understanding']}])
    ids = {r.candidate_tool_id for r in result.candidate_tool_requests}
    assert 'pdf_to_md_mineru' in ids
    assert 'vision_understanding' in ids
    assert any(t['tool_id'] == 'vision_understanding' for t in result.uploaded_file_triggers)
