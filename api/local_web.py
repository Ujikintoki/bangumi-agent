"""同源网页及不含凭据的本机配置反馈。"""
from hashlib import sha256
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, HTMLResponse

from api.access import same_origin_local_request
from core.auth import authenticated_user
from core.config import get_settings

router = APIRouter()
_WEB = Path(__file__).resolve().parents[1] / "web"


@router.get("/", include_in_schema=False)
async def index():
    version = sha256((_WEB / "chat.js").read_bytes()).hexdigest()[:16]
    html = (_WEB / "index.html").read_text(encoding="utf-8").replace('/web/chat.js', f'/web/chat.js?v={version}')
    return HTMLResponse(html, headers={"Cache-Control": "no-store", "X-UI-Version": version})


@router.get("/web/chat.js", include_in_schema=False)
async def javascript():
    return FileResponse(_WEB / "chat.js", media_type="text/javascript", headers={"Cache-Control": "no-store"})


@router.get("/api/readiness", dependencies=[Depends(same_origin_local_request)])
async def readiness(user_id: str = Depends(authenticated_user)):
    settings = get_settings()
    missing = []
    if not settings.LLM_API_KEY.strip():
        missing.append("LLM_API_KEY")
    if not settings.LLM_MODEL.strip():
        missing.append("LLM_MODEL")
    return {
        "ready": not missing,
        "missing": missing,
        "rag_enabled": settings.RAG_ENABLED,
        "memory_enabled": settings.MEMORY_ENABLED,
        "history": "sqlite",
        "storage_path": settings.SESSION_DB_PATH,
    }
