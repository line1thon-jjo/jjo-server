"""Authenticated TXT extraction without practice creation or original storage."""

from fastapi import APIRouter, Depends, File, UploadFile
from pydantic import BaseModel

from app.domains.auth.models import User
from app.domains.auth.router import AuthError, AuthRoute, get_current_user
from app.domains.practice.script_upload import MAX_SCRIPT_FILE_BYTES, extract_script


class ScriptUploadResponse(BaseModel):
    script: str


# The parent practice router owns the /practices prefix.
router = APIRouter(tags=["practice"], route_class=AuthRoute)


@router.post("/script-upload", response_model=ScriptUploadResponse)
async def upload_script(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
) -> ScriptUploadResponse:
    try:
        if file.size is not None and file.size > MAX_SCRIPT_FILE_BYTES:
            raise AuthError(413, "SCRIPT_FILE_TOO_LARGE", ["file"])
        content = await file.read(MAX_SCRIPT_FILE_BYTES + 1)
        return ScriptUploadResponse(script=extract_script(file.filename, content))
    finally:
        # Close the multipart parser's spooled file on success and every failure.
        await file.close()
