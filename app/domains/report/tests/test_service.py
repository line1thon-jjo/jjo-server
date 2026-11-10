from types import SimpleNamespace

import pytest
from sqlalchemy import JSON, Column, Integer, MetaData, Table, create_engine, delete, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.contracts.analysis_result import AnalysisError, AnalysisResult, ErrorCode, Summary, Syllable
from app.domains.report import service, worker
from app.domains.report.models import Report, ReportStatus
from app.domains.report.service import RecordingNotFoundError

USER_ID = 7
RECORDING_ID = 11


@pytest.fixture
def engine():
    # JSONB·BigInteger 기본키는 SQLite 에서 쓸 수 없어 복사한 테이블 정의만 바꾼다.
    metadata = MetaData()
    Table("recordings", metadata, Column("id", Integer, primary_key=True))  # FK 대상만 있으면 된다
    table = Report.__table__.to_metadata(metadata)
    table.c.id.type = Integer()
    table.c.recording_id.type = Integer()
    table.c.analysis_result.type = JSON()
    table.c.feedback.type = JSON()
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata.create_all(eng)
    return eng


@pytest.fixture
def db(engine):
    with Session(engine, expire_on_commit=False) as session:
        yield session


@pytest.fixture
def factory(engine):
    return lambda: Session(engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def fake_practice(monkeypatch):
    recording = SimpleNamespace(
        recording_id=RECORDING_ID, practice_id=1, attempt_no=1,
        script_snapshot="안녕하세요 여러분", audio_path="/tmp/a.wav", duration_ms=3000,
    )

    def fake_get(db, *, recording_id, user_id):
        if (recording_id, user_id) != (RECORDING_ID, USER_ID):
            raise RecordingNotFoundError()
        return recording

    monkeypatch.setattr(service, "get_recording_for_analysis", fake_get)
    yield recording
    service._ACTIVE.clear()


def make_result(**overrides) -> AnalysisResult:
    data = dict(
        duration_ms=3000, spoken_text="안녕하세요 여러분",
        syllables=[Syllable(idx=0, char="안", start_ms=0, end_ms=100)],
        pauses=[], phrases=[], habits=[],
        summary=Summary(avg_syllables_per_sec=5.0, articulation_syllables_per_sec=6.0, aligned_ratio=1.0),
    )
    data.update(overrides)
    return AnalysisResult(**data)


def start(db):
    return service.start_analysis(db, user_id=USER_ID, recording_id=RECORDING_ID)


def reload(engine) -> Report:
    with Session(engine) as session:
        return session.scalar(select(Report))


# ---- 시작 ----

def test_start_creates_pending_report_and_job(db, fake_practice):
    report, job = start(db)
    assert report.status == ReportStatus.PENDING and report.recording_id == RECORDING_ID
    assert job is fake_practice
    assert service.is_active(report.id)


def test_start_is_idempotent_while_in_progress(db):
    first, job1 = start(db)
    second, job2 = start(db)
    assert job1 is not None and job2 is None
    assert second.id == first.id


def test_start_unknown_or_foreign_recording_raises(db):
    with pytest.raises(RecordingNotFoundError):
        service.start_analysis(db, user_id=USER_ID + 1, recording_id=RECORDING_ID)
    with pytest.raises(RecordingNotFoundError):
        service.start_analysis(db, user_id=USER_ID, recording_id=999)


def test_start_does_not_rerun_completed(db, factory):
    report, job = start(db)
    worker.run_job(report.id, job, lambda path, script: make_result(), factory)
    again, job2 = start(db)
    assert again.status == ReportStatus.COMPLETED and job2 is None


# ---- 실행 ----

def test_run_job_success_saves_raw_result(engine, db, factory):
    report, job = start(db)
    seen = []
    worker.run_job(report.id, job, lambda path, script: seen.append((path, script)) or make_result(), factory)
    saved = reload(engine)
    assert seen == [("/tmp/a.wav", "안녕하세요 여러분")]
    assert saved.status == ReportStatus.COMPLETED
    assert saved.schema_version == "1.1" and saved.completed_at is not None
    assert saved.analysis_result["spokenText"] == "안녕하세요 여러분"  # BE3 JSON(camelCase) 그대로
    assert saved.analysis_result["syllables"][0]["char"] == "안"
    assert not service.is_active(report.id)


def test_run_job_analysis_error_saves_code(engine, db, factory):
    report, job = start(db)

    def fail(path, script):
        raise AnalysisError(ErrorCode.NO_SPEECH, "말소리 없음")

    worker.run_job(report.id, job, fail, factory)
    saved = reload(engine)
    assert saved.status == ReportStatus.FAILED and saved.failure_reason == "NO_SPEECH"
    assert saved.analysis_result is None


def test_run_job_unexpected_error_becomes_internal(engine, db, factory):
    report, job = start(db)

    def crash(path, script):
        raise RuntimeError("boom")

    worker.run_job(report.id, job, crash, factory)
    saved = reload(engine)
    assert saved.status == ReportStatus.FAILED and saved.failure_reason == "INTERNAL"


def test_run_job_unsupported_schema_version_fails(engine, db, factory):
    report, job = start(db)
    worker.run_job(report.id, job, lambda path, script: make_result(version="2.0"), factory)
    saved = reload(engine)
    assert saved.status == ReportStatus.FAILED and saved.failure_reason == "INTERNAL"
    assert saved.analysis_result is None


def test_run_job_minor_version_difference_is_accepted(engine, db, factory):
    report, job = start(db)
    worker.run_job(report.id, job, lambda path, script: make_result(version="1.2"), factory)
    saved = reload(engine)
    assert saved.status == ReportStatus.COMPLETED and saved.schema_version == "1.2"


def test_run_job_skips_when_already_claimed(db, factory):
    report, job = start(db)
    calls = []
    analyze = lambda path, script: calls.append(1) or make_result()
    worker.run_job(report.id, job, analyze, factory)
    worker.run_job(report.id, job, analyze, factory)  # 이미 COMPLETED 라 선점 실패
    assert calls == [1]


def test_run_job_survives_report_deleted_during_analysis(engine, db, factory):
    report, job = start(db)

    def analyze_and_delete(path, script):
        with Session(engine) as other:
            other.execute(delete(Report))
            other.commit()
        return make_result()

    worker.run_job(report.id, job, analyze_and_delete, factory)  # 예외 없이 끝나야 한다
    assert reload(engine) is None
    assert not service.is_active(report.id)


def test_enqueue_runs_in_worker_thread(engine, db, factory):
    report, job = start(db)
    worker.enqueue(report.id, job, lambda path, script: make_result(), factory).result(timeout=10)
    assert reload(engine).status == ReportStatus.COMPLETED


# ---- 재시도와 끊긴 작업 ----

def test_restart_after_failure_resets_report(engine, db, factory):
    report, job = start(db)
    worker.run_job(report.id, job, lambda p, s: (_ for _ in ()).throw(AnalysisError(ErrorCode.AUDIO_TOO_SHORT, "짧음")), factory)
    assert reload(engine).status == ReportStatus.FAILED
    retried, job2 = start(db)
    assert job2 is not None and retried.id == report.id
    assert retried.status == ReportStatus.PENDING and retried.failure_reason is None


def test_get_report_returns_none_when_missing(db):
    assert service.get_report(db, user_id=USER_ID, recording_id=RECORDING_ID) is None
    with pytest.raises(RecordingNotFoundError):
        service.get_report(db, user_id=USER_ID + 1, recording_id=RECORDING_ID)


def test_get_report_keeps_active_report(db):
    report, _ = start(db)
    found = service.get_report(db, user_id=USER_ID, recording_id=RECORDING_ID)
    assert found.status == ReportStatus.PENDING and found.id == report.id


def test_get_report_marks_interrupted_report_failed(db):
    report, _ = start(db)
    service.clear_active(report.id)  # 서버가 다시 시작돼 작업 기록이 사라진 상황
    found = service.get_report(db, user_id=USER_ID, recording_id=RECORDING_ID)
    assert found.status == ReportStatus.FAILED and found.failure_reason == "INTERNAL"


def test_restart_of_interrupted_report_runs_again(db):
    report, _ = start(db)
    service.clear_active(report.id)
    again, job = start(db)
    assert job is not None and again.status == ReportStatus.PENDING
