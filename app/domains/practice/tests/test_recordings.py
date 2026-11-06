"""업로드 API, 저장 실패 보상 및 실제 PostgreSQL 회차 경쟁 검증.

실행: AUTH_TEST_DATABASE_URL=postgresql+psycopg://... pytest -q tests app/domains/practice/tests
PostgreSQL fixture는 임의 스키마를 생성/삭제하며 공용 테이블은 사용하지 않는다.
"""

from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from threading import Barrier, Event
from time import monotonic, sleep
import wave

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Integer, MetaData, event, func, inspect, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.storage import (
    AudioValidationError, CHUNK_BYTES, MAX_AUDIO_BYTES, LocalRecordingStorage,
    get_recording_storage,
)
from app.domains.auth.models import User
from app.domains.practice import router as routes
from app.domains.practice.models import Practice, Recording
from app.domains.practice.service import (
    RecordingNotFoundError, get_recording_for_analysis, upload_recording,
)
from app.domains.practice.tests.test_api import api
from app.main import app
from tests.test_auth_api import client


# WAV is an actual small PCM file. Other fixtures exercise container headers
# only: these tests do not claim to decode WebM/M4A/MP3.
_wav = BytesIO()
with wave.open(_wav, "wb") as _writer:
    _writer.setnchannels(1)
    _writer.setsampwidth(2)
    _writer.setframerate(8000)
    _writer.writeframes(b"\x00\x00" * 8)
WAV = _wav.getvalue()
WEBM = b"\x1a\x45\xdf\xa3\x87\x42\x82\x84webm"
M4A = (20).to_bytes(4, "big") + b"ftypM4A " + b"\x00" * 4 + b"isom"
MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00"


@pytest.fixture
def recording_api(api, tmp_path):
    c, engine, headers, uid = api
    metadata = MetaData()
    User.__table__.to_metadata(metadata)
    Practice.__table__.to_metadata(metadata)
    table = Recording.__table__.to_metadata(metadata)
    if engine.dialect.name == "sqlite":
        table.c.id.type = Integer()
    table.create(engine)
    storage = LocalRecordingStorage(tmp_path / "audio")
    app.dependency_overrides[get_recording_storage] = lambda: storage
    response = c.post("/practices", headers=headers, json={"script": " 원문\n대본 "})
    assert response.status_code == 201
    try:
        yield c, engine, headers, uid, response.json()["id"], storage
    finally:
        app.dependency_overrides.pop(get_recording_storage, None)


def post(env, content=WAV, *, duration="1234", headers=None, practice_id=None):
    c, _, owner_headers, _, pid, _ = env
    return c.post(
        f"/practices/{pid if practice_id is None else practice_id}/recordings",
        headers=owner_headers if headers is None else headers,
        files={"file": ("../../untrusted.exe", content, "application/octet-stream")},
        data={"duration_ms": duration},
    )


def saved_count(engine):
    with Session(engine) as db:
        return db.scalar(select(func.count()).select_from(Recording))


@pytest.mark.parametrize("content,suffix", [(WAV, ".wav"), (WEBM, ".webm"),
                                            (M4A, ".m4a"), (MP3, ".mp3"),
                                            (b"\xff\xfb\x90\x00", ".mp3")])
def test_upload_and_be2_dto(recording_api, content, suffix):
    _, engine, _, uid, pid, storage = recording_api
    response = post(recording_api, content)
    assert response.status_code == 201, response.text
    result = response.json()
    assert set(result) == {"recording_id", "practice_id", "attempt_no", "duration_ms", "created_at"}
    assert result["practice_id"] == pid and result["attempt_no"] == 1
    assert result["duration_ms"] == 1234 and isinstance(result["recording_id"], int)
    assert result["created_at"]
    with Session(engine) as db:
        dto = get_recording_for_analysis(db, recording_id=result["recording_id"], user_id=uid)
        assert dto.script_snapshot == " 원문\n대본 "
        assert dto.duration_ms == 1234 and dto.attempt_no == 1 and dto.practice_id == pid
        assert dto.recording_id == result["recording_id"]
        path = Path(dto.audio_path)
        assert path.is_absolute() and path.parent == storage.root
        assert path.suffix == suffix and path.read_bytes() == content
        assert not isinstance(dto, Recording)
        assert "audio_path" not in result
        for rid, owner in [(dto.recording_id, uid + 999), (dto.recording_id + 999, uid)]:
            with pytest.raises(RecordingNotFoundError):
                get_recording_for_analysis(db, recording_id=rid, user_id=owner)


