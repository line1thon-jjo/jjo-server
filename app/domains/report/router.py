"""report 도메인 라우터 (BE2 담당): 분석 시작·상태, 탭별 결과, 녹음 재생 주소.

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
    AudioUrlResponse,
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
        attempt_no=recording.attempt_no, duration_ms=recording.duration_ms,
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


@router.post("", status_code=202, response_model=ReportStatusResponse, response_model_exclude_none=True)
def start(
    body: StartAnalysisRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ReportStatusResponse:
    """분석을 시작한다. 이미 진행 중이거나 완료된 녹음이면 새로 시작하지 않고 현재 상태를 돌려준다."""
    try:
        started = service.start_analysis(db, user_id=user.id, recording_id=body.recording_id)
    except service.RecordingNotFoundError:
        raise AuthError(404, "RECORDING_NOT_FOUND") from None
    if started.job is not None:
        worker.enqueue(started.report.id, started.job)
    return _status_response(_view(db, user, body.recording_id))


@router.get("/{recording_id}", response_model=ReportStatusResponse, response_model_exclude_none=True)
def status(
    recording_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ReportStatusResponse:
    return _status_response(_view(db, user, recording_id))


@router.get("/{recording_id}/overview", response_model=Overview)
def overview(
    recording_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Overview:
    report, result = _completed(db, user, recording_id)
    stats = transform.build_overview_stats(result)
    summary = load_feedback(report.feedback, result).summary
    return Overview(summary=summary, speed=stats.speed, habits=stats.habits)


@router.get("/{recording_id}/utterance", response_model=UtteranceAnalysis, response_model_exclude_none=True)
def utterance(
    recording_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UtteranceAnalysis:
    _, result = _completed(db, user, recording_id)
    return transform.build_utterance(result)


@router.get("/{recording_id}/feedback", response_model=FeedbackResponse)
def feedback(
    recording_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FeedbackResponse:
    """AI 피드백 탭. 항목은 0~3개이고 배열 순서가 순위(rank)와 같다."""
    report, result = _completed(db, user, recording_id)
    return FeedbackResponse(items=load_feedback(report.feedback, result).items)


@router.get("/{recording_id}/audio-url", response_model=AudioUrlResponse)
def audio_url(
    recording_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AudioUrlResponse:
    """30분간 유효한 재생 주소를 만든다. 만료되면 이 API 로 새 주소를 받는다."""
    _view(db, user, recording_id)  # 본인 녹음인지 확인
    token = issue_audio_token(user.id, recording_id)
    return AudioUrlResponse(
        audio_url=f"/reports/{recording_id}/audio?token={token}", expires_in_seconds=AUDIO_URL_TTL_SECONDS,
    )


@router.get("/{recording_id}/audio", name="audio_file")
def audio_file(
    recording_id: int,
    token: str = Query(min_length=1),
    db: Session = Depends(get_db),
) -> FileResponse:
    """서명 토큰으로 녹음 파일을 제공한다(Range 지원). 로그인 헤더가 필요 없다."""
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
