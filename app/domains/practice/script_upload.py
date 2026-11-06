"""TXT extraction only: no title generation, persistence, or filesystem writes."""

from pathlib import PurePath

from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from app.domains.auth.router import AuthError
from app.domains.practice.schemas import PracticeCreate

MAX_SCRIPT_FILE_BYTES = 1024 * 1024


def extract_script(filename: str | None, content: bytes) -> str:
    if not filename or PurePath(filename).suffix.lower() != ".txt":
        raise AuthError(415, "SCRIPT_FILE_TYPE_UNSUPPORTED", ["file"])
    if len(content) > MAX_SCRIPT_FILE_BYTES:
        raise AuthError(413, "SCRIPT_FILE_TOO_LARGE", ["file"])
    try:
        # utf-8-sig removes one leading BOM and leaves whitespace/newlines intact.
        script = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise AuthError(422, "SCRIPT_FILE_INVALID_ENCODING", ["file"]) from exc
    if not script.strip():
        raise AuthError(422, "SCRIPT_FILE_TEXT_EMPTY", ["file"])
    try:
        return PracticeCreate(script=script).script
    except ValidationError as exc:
        errors = [dict(error, loc=("body", *error["loc"])) for error in exc.errors()]
        raise RequestValidationError(errors) from exc