def test_auth_and_ownership(recording_api):
    _, engine, _, _, _, storage = recording_api
    assert post(recording_api, headers={}).status_code == 401
    assert post(recording_api, headers={"Authorization": "Bearer invalid"}).status_code == 401
    assert post(recording_api, practice_id=999999).status_code == 404
    with Session(engine) as db:
        user = User(google_sub="other-recording-owner", name="Other", email="other@test.invalid")
        db.add(user)
        db.flush()
        practice = Practice(user_id=user.id, title="Other", script="private")
        db.add(practice)
        db.commit()
        other_id = practice.id
    response = post(recording_api, practice_id=other_id)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PRACTICE_NOT_FOUND"
    assert saved_count(engine) == 0
    assert not storage.root.exists()


@pytest.mark.parametrize("content,status", [(b"", 422), (b"not audio", 415),
    (b"RIFF1234NOPE", 415), (b"ID3\xff" + b"\x00" * 6, 415),
    (b"\xff\xfd\x90\x00", 415),  # MPEG Layer II is not MP3 (Layer III).
    (b"\x1a\x45\xdf\xa3matroska", 415),
    (WAV + b"\x00" * MAX_AUDIO_BYTES, 413)])
def test_invalid_files_leave_nothing(recording_api, content, status):
    response = post(recording_api, content)
    assert response.status_code == status, response.text
    assert saved_count(recording_api[1]) == 0
    assert list(recording_api[5].root.glob("*")) == []


@pytest.mark.parametrize("duration", ["0", "-1", "1.5", "nan", "", "true", "2147483648"])
def test_duration_rejected(recording_api, duration):
    assert post(recording_api, duration=duration).status_code == 422
    assert saved_count(recording_api[1]) == 0
    assert not recording_api[5].root.exists()


def test_required_fields(recording_api):
    c, engine, headers, _, pid, _ = recording_api
    url = f"/practices/{pid}/recordings"
    assert c.post(url, headers=headers, data={"duration_ms": "3"}).status_code == 422
    assert c.post(url, headers=headers, files={"file": ("x.wav", WAV)}).status_code == 422
    assert saved_count(engine) == 0


def test_missing_multipart_dependency_preserves_existing_api(recording_api, monkeypatch):
    c, engine, _, _, _, storage = recording_api
    monkeypatch.setattr(routes, "multipart_available", lambda: False)
    assert c.get("/health").status_code == 200
    assert post(recording_api, headers={}).status_code == 401
    response = post(recording_api)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "UPLOAD_CONFIGURATION_ERROR"
    assert saved_count(engine) == 0 and not storage.root.exists()


def test_new_attempt_preserves_snapshot_and_server_fields(recording_api):
    c, engine, headers, uid, pid, _ = recording_api
    first = post(recording_api).json()
    with Session(engine) as db:
        db.get(Practice, pid).script = "수정한 대본"
        db.commit()
    second_response = c.post(
        f"/practices/{pid}/recordings", headers=headers,
        files={"file": ("x.wav", WAV)},
        data={"duration_ms": "20", "attempt_no": "99", "script_snapshot": "forged", "user_id": "999"},
    )
    assert second_response.status_code == 201
    second = second_response.json()
    assert [first["attempt_no"], second["attempt_no"]] == [1, 2]
    assert first["recording_id"] != second["recording_id"]
    with Session(engine) as db:
        old = get_recording_for_analysis(db, recording_id=first["recording_id"], user_id=uid)
        new = get_recording_for_analysis(db, recording_id=second["recording_id"], user_id=uid)
        assert old.script_snapshot == " 원문\n대본 "
        assert new.script_snapshot == "수정한 대본"
        assert old.audio_path != new.audio_path


