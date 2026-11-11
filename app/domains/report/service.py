"""report 서비스 (BE2 담당): 분석 시작, 상태 전이, 조회. DB만 알고 FastAPI는 모른다.

상태 흐름: PENDING(대기) → ANALYZING(분석 중) → COMPLETED / FAILED
리포트는 녹음 하나당 하나이고, 실제 분석은 worker.py 가 별도 스레드에서 실행한다.
"""
import logging
import threading
from datetime import datetime, timezone
from typing import Any, NamedTuple, Protocol

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.contracts.analysis_result import SCHEMA_VERSION, AnalysisResult, ErrorCode
from app.domains.practice.service import (
    RecordingNotFoundError, get_practice, get_recording_for_analysis, list_recordings,
)
from app.domains.report.models import Report, ReportStatus

__all__ = [
    "AnalysisInput", "StartResult", "ReportView", "AttemptView", "RecordingNotFoundError", "start_analysis",
    "get_report", "get_report_view", "list_attempts",
    "claim_for_analysis", "save_success", "save_failure", "is_supported_version", "clear_active",
    "is_active",
]

logger = logging.getLogger(__name__)

INTERNAL_FAILURE = ErrorCode.INTERNAL.value
_ACTIVE: set[int] = set()  # 이 프로세스에서 분석이 대기 중이거나 실행 중인 리포트
_ACTIVE_LOCK = threading.Lock()


class AnalysisInput(Protocol):
    """분석에 필요한 녹음 정보. practice 의 DTO 가 이 모양을 따른다."""

    @property
    def recording_id(self) -> int: ...

    @property
    def practice_id(self) -> int: ...

    @property
    def attempt_no(self) -> int: ...

    @property
    def duration_ms(self) -> int: ...

    @property
    def audio_path(self) -> str: ...

    @property
    def script_snapshot(self) -> str: ...


class StartResult(NamedTuple):
    report: Report
    job: AnalysisInput | None  # None 이면 새로 분석할 필요가 없다(이미 진행 중이거나 완료)


def _mark_active(report_id: int) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE.add(report_id)


def clear_active(report_id: int) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE.discard(report_id)


def is_active(report_id: int) -> bool:
    with _ACTIVE_LOCK:
        return report_id in _ACTIVE


def _reset(report: Report) -> None:
    report.status = ReportStatus.PENDING
    report.analysis_result = None
    report.feedback = None
    report.schema_version = None
    report.failure_reason = None
    report.completed_at = None


def _find_report(db: Session, recording_id: int, *, lock: bool = False) -> Report | None:
    # 세션에 남은 오래된 객체 대신 항상 DB의 현재 값을 읽는다(분석은 다른 세션이 상태를 바꾼다).
    stmt = select(Report).where(Report.recording_id == recording_id).execution_options(populate_existing=True)
    if lock:
        stmt = stmt.with_for_update()
    return db.scalar(stmt)


def start_analysis(db: Session, *, user_id: int, recording_id: int) -> StartResult:
    """녹음의 분석을 시작할 준비를 하고 commit 한다. 호출한 쪽이 job 이 있으면 worker 에 넣는다.

    - 리포트가 없으면 PENDING 으로 만든다.
    - FAILED 이거나, 서버 재시작 등으로 끊긴 PENDING/ANALYZING 이면 처음 상태로 되돌려 다시 분석한다.
    - 이미 진행 중이거나 COMPLETED 이면 기존 리포트를 그대로 돌려주고 job 은 None 이다.
    녹음이 없거나 타인 소유면 RecordingNotFoundError 를 낸다.
    """
    recording = get_recording_for_analysis(db, recording_id=recording_id, user_id=user_id)
    try:
        report = _find_report(db, recording.recording_id, lock=True)
        run = False
        if report is None:
            report = Report(recording_id=recording.recording_id, status=ReportStatus.PENDING)
            db.add(report)
            db.flush()
            run = True
        elif report.status == ReportStatus.FAILED or (
            report.status in (ReportStatus.PENDING, ReportStatus.ANALYZING) and not is_active(report.id)
        ):
            _reset(report)
            run = True
        if run:
            _mark_active(report.id)  # commit 직후 조회가 끊긴 분석으로 오해하지 않도록 먼저 표시
        db.commit()
    except IntegrityError:
        # 같은 녹음으로 동시에 시작해 다른 요청이 먼저 만든 경우: 그 리포트를 쓴다.
        db.rollback()
        report = _find_report(db, recording.recording_id)
        if report is None:
            raise
        run = False
    except Exception:
        db.rollback()
        raise
    return StartResult(report=report, job=recording if run else None)


class ReportView(NamedTuple):
    report: Report | None  # 아직 분석을 시작하지 않았으면 None
    recording: AnalysisInput
    practice_title: str
    recorded_at: datetime


class AttemptView(NamedTuple):
    recording_id: int
    attempt_no: int
    recorded_at: datetime
    status: ReportStatus | None  # 아직 분석을 시작하지 않았으면 None


