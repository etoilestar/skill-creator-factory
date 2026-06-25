"""Platform runtime tool helpers used by generated Skill scripts."""

from .api_tools import api_get, api_post, registered_tool_call
from .document_tools import (
    build_pdf_report,
    chunk_document,
    chunk_pdf_by_page,
    chunk_text,
    create_docx,
    create_pdf,
    create_pptx,
    create_text_file,
    extract_pdf_text,
    images_to_pdf,
    merge_pdfs,
    read_docx_text,
    read_pptx_text,
    read_spreadsheet,
)
from .retrieval_tools import (
    build_faiss_index,
    describe_database_table,
    fetch_url_text,
    get_ollama_embedding,
    list_database_tables,
    query_database_readonly,
    search_faiss_index,
    web_search,
)
from .vision_tools import analyze_image_with_vision, ocr_image
from .wechat_tools import create_wechat_draft, publish_wechat_draft, upload_wechat_media

__all__ = [
    "analyze_image_with_vision",
    "api_get",
    "api_post",
    "build_faiss_index",
    "build_pdf_report",
    "chunk_document",
    "chunk_pdf_by_page",
    "chunk_text",
    "create_docx",
    "create_pdf",
    "create_pptx",
    "create_text_file",
    "create_wechat_draft",
    "describe_database_table",
    "extract_pdf_text",
    "fetch_url_text",
    "get_ollama_embedding",
    "images_to_pdf",
    "list_database_tables",
    "merge_pdfs",
    "ocr_image",
    "publish_wechat_draft",
    "query_database_readonly",
    "read_docx_text",
    "read_pptx_text",
    "read_spreadsheet",
    "registered_tool_call",
    "search_faiss_index",
    "upload_wechat_media",
    "web_search",
]
