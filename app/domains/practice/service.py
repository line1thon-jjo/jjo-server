import logging
from typing import BinaryIO

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.storage import RecordingStorage
from app.domains.practice.models import Practice, Recording
from app.domains.practice.schemas import PracticeCreate, RecordingAnalysisInput, RecordingResponse
from app.domains.practice.title import generate_title

logger = logging.getLogger(__name__)


def create_practice(db: Session, *, user_id: int, data: PracticeCreate) -> Practice:
    """검증된 대본으로 연습을 생성하고, ID가 채워진 객체를 반환한다.

    user_id는 이후 HTTP 연결 시 반드시 인증된 사용자 ID를 전달해야 한다.
    이 함수 자체는 로그인이나 사용자 인증을 수행하지 않는다.

    서비스는 flush만 수행한다. 호출자가 트랜잭션의 commit을 책임지며,
    flush/commit 실패를 포함한 예외 발생 시 rollback도 수행해야 한다.
    반환 시점에는 아직 저장이 확정되지 않았다.
    """
    practice = Practice(
        user_id=user_id,
        title=generate_title(data.script),
        script=data.script,
    )
    db.add(practice)
    db.flush()
    return practice


class RecordingNotFoundError(LookupError):
    """미존재와 타인 소유를 구별하지 않는다."""


def get_recording_for_analysis(
    db: Session, *, recording_id: int, user_id: int,
) -> RecordingAnalysisInput:
    """BE2용 새 계약 초안. 인증된 user_id를 전달해야 한다. DB 쓰기는 없다."""
    recording = db.scalar(
        select(Recording).join(Practice, Recording.practice_id == Practice.id)
        .where(Recording.id == recording_id, Practice.user_id == user_id)
    )
    if recording is None:
        raise RecordingNotFoundError()
    return RecordingAnalysisInput(
        recording_id=recording.id, practice_id=recording.practice_id,
        attempt_no=recording.attempt_no, script_snapshot=recording.script_snapshot,
        audio_path=recording.audio_path, duration_ms=recording.duration_ms,
    )


def upload_recording(
    db: Session, *, practice_id: int, user_id: int, source: BinaryIO,
    duration_ms: int, storage: RecordingStorage,
) -> RecordingResponse:
    """파일과 회차를 생성하고 commit한다. 전용 요청 Session으로 호출한다.

    인증 조회가 이미 시작한 트랜잭션도 함께 commit/rollback한다.
    PostgreSQL READ COMMITTED에서 연습 행 잠금으로 번호 계산을 직렬화한다.
    DB 유일 제약은 최종 방어선이며 SQLite에서는 이 잠금이 제공되지 않는다.
    파일 저장소도 부분 저장 실패 시 자체적으로 파일을 정리해야 한다.
    """
    audio_path = None
    try:
        if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or not 1 <= duration_ms <= 2147483647:
            raise ValueError("duration_ms must fit a positive database integer")
        # Reject unauthorized destinations before persisting bytes. Do not lock
        # the row while copying a potentially slow 20 MiB upload to storage.
        if db.scalar(select(Practice.id).where(
            Practice.id == practice_id, Practice.user_id == user_id,
        )) is None:
            raise RecordingNotFoundError()
        audio_path = storage.save(source)
        # Recheck ownership/existence and read the latest script under the lock.
        # A concurrent edit/delete during the file copy must not leak a stale
        # snapshot or leave a file behind.
        practice = db.scalar(
            select(Practice).where(Practice.id == practice_id, Practice.user_id == user_id)
            .with_for_update().execution_options(populate_existing=True)
        )
        if practice is None:
            raise RecordingNotFoundError()
        attempt_no = (db.scalar(select(func.max(Recording.attempt_no)).where(
            Recording.practice_id == practice_id,
        )) or 0) + 1
        recording = Recording(
            practice_id=practice_id, attempt_no=attempt_no,
            script_snapshot=practice.script, audio_path=audio_path, duration_ms=duration_ms,
        )
        db.add(recording)
        db.flush()
        result = RecordingResponse(
            recording_id=recording.id, practice_id=recording.practice_id,
            attempt_no=recording.attempt_no, duration_ms=recording.duration_ms,
            created_at=recording.created_at,
        )
        db.commit()
        return result
    except BaseException:
        try:
            db.rollback()
        except BaseException:
            logger.exception("Failed to roll back a recording transaction")
        if audio_path is not None:
            try:
                storage.delete(audio_path)
            except BaseException:
                logger.exception("Failed to remove a recording after transaction failure")
        raise
