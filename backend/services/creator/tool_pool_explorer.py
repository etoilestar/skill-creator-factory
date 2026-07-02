from __future__ import annotations
from pathlib import Path
from typing import Any
from pydantic import BaseModel, Field
from .tool_pool_models import ToolPoolAddToolRequest

EXT_TO_TOOL = {'.pdf':'pdf_parsing','.docx':'docx_parsing','.pptx':'pptx_parsing','.xlsx':'spreadsheet_read','.xlsm':'spreadsheet_read','.csv':'csv_read','.tsv':'csv_read'}
MULTI_TEXT_EXTS = set(EXT_TO_TOOL)|{'.txt','.md'}
IMAGE_EXTS = {'.png','.jpg','.jpeg','.webp','.gif'}

class ToolPoolExplorationResult(BaseModel):
    candidate_tool_requests: list[ToolPoolAddToolRequest] = Field(default_factory=list)
    missing_capability_requests: list[dict[str, Any]] = Field(default_factory=list)
    denied_by_explorer: list[dict[str, Any]] = Field(default_factory=list)
    confidence: float = 0.8
    reason: str = ''

def _file_exts(values: Any) -> set[str]:
    out=set()
    if isinstance(values, dict): values = list(values.values()) + list(values.keys())
    if not isinstance(values, list): values=[values]
    for v in values:
        if isinstance(v, dict):
            out |= _file_exts(list(v.values()))
        else:
            ext=Path(str(v)).suffix.lower()
            if ext: out.add(ext)
    return out

def explore_tool_pool(*, user_request: str = '', blueprint_text: str = '', file_specs: list[dict[str, Any]] | None = None, uploaded_files: list[dict[str, Any]] | None = None, current_tool_pool: Any = None, available_tool_registry: Any = None, missing_tool_configs: Any = None) -> ToolPoolExplorationResult:
    reqs=[]; text=(user_request+'\n'+blueprint_text).lower()
    uploaded_exts=_file_exts(uploaded_files or [])
    for spec in file_specs or []:
        target=str(spec.get('path') or spec.get('target_file') or '')
        if not target.startswith('scripts/'): continue
        exts=uploaded_exts | _file_exts(spec.get('inputs') or {}) | _file_exts(spec.get('required_capabilities') or [])
        selected=[str(x) for x in (spec.get('selected_tools') or spec.get('required_tool_slots') or [])]
        tools=[]
        if len(exts & MULTI_TEXT_EXTS) > 1 or 'multi-format' in text or '多格式' in text:
            tools.append(('unified_file_text_read','multi-format text read'))
        else:
            for ext in sorted(exts):
                if ext in EXT_TO_TOOL: tools.append((EXT_TO_TOOL[ext], f'{ext} input'))
        if exts & IMAGE_EXTS or 'image' in text or '图片' in text:
            tools.append(('vision_understanding','image understanding'))
        if 'pdf' in text and any(w in text for w in ['output','生成','report','报告']):
            tools.append(('pdf_generation','pdf output'))
        for tool in selected:
            if tool and not tool.startswith('read_'): tools.append((tool,'selected by blueprint'))
        seen=set()
        for tool, reason in tools:
            if tool in seen: continue
            seen.add(tool)
            reqs.append(ToolPoolAddToolRequest(target_file=target, requested_capability=tool, candidate_tool_id=tool, source='registry_exploration', reason=reason, confidence=0.85))
    return ToolPoolExplorationResult(candidate_tool_requests=reqs, reason='deterministic registry exploration')