def get_report(db: Session, *, user_id: int, recording_id: int) -> Report | None:
    """리포트를 조회한다. 없으면 None. 녹음이 없거나 타인 소유면 RecordingNotFoundError."""
    recording = get_recording_for_analysis(db, recording_id=recording_id, user_id=user_id)
    return _report_for(db, recording)


def get_report_view(db: Session, *, user_id: int, recording_id: int) -> ReportView:
    """리포트와 화면 헤더에 필요한 녹음·연습 정보를 함께 조회한다."""
    recording = get_recording_for_analysis(db, recording_id=recording_id, user_id=user_id)
    report = _report_for(db, recording)
    practice = get_practice(db, user_id=user_id, practice_id=recording.practice_id)
    items = list_recordings(db, user_id=user_id, practice_id=recording.practice_id) or []
    recorded_at = next((i.created_at for i in items if i.recording_id == recording.recording_id), None)
    if recorded_at is None:  # 위에서 본인 녹음임을 확인했으므로 정상이라면 일어나지 않는다
        raise RecordingNotFoundError()
    return ReportView(
        report=report, recording=recording, practice_title=practice.title if practice else "", recorded_at=recorded_at,
    )


def list_attempts(db: Session, *, user_id: int, practice_id: int) -> list[AttemptView] | None:
    """연습의 회차 목록과 회차별 분석 상태. 연습이 없거나 타인 소유면 None."""
    items = list_recordings(db, user_id=user_id, practice_id=practice_id)
    if items is None:
        return None
    reports = list(db.scalars(  # 끊긴 작업 정리에서 commit 하므로 먼저 다 읽는다
        select(Report)
        .where(Report.recording_id.in_([i.recording_id for i in items]))
        .execution_options(populate_existing=True)
    ))
    by_recording = {r.recording_id: _settle_interrupted(db, r) for r in reports}
    return [
        AttemptView(
            recording_id=i.recording_id, attempt_no=i.attempt_no, recorded_at=i.created_at,
            status=by_recording[i.recording_id].status if i.recording_id in by_recording else None,
        )
        for i in items
    ]


def _report_for(db: Session, recording: AnalysisInput) -> Report | None:
    """PENDING/ANALYZING 인데 이 프로세스가 작업을 들고 있지 않으면 서버가 꺼지면서 끊긴 것이므로
    FAILED(INTERNAL)로 정리해서 화면이 영원히 "분석 중"에 머물지 않게 한다.
    """
    report = _find_report(db, recording.recording_id)
    return _settle_interrupted(db, report) if report is not None else None


def _settle_interrupted(db: Session, report: Report) -> Report:
    if report.status in (ReportStatus.PENDING, ReportStatus.ANALYZING) and not is_active(report.id):
        logger.warning("Marking interrupted report %s as failed", report.id)
        db.execute(
            update(Report)
            .where(Report.id == report.id, Report.status.in_((ReportStatus.PENDING, ReportStatus.ANALYZING)))
            .values(status=ReportStatus.FAILED, failure_reason=INTERNAL_FAILURE)
        )
        db.commit()
        db.refresh(report)
    return report


def claim_for_analysis(db: Session, report_id: int) -> bool:
    """PENDING 인 리포트를 ANALYZING 으로 선점한다. 이미 다른 곳이 가져갔거나 삭제됐으면 False."""
    result = db.execute(
        update(Report)
        .where(Report.id == report_id, Report.status == ReportStatus.PENDING)
        .values(status=ReportStatus.ANALYZING)
    )
    db.commit()
    return result.rowcount == 1


def is_supported_version(result: AnalysisResult) -> bool:
    """메이저 버전이 같을 때만 이 서버가 해석할 수 있다."""
    return result.version.split(".")[0] == SCHEMA_VERSION.split(".")[0]


def save_success(
    db: Session, report_id: int, result: AnalysisResult, feedback: dict[str, Any] | None = None,
) -> bool:
    """분석 결과와 피드백을 저장하고 COMPLETED 로 바꾼다. 그 사이 리포트가 삭제됐으면 False."""
    outcome = db.execute(
        update(Report)
        .where(Report.id == report_id, Report.status == ReportStatus.ANALYZING)
        .values(
            status=ReportStatus.COMPLETED,
            analysis_result=result.to_json_dict(),
            feedback=feedback,
            schema_version=result.version,
            failure_reason=None,
            completed_at=datetime.now(timezone.utc),
        )
    )
    db.commit()
    return outcome.rowcount == 1


def save_failure(db: Session, report_id: int, code: str) -> bool:
    """실패 사유 코드를 저장하고 FAILED 로 바꾼다. 그 사이 리포트가 삭제됐으면 False."""
    outcome = db.execute(
        update(Report)
        .where(Report.id == report_id, Report.status.in_((ReportStatus.PENDING, ReportStatus.ANALYZING)))
        .values(status=ReportStatus.FAILED, failure_reason=code)
    )
    db.commit()
    return outcome.rowcount == 1
