"""report 도메인 라우터 (BE2 담당): 회차 목록, 분석 시작·상태, 탭별 결과, 녹음 재생 주소.

리포트는 녹음 하나당 하나라서 경로의 {recording_id} 로 접근한다. 에러 응답 형식은 auth·practice 와 같다.
"""
from pathlib import Path

import jwt
from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.contracts.analysis_result import AnalysisResult
from app.core.database import get_db
from app.domains.auth.models import User
from app.domains.auth.router import AuthError, AuthRoute, get_current_user
from app.domains.practice.service import get_recording_for_analysis
from app.domains.report import service, transform, worker
from app.domains.report.audio import AUDIO_URL_TTL_SECONDS, issue_audio_token, verify_audio_token
from app.domains.report.feedback.service import load_feedback
from app.domains.report.models import Report, ReportStatus
from app.domains.report.schemas import (
    Attempt,
    AttemptsResponse,
    AudioUrlResponse,
    ErrorResponse,
    Failure,
    FeedbackResponse,
    Overview,
    ReportStatusResponse,
    StartAnalysisRequest,
    UtteranceAnalysis,
)

router = APIRouter(prefix="/reports", tags=["report"], route_class=AuthRoute)

KNOWN_WARNINGS = ("SCRIPT_DEVIATION", "PARTIAL_ALIGNMENT", "PITCH_UNAVAILABLE")
AUDIO_MEDIA_TYPES = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".webm": "audio/webm"}


# Swagger 에 보이는 오류 응답. 같은 상태 코드의 여러 code 는 설명에 함께 적는다.
def _errors(*items: tuple[int, str]) -> dict[int | str, dict]:
    out: dict[int | str, dict] = {}
    for status_code, description in items:
        if status_code in out:
            out[status_code]["description"] += " / " + description
        else:
            out[status_code] = {"model": ErrorResponse, "description": description}
    # FastAPI 기본 422 형식 대신 실제로 내려가는 형식을 보여 준다
    out.setdefault(422, {"model": ErrorResponse, "description": "VALIDATION_ERROR: 요청 형식이 틀림 (fields 에 틀린 항목)"})
    return out


_LOGIN = (401, "ACCESS_CREDENTIAL_INVALID: 로그인 토큰이 없거나 만료됨")
_NO_RECORDING = (404, "RECORDING_NOT_FOUND: 녹음이 없거나 다른 사람의 녹음")
_NO_REPORT = (404, "REPORT_NOT_FOUND: 아직 분석을 시작하지 않음 → POST /reports")
_NOT_DONE = (409, "REPORT_NOT_COMPLETED: 분석 중이거나 실패한 리포트")
_BAD_DATA = (500, "REPORT_DATA_INVALID: 저장된 분석 결과를 읽을 수 없음")
_TAB_ERRORS = _errors(_LOGIN, _NO_RECORDING, _NO_REPORT, _NOT_DONE, _BAD_DATA)


def _status_response(view: service.ReportView) -> ReportStatusResponse:
    report, recording = view.report, view.recording
    if report is None:
        raise AuthError(404, "REPORT_NOT_FOUND")
    failure = Failure(code=report.failure_reason or "INTERNAL") if report.status == ReportStatus.FAILED else None
    warnings: list[str] = []
    if report.status == ReportStatus.COMPLETED and report.analysis_result:
        warnings = [w for w in report.analysis_result.get("warnings", []) if w in KNOWN_WARNINGS]
    return ReportStatusResponse(
        recording_id=recording.recording_id, status=report.status, practice_title=view.practice_title,
        recorded_at=view.recorded_at, attempt_no=recording.attempt_no, duration_ms=recording.duration_ms,
        failure=failure, warnings=warnings,
    )


def _view(db: Session, user: User, recording_id: int) -> service.ReportView:
    try:
        return service.get_report_view(db, user_id=user.id, recording_id=recording_id)
    except service.RecordingNotFoundError:
        raise AuthError(404, "RECORDING_NOT_FOUND") from None


def _completed(db: Session, user: User, recording_id: int) -> tuple[Report, AnalysisResult]:
    report: Report | None = _view(db, user, recording_id).report
    if report is None:
        raise AuthError(404, "REPORT_NOT_FOUND")
    if report.status != ReportStatus.COMPLETED or not report.analysis_result:
        raise AuthError(409, "REPORT_NOT_COMPLETED")
    try:
        return report, AnalysisResult.model_validate(report.analysis_result)
    except ValidationError:
        raise AuthError(500, "REPORT_DATA_INVALID") from None


