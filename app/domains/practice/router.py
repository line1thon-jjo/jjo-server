from collections.abc import AsyncIterator
import logging

from anyio import CancelScope
from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from app.core.database import get_db
from app.core.storage import MAX_AUDIO_BYTES, AudioValidationError, RecordingStorage, get_recording_storage
from app.domains.auth.models import User
from app.domains.auth.router import AuthError, AuthRoute, get_current_user
from app.domains.practice.schemas import (
    PracticeCreate, PracticeResponse, RecordingErrorResponse, RecordingMetadata, RecordingResponse,
)
from app.domains.practice.script_upload_router import router as script_upload_router
from app.domains.practice.service import (
    RecordingNotFoundError, create_practice, get_practice, list_practices, upload_recording,
)
from app.domains.practice.uploads import MultiPartException, MultipartLimitError, parse_recording_multipart

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/practices", tags=["practice"], route_class=AuthRoute)
router.include_router(script_upload_router)


@router.get("", response_model=list[PracticeResponse])
def list_owned(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[PracticeResponse]:
    return [PracticeResponse.model_validate(practice)
            for practice in list_practices(db, user_id=user.id)]


@router.get("/{practice_id}", response_model=PracticeResponse)
def detail(
    practice_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PracticeResponse:
    practice = get_practice(db, user_id=user.id, practice_id=practice_id)
    if practice is None:
        raise AuthError(404, "PRACTICE_NOT_FOUND")
    return PracticeResponse.model_validate(practice)


@router.post("", status_code=201, response_model=PracticeResponse)
def create(
    body: PracticeCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PracticeResponse:
    try:
        practice = create_practice(db, user_id=user.id, data=body)
        # Validate the response model before commit so validation failures also roll back.
        result = PracticeResponse.model_validate(practice)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return result


def multipart_available() -> bool:
    # main에 선언된 python-multipart의 실행 환경 설치 여부를 확인한다.
    try:
        import python_multipart  # noqa: F401
    except ImportError:
        return False
    return True


async def recording_form(
    request: Request, user: User = Depends(get_current_user),
) -> AsyncIterator[tuple[UploadFile, int]]:
    """인증 후 파싱하고 의존성 종료 시 스풀 파일을 닫는다."""
    if not multipart_available():
        raise AuthError(503, "UPLOAD_CONFIGURATION_ERROR")
    try:
        form = await parse_recording_multipart(request)
    except MultipartLimitError as exc:
        raise AuthError(413, exc.code) from None
    except MultiPartException:
        raise AuthError(422, "VALIDATION_ERROR", ["file", "duration_ms"]) from None
    try:
        file = form.get("file")
        fields = [] if isinstance(file, UploadFile) else ["file"]
        metadata = None
        try:
            metadata = RecordingMetadata.model_validate({"duration_ms": form.get("duration_ms")})
        except ValidationError:
            fields.append("duration_ms")
        if fields:
            raise AuthError(422, "VALIDATION_ERROR", fields)
        assert isinstance(file, UploadFile) and metadata is not None
        yield file, metadata.duration_ms
    finally:
        # Closing a temporary file must not hide the handler's DB/storage error.
        # Shield cleanup when the request was cancelled during parsing/handling.
        with CancelScope(shield=True):
            try:
                await form.close()
            except BaseException:
                logger.exception("Failed to close recording multipart data")


@router.post(
    "/{practice_id}/recordings", status_code=201, response_model=RecordingResponse,
    responses={
        **{code: {"model": RecordingErrorResponse, "description": description} for code, description in {
            401: "인증 실패", 404: "미존재 또는 타인 소유 연습",
            413: "파일 또는 multipart 입력 한도 초과", 415: "지원하지 않는 음원 헤더",
            422: "누락·잘못된 폼·빈 파일", 503: "업로드 의존성 또는 인증 설정 미비",
        }.items()},
        500: {"description": "저장소 또는 DB 오류", "content": {
            "text/plain": {"schema": {"type": "string"}, "example": "Internal Server Error"},
        }},
    },
    openapi_extra={"requestBody": {
        "required": True,
        "description": "파일 1개·일반 필드 최대 8개. 파일 본문 20 MiB, 전체 본문 20 MiB + 64 KiB. "
                       "일반 필드 각 1 KiB, 파트 헤더 각 16 KiB 한도.",
        "content": {"multipart/form-data": {
        "schema": {"type": "object", "required": ["file", "duration_ms"], "properties": {
            "file": {"type": "string", "format": "binary", "minLength": 1,
                     "maxLength": MAX_AUDIO_BYTES,
                     "description": "WAV/WebM/M4A/MP3 헤더 검사, 파일 본문 최대 20 MiB"},
            "duration_ms": RecordingMetadata.model_json_schema()["properties"]["duration_ms"],
        }},
    }}}},
)
def upload(
    practice_id: int,
    form: tuple[UploadFile, int] = Depends(recording_form),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    storage: RecordingStorage = Depends(get_recording_storage),
) -> RecordingResponse:
    """계약 초안: 클라이언트 길이 메타데이터를 저장하며 분석은 실행하지 않는다."""
    file, duration_ms = form
    try:
        return upload_recording(
            db, practice_id=practice_id, user_id=user.id, source=file.file,
            duration_ms=duration_ms, storage=storage,
        )
    except RecordingNotFoundError:
        raise AuthError(404, "PRACTICE_NOT_FOUND") from None
    except AudioValidationError as exc:
        raise AuthError(exc.status_code, exc.code) from None