@pytest.mark.parametrize("stage", ["save", "flush", "commit"])
def test_failures_rollback_and_remove_files(recording_api, monkeypatch, stage):
    _, engine, _, _, _, storage = recording_api
    original_upload = routes.upload_recording

    def fail(db, **kwargs):
        if stage == "save":
            def broken(_):
                raise OSError("private-storage-path")
            patch.setattr(storage, "save", broken)
        elif stage == "flush":
            original_flush = db.flush
            def flush(*args, **kw):
                original_flush(*args, **kw)
                raise SQLAlchemyError("private-database-error")
            patch.setattr(db, "flush", flush)
        else:
            def commit(_):
                raise SQLAlchemyError("private-database-error")
            event.listen(db, "before_commit", commit, once=True)
        return original_upload(db, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(routes, "upload_recording", fail)
        # Existing server behavior: exceptions reach the server's generic 500 handler.
        with pytest.raises((OSError, SQLAlchemyError)):
            post(recording_api)
        with TestClient(app, raise_server_exceptions=False) as client_no_raise:
            response = post((client_no_raise, *recording_api[1:]))
        assert response.status_code == 500
        assert response.text == "Internal Server Error"
        assert saved_count(engine) == 0
        assert list(storage.root.glob("*")) == []
    response = post(recording_api)
    assert response.status_code == 201 and response.json()["attempt_no"] == 1


def test_storage_read_failure_removes_partial_file(tmp_path):
    storage = LocalRecordingStorage(tmp_path)
    class BrokenSource:
        calls = 0
        def read(self, size):
            assert 0 < size <= CHUNK_BYTES
            self.calls += 1
            if self.calls == 1:
                return WAV
            raise OSError("read failure")
    with pytest.raises(OSError):
        storage.save(BrokenSource())
    assert list(tmp_path.iterdir()) == []


def test_storage_write_failure_removes_partial_file(tmp_path, monkeypatch):
    storage = LocalRecordingStorage(tmp_path)
    original_open = Path.open
    class FailingWriter:
        def __init__(self, target):
            self.target = target
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.target.close()
        def write(self, data):
            self.target.write(data[:3])
            raise OSError("disk full")
    def broken_open(path, *args, **kwargs):
        return FailingWriter(original_open(path, *args, **kwargs))
    monkeypatch.setattr(Path, "open", broken_open)
    with pytest.raises(OSError, match="disk full"):
        storage.save(BytesIO(WAV))
    assert list(tmp_path.iterdir()) == []


def test_storage_exact_limit_and_bounded_reads(tmp_path):
    storage = LocalRecordingStorage(tmp_path)
    class BoundedSource(BytesIO):
        def read(self, size=-1):
            assert 0 < size <= CHUNK_BYTES
            return super().read(size)
    content = WAV + b"\x00" * (MAX_AUDIO_BYTES - len(WAV))
    path = storage.save(BoundedSource(content))
    assert Path(path).stat().st_size == MAX_AUDIO_BYTES
    storage.delete(path)
    with pytest.raises(AudioValidationError) as error:
        storage.save(BoundedSource(content + b"x"))
    assert error.value.status_code == 413
    assert list(tmp_path.iterdir()) == []


def test_postgresql_concurrent_attempts_and_unique_constraint(recording_api):
    _, engine, _, uid, pid, storage = recording_api
    if engine.dialect.name != "postgresql":
        pytest.skip("Row locks require real PostgreSQL; SQLite is not evidence")
    with engine.connect() as connection:
        assert connection.get_isolation_level() == "READ COMMITTED"
        schema = engine.get_execution_options()["schema_translate_map"][None]
        constraints = inspect(connection).get_unique_constraints("recordings", schema=schema)
        assert any(set(item["column_names"]) == {"practice_id", "attempt_no"} for item in constraints)
    barrier = Barrier(6)
    def worker(_):
        with Session(engine) as db:
            backend = db.scalar(select(func.pg_backend_pid()))
            transaction = db.scalar(select(func.txid_current()))
            barrier.wait(timeout=10)
            result = upload_recording(db, practice_id=pid, user_id=uid, source=BytesIO(WAV),
                                      duration_ms=10, storage=storage)
            return result, backend, transaction
    with ThreadPoolExecutor(max_workers=6) as pool:
        workers = list(pool.map(worker, range(6)))
    assert len({item[1] for item in workers}) == 6  # Six physical connections.
    assert len({item[2] for item in workers}) == 6  # Six real transactions.
    results = [item[0] for item in workers]
    assert sorted(item.attempt_no for item in results) == list(range(1, 7))
    assert len({item.recording_id for item in results}) == 6
    assert len(list(storage.root.iterdir())) == saved_count(engine) == 6
    with Session(engine) as db:
        db.add(Recording(practice_id=pid, attempt_no=1, script_snapshot="duplicate",
                         audio_path="unused", duration_ms=10))
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()
    assert saved_count(engine) == 6


def test_postgresql_second_upload_waits_for_practice_lock(recording_api):
    _, engine, _, uid, pid, storage = recording_api
    if engine.dialect.name != "postgresql":
        pytest.skip("Requires PostgreSQL row locks")
    first_locked, release_first, second_select, second_finished = (Event() for _ in range(4))
    backends = {}

    def observe(conn, cursor, statement, parameters, context, executemany):
        if "FOR UPDATE" in statement and first_locked.is_set():
            second_select.set()
    event.listen(engine, "before_cursor_execute", observe)
    def worker(first):
        with Session(engine) as db:
            backends[first] = db.scalar(select(func.pg_backend_pid()))
            if first:
                def pause_flush(session, flush_context, instances):
                    first_locked.set()  # Real SELECT FOR UPDATE has returned.
                    assert release_first.wait(10)
                event.listen(db, "before_flush", pause_flush, once=True)
            result = upload_recording(db, practice_id=pid, user_id=uid, source=BytesIO(WAV),
                                      duration_ms=10, storage=storage)
            if not first:
                second_finished.set()
            return result
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            one = pool.submit(worker, True)
            try:
                assert first_locked.wait(10)
                two = pool.submit(worker, False)
                assert second_select.wait(10)
                assert backends[True] != backends[False]
                deadline = monotonic() + 5
                with engine.connect() as observer:
                    while True:
                        blockers = observer.scalar(select(func.pg_blocking_pids(backends[False])))
                        if backends[True] in blockers:
                            break
                        assert monotonic() < deadline, "Second connection never waited for the row lock"
                        sleep(0.01)
                assert not second_finished.is_set()
            finally:
                release_first.set()
            assert one.result(timeout=10).attempt_no == 1
            assert two.result(timeout=10).attempt_no == 2
    finally:
        release_first.set()
        event.remove(engine, "before_cursor_execute", observe)


@pytest.mark.parametrize("change", ["edit", "delete"])
def test_postgresql_file_copy_does_not_hold_row_lock_and_rechecks_practice(recording_api, change):
    _, engine, _, uid, pid, storage = recording_api
    if engine.dialect.name != "postgresql":
        pytest.skip("Separate transactions and row lock timing require PostgreSQL")
    class ChangingStorage:
        def save(self, source):
            path = storage.save(source)
            try:
                with Session(engine) as editor:
                    editor.execute(text("SET LOCAL lock_timeout = '500ms'"))
                    practice = editor.get(Practice, pid)
                    if change == "edit":
                        practice.script = "파일 저장 중 수정한 대본"
                    else:
                        editor.delete(practice)
                    editor.commit()  # Fails if upload already holds the row lock.
            except BaseException:
                storage.delete(path)
                raise
            return path
        def delete(self, path):
            storage.delete(path)
    with Session(engine) as db:
        if change == "delete":
            with pytest.raises(RecordingNotFoundError):
                upload_recording(db, practice_id=pid, user_id=uid, source=BytesIO(WAV),
                                 duration_ms=10, storage=ChangingStorage())
            assert saved_count(engine) == 0 and list(storage.root.iterdir()) == []
        else:
            result = upload_recording(db, practice_id=pid, user_id=uid, source=BytesIO(WAV),
                                      duration_ms=10, storage=ChangingStorage())
            dto = get_recording_for_analysis(db, recording_id=result.recording_id, user_id=uid)
            assert dto.script_snapshot == "파일 저장 중 수정한 대본"


def test_delete_failure_preserves_db_error(recording_api, monkeypatch, caplog):
    _, engine, _, _, _, storage = recording_api
    original = routes.upload_recording
    failure = SQLAlchemyError("original DB failure")
    def fail(db, **kwargs):
        def fail_commit(session):
            raise failure
        event.listen(db, "before_commit", fail_commit, once=True)
        return original(db, **kwargs)
    def fail_delete(path):
        raise OSError("cleanup disk error")
    monkeypatch.setattr(routes, "upload_recording", fail)
    monkeypatch.setattr(storage, "delete", fail_delete)
    with pytest.raises(SQLAlchemyError) as caught:
        post(recording_api)
    assert caught.value is failure
    assert saved_count(engine) == 0
    assert "Failed to remove a recording" in caplog.text
    # A genuine unlink failure cannot promise no orphan. Log for recovery,
    # preserve the DB error, and clean up the injected failure's file here.
    for path in storage.root.iterdir():
        path.unlink()


def test_storage_cleanup_failure_preserves_write_error(tmp_path, monkeypatch, caplog):
    storage = LocalRecordingStorage(tmp_path)
    failure = OSError("original read error")
    class Source:
        first = True
        def read(self, size):
            if self.first:
                self.first = False
                return WAV
            raise failure
    original_unlink = Path.unlink
    def fail_unlink(path, **kwargs):
        raise OSError("cleanup failure")
    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_unlink)
        with pytest.raises(OSError) as caught:
            storage.save(Source())
    assert caught.value is failure
    assert "Failed to remove a partial recording" in caplog.text
    for path in tmp_path.iterdir():
        original_unlink(path)
