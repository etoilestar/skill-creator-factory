from fastapi import APIRouter

from ..config import settings
from ..services.llm_proxy import check_connection

router = APIRouter(prefix="/api/health", tags=["health"])


@router.get("")
async def health_check():
    return {"status": "ok"}


@router.get("/llm")
async def llm_health():
    result = await check_connection()
    return {"llm_url": settings.llm_base_url, **result}