@router.get(
    "/practices/{practice_id}/attempts", response_model=AttemptsResponse, summary="회차 선택 목록",
    responses=_errors(_LOGIN, (404, "PRACTICE_NOT_FOUND: 연습이 없거나 다른 사람의 연습")),
)
def attempts(
    practice_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AttemptsResponse:
    """연습의 회차를 회차 번호 오름차순으로 돌려준다. 분석을 시작하지 않은 회차는 `status` 가 `null` 이다."""
    items = service.list_attempts(db, user_id=user.id, practice_id=practice_id)
    if items is None:
        raise AuthError(404, "PRACTICE_NOT_FOUND")
    return AttemptsResponse(attempts=[Attempt(**item._asdict()) for item in items])


@router.post(
    "", status_code=202, response_model=ReportStatusResponse, response_model_exclude_none=True, summary="분석 시작",
    responses=_errors(_LOGIN, _NO_RECORDING),
)
def start(
    body: StartAnalysisRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ReportStatusResponse:
    """분석을 백그라운드로 시작하고 현재 상태를 돌려준다.

    - 이미 분석 중이거나 완료된 녹음이면 새로 시작하지 않는다(여러 번 불러도 안전).
    - 실패한 녹음은 처음부터 다시 분석한다.
    - 이후 `GET /reports/{recording_id}` 를 3초 간격으로 조회한다. 녹음 1분 기준 20~30초 걸린다.
    """
    try:
        started = service.start_analysis(db, user_id=user.id, recording_id=body.recording_id)
    except service.RecordingNotFoundError:
        raise AuthError(404, "RECORDING_NOT_FOUND") from None
    if started.job is not None:
        worker.enqueue(started.report.id, started.job)
    return _status_response(_view(db, user, body.recording_id))


@router.get(
    "/{recording_id}", response_model=ReportStatusResponse, response_model_exclude_none=True,
    summary="분석 상태와 화면 헤더", responses=_errors(_LOGIN, _NO_RECORDING, _NO_REPORT),
)
def status(
    recording_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ReportStatusResponse:
    """분석 상태(`status`)와 결과 화면 헤더. `failure` 는 FAILED 일 때만, `warnings` 는 COMPLETED 일 때만 값이 있다."""
    return _status_response(_view(db, user, recording_id))


@router.get("/{recording_id}/overview", response_model=Overview, summary="전체 결과 탭", responses=_TAB_ERRORS)
def overview(
    recording_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Overview:
    """총평, 빠르기 구절 수, 반복된 습관. 분석이 끝난(COMPLETED) 뒤에만 조회할 수 있다."""
    report, result = _completed(db, user, recording_id)
    stats = transform.build_overview_stats(result)
    summary = load_feedback(report.feedback, result).summary
    return Overview(summary=summary, speed=stats.speed, habits=stats.habits)


@router.get(
    "/{recording_id}/utterance", response_model=UtteranceAnalysis, response_model_exclude_none=True,
    summary="발화 분석 탭", responses=_TAB_ERRORS,
)
def utterance(
    recording_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UtteranceAnalysis:
    """본문과 글자별 표시(높낮이·늘임), 빠르기, 쉼, 대본과 다른 곳, 구절별 재생 구간. 모든 위치는 `spoken_text` 의 글자 번호다."""
    _, result = _completed(db, user, recording_id)
    return transform.build_utterance(result)


@router.get("/{recording_id}/feedback", response_model=FeedbackResponse, summary="AI 피드백 탭", responses=_TAB_ERRORS)
def feedback(
    recording_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FeedbackResponse:
    """AI 피드백 탭. 항목은 0~3개이고 배열 순서가 순위(rank)와 같다."""
    report, result = _completed(db, user, recording_id)
    return FeedbackResponse(items=load_feedback(report.feedback, result).items)


@router.get(
    "/{recording_id}/audio-url", response_model=AudioUrlResponse, summary="녹음 재생 주소 발급",
    responses=_errors(_LOGIN, _NO_RECORDING),
)
def audio_url(
    recording_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AudioUrlResponse:
    """30분간 유효한 재생 주소를 만든다. `audio_url` 은 상대 경로라 앞에 API 주소를 붙여서 쓴다. 만료되면 새로 받는다."""
    _view(db, user, recording_id)  # 본인 녹음인지 확인
    token = issue_audio_token(user.id, recording_id)
    return AudioUrlResponse(
        audio_url=f"/reports/{recording_id}/audio?token={token}", expires_in_seconds=AUDIO_URL_TTL_SECONDS,
    )


@router.get(
    "/{recording_id}/audio", name="audio_file", summary="녹음 파일 (재생용)",
    responses={
        200: {"content": {"audio/*": {}}, "description": "녹음 파일 전체"},
        206: {"content": {"audio/*": {}}, "description": "Range 요청한 구간"},
        **_errors(
            (401, "AUDIO_TOKEN_INVALID: token 이 틀리거나 만료됨(30분) → audio-url 을 다시 받기"),
            _NO_RECORDING, (404, "AUDIO_FILE_NOT_FOUND: 서버에 녹음 파일이 없음"),
        ),
    },
)
def audio_file(
    recording_id: int,
    token: str = Query(min_length=1, description="audio-url 이 발급한 재생 토큰"),
    db: Session = Depends(get_db),
) -> FileResponse:
    """녹음 파일을 돌려준다. 로그인 헤더 대신 주소의 `token` 으로 확인하므로 `<audio src>` 에 바로 쓸 수 있다. 구간 이동(Range)을 지원한다."""
    try:
        user_id = verify_audio_token(token, recording_id)
    except (jwt.InvalidTokenError, RuntimeError):
        raise AuthError(401, "AUDIO_TOKEN_INVALID") from None
    try:
        recording = get_recording_for_analysis(db, recording_id=recording_id, user_id=user_id)
    except service.RecordingNotFoundError:
        raise AuthError(404, "RECORDING_NOT_FOUND") from None
    path = Path(recording.audio_path)
    if not path.is_file():
        raise AuthError(404, "AUDIO_FILE_NOT_FOUND")
    return FileResponse(path, media_type=AUDIO_MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream"))
