"""Platform runtime tool helpers used by generated Skill scripts."""

from .argv_tools import strict_json_argv_guard

from .api_tools import api_get, api_post, registered_tool_call
from .document_tools import (
    create_csv,
    create_docx,
    create_pdf,
    create_pdf_document,
    create_pptx,
    create_xlsx,
    create_text_file,
    extract_pdf_text,
    images_to_pdf,
    merge_pdfs,
    read_docx_text,
    read_csv,
    read_file_text,
    read_pptx_text,
    read_spreadsheet,
)
from .retrieval_tools import (
    describe_database_table,
    fetch_url_text,
    list_database_tables,
    query_database_readonly,
    web_search,
)
from .vision_tools import analyze_image_with_vision, ocr_image
from .wechat_tools import create_wechat_draft, publish_wechat_draft, upload_wechat_media

__all__ = [
    "strict_json_argv_guard",
    "analyze_image_with_vision",
    "api_get",
    "api_post",
    "create_csv",
    "create_docx",
    "create_pdf",
    "create_pptx",
    "create_xlsx",
    "create_text_file",
    "create_wechat_draft",
    "describe_database_table",
    "extract_pdf_text",
    "fetch_url_text",
    "images_to_pdf",
    "list_database_tables",
    "merge_pdfs",
    "ocr_image",
    "publish_wechat_draft",
    "query_database_readonly",
    "read_docx_text",
    "read_csv",
    "read_file_text",
    "read_pptx_text",
    "read_spreadsheet",
    "create_pdf_document",
    "registered_tool_call",
    "upload_wechat_media",
    "web_search",
]
