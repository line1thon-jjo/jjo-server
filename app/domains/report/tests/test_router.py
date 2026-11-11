"""report 라우터: 실제 로그인 토큰과 테스트 DB로 HTTP 흐름을 확인한다."""
import pytest
from sqlalchemy import JSON, Integer, MetaData
from sqlalchemy.orm import Session

from app.contracts.analysis_result import (
    AnalysisError, AnalysisResult, ErrorCode, Habit, HabitType, IdxRange, Pause, PauseBoundary,
    Phrase, SpeedLabel, Summary, Syllable, UnclearRange, UnclearReason,
)
from app.domains.auth.models import User
from app.domains.practice.models import Practice, Recording
from app.domains.report import audio, service, worker
from app.domains.report import router as routes
from app.domains.report.models import Report
from tests.test_auth_api import client, pending, signup  # noqa: F401  (client 는 pytest fixture)

TEXT = "안녕하세요 여러분, 반갑습니다."
WAV_BYTES = b"0123456789" * 10


@pytest.fixture
def env(client, tmp_path, monkeypatch):  # noqa: F811
    c, engine = client
    metadata = MetaData()
    User.__table__.to_metadata(metadata)
    practices = Practice.__table__.to_metadata(metadata)
    recordings = Recording.__table__.to_metadata(metadata)
    reports = Report.__table__.to_metadata(metadata)
    if engine.dialect.name == "sqlite":
        for table in (practices, recordings, reports):
            table.c.id.type = Integer()
        reports.c.analysis_result.type = JSON()
        reports.c.feedback.type = JSON()
    for table in (practices, recordings, reports):
        table.create(engine)

    account = signup(c, pending(c)).json()
    headers = {"Authorization": "Bearer " + account["access_token"]}
    uid = account["user"]["id"]
    audio_file = tmp_path / "rec.wav"
    audio_file.write_bytes(WAV_BYTES)
    with Session(engine, expire_on_commit=False) as db:
        other = User(google_sub="other", email="other@example.test", name="Other")
        db.add(other)
        db.flush()
        mine = Practice(user_id=uid, title="중문이 기말 발표", script=TEXT)
        theirs = Practice(user_id=other.id, title="남의 연습", script=TEXT)
        db.add_all([mine, theirs])
        db.flush()
        own = Recording(practice_id=mine.id, attempt_no=2, script_snapshot=TEXT,
                        audio_path=str(audio_file), duration_ms=3000)
        foreign = Recording(practice_id=theirs.id, attempt_no=1, script_snapshot=TEXT,
                            audio_path=str(audio_file), duration_ms=3000)
        db.add_all([own, foreign])
        db.commit()
        ids = SimpleIds(own=own.id, foreign=foreign.id)

    jobs = []
    monkeypatch.setattr(routes.worker, "enqueue", lambda report_id, job, *a, **k: jobs.append((report_id, job)))
    yield SimpleEnv(c, engine, headers, ids, jobs, audio_file)
    service._ACTIVE.clear()


class SimpleIds:
    def __init__(self, own, foreign):
        self.own, self.foreign = own, foreign


class SimpleEnv:
    def __init__(self, c, engine, headers, ids, jobs, audio_file):
        self.c, self.engine, self.headers, self.ids, self.jobs, self.audio_file = c, engine, headers, ids, jobs, audio_file

    def start(self, recording_id=None):
        return self.c.post("/reports", headers=self.headers, json={"recording_id": recording_id or self.ids.own})

    def run_pending_job(self, analyze):
        report_id, job = self.jobs.pop(0)
        worker.run_job(report_id, job, analyze, lambda: Session(self.engine, expire_on_commit=False))


def make_result(**overrides) -> AnalysisResult:
    syllables = []
    for i, ch in enumerate(TEXT):
        if "가" <= ch <= "힣":
            kwargs = dict(idx=i, char=ch, start_ms=i * 100, end_ms=i * 100 + 100)
            if i == 1:
                kwargs.update(pitch_level=7)
            if i == 4:
                kwargs.update(stretch=True, stretch_ratio=2.9)
            syllables.append(Syllable(**kwargs))
    data = dict(
        duration_ms=3000, spoken_text=TEXT, syllables=syllables,
        pauses=[Pause(after_idx=8, duration_ms=800, boundary=PauseBoundary.WORD)],
        phrases=[
            Phrase(start_idx=0, end_idx=4, speed_syll_per_sec=7.0, speed_ratio=1.5, speed_label=SpeedLabel.FAST),
            Phrase(start_idx=6, end_idx=8, speed_syll_per_sec=5.0, speed_ratio=1.0, speed_label=SpeedLabel.NORMAL),
        ],
        habits=[Habit(type=HabitType.END_RISE, count=3, occurrences=[IdxRange(start_idx=4, end_idx=4)], first_idx=4)],
        unclear_ranges=[UnclearRange(start_idx=11, end_idx=12, reason=UnclearReason.SKIPPED, confidence=0.9)],
        warnings=["PARTIAL_ALIGNMENT", "SOMETHING_ELSE"],
        summary=Summary(avg_syllables_per_sec=5.0, articulation_syllables_per_sec=6.0, aligned_ratio=0.8),
    )
    data.update(overrides)
    return AnalysisResult(**data)


def error_code(response):
    return response.json()["error"]["code"]


# ---- 인증과 시작 ----

def test_endpoints_require_login(env):
    for method, path in [("post", "/reports"), ("get", f"/reports/{env.ids.own}"),
                         ("get", f"/reports/{env.ids.own}/overview"), ("get", f"/reports/{env.ids.own}/utterance"),
                         ("get", f"/reports/{env.ids.own}/audio-url")]:
        response = getattr(env.c, method)(path, **({"json": {"recording_id": env.ids.own}} if method == "post" else {}))
        assert response.status_code == 401 and error_code(response) == "ACCESS_CREDENTIAL_INVALID"


def test_start_returns_pending_with_header_and_enqueues_once(env):
    response = env.start()
    assert response.status_code == 202, response.text
    assert response.json() == {
        "recording_id": env.ids.own, "status": "PENDING", "practice_title": "중문이 기말 발표",
        "attempt_no": 2, "duration_ms": 3000, "warnings": [],
    }
    assert len(env.jobs) == 1
    assert env.start().status_code == 202 and len(env.jobs) == 1  # 이미 진행 중이면 다시 시작하지 않음


def test_start_rejects_foreign_unknown_and_extra_fields(env):
    assert error_code(env.start(env.ids.foreign)) == "RECORDING_NOT_FOUND"
    assert error_code(env.start(999999)) == "RECORDING_NOT_FOUND"
    bad = env.c.post("/reports", headers=env.headers, json={"recording_id": env.ids.own, "user_id": 1})
    assert bad.status_code == 422 and bad.json()["error"]["fields"] == ["user_id"]
    assert env.jobs == []


# ---- 상태 ----

def test_status_before_start_and_for_foreign_recording(env):
    response = env.c.get(f"/reports/{env.ids.own}", headers=env.headers)
    assert response.status_code == 404 and error_code(response) == "REPORT_NOT_FOUND"
    assert error_code(env.c.get(f"/reports/{env.ids.foreign}", headers=env.headers)) == "RECORDING_NOT_FOUND"


def test_results_not_ready_until_completed(env):
    env.start()
    for tab in ("overview", "utterance"):
        response = env.c.get(f"/reports/{env.ids.own}/{tab}", headers=env.headers)
        assert response.status_code == 409 and error_code(response) == "REPORT_NOT_COMPLETED"
    assert env.c.get(f"/reports/{env.ids.own}", headers=env.headers).json()["status"] == "PENDING"


def test_failed_report_exposes_only_failure_code(env):
    env.start()

    def no_speech(path, script):
        raise AnalysisError(ErrorCode.NO_SPEECH, "none")

    env.run_pending_job(no_speech)
    body = env.c.get(f"/reports/{env.ids.own}", headers=env.headers).json()
    assert body["status"] == "FAILED" and body["failure"] == {"code": "NO_SPEECH"} and body["warnings"] == []
    response = env.c.get(f"/reports/{env.ids.own}/utterance", headers=env.headers)
    assert response.status_code == 409


def test_failed_report_can_be_restarted(env):
    env.start()
    env.run_pending_job(lambda p, s: (_ for _ in ()).throw(RuntimeError("boom")))
    assert env.c.get(f"/reports/{env.ids.own}", headers=env.headers).json()["failure"] == {"code": "INTERNAL"}
    assert env.start().json()["status"] == "PENDING" and len(env.jobs) == 1


# ---- 완료된 결과 ----

def test_completed_status_overview_and_utterance(env):
    env.start()
    env.run_pending_job(lambda p, s: make_result())
    status = env.c.get(f"/reports/{env.ids.own}", headers=env.headers).json()
    assert status["status"] == "COMPLETED" and "failure" not in status
    assert status["warnings"] == ["PARTIAL_ALIGNMENT"]  # 정의되지 않은 경고 코드는 내려가지 않는다

    overview = env.c.get(f"/reports/{env.ids.own}/overview", headers=env.headers).json()
    assert overview["speed"] == {"fast": 1, "normal": 1, "slow": 0, "measured": 2}
    assert overview["habits"] == [{"type": "END_RISE", "count": 3}]
    assert overview["summary"]["headline"] and overview["summary"]["footer"]  # 키가 없어 규칙 총평

    utterance = env.c.get(f"/reports/{env.ids.own}/utterance", headers=env.headers).json()
    assert utterance["spoken_text"] == TEXT
    assert utterance["marks"] == [{"idx": 1, "pitch": 7}, {"idx": 4, "stretch": 2.9}]  # 값이 없는 필드는 아예 없음
    assert utterance["paces"] == [{"start_idx": 0, "end_idx": 4, "label": "FAST"}]
    assert utterance["pauses"] == [{"after_idx": 8, "duration_ms": 800}]
    assert utterance["skipped"] == [{"start_idx": 11, "end_idx": 12}]
    assert utterance["replaced"] == [] and utterance["added"] == []
    assert utterance["segments"][0]["start_idx"] == 0 and utterance["segments"][-1]["end_idx"] == len(TEXT) - 1


def test_feedback_tab_uses_saved_feedback(env):
    env.start()
    saved = {
        "version": 1, "source": "gemini", "model": "m",
        "summary": {"headline": "안정적이었어요.", "footer": "끝 올림만 다듬어 보세요."},
        "items": [{"rank": 1, "title": "끝 내리기", "quote": "안녕하세요", "body": "끝이 올라갔어요.",
                   "start_ms": 0, "end_ms": 500, "start_idx": 0, "end_idx": 4, "kind": "END_RISE"}],
    }
    report_id, job = env.jobs.pop(0)
    worker.run_job(report_id, job, lambda p, s: make_result(), lambda: Session(env.engine, expire_on_commit=False),
                   lambda result: saved)
    body = env.c.get(f"/reports/{env.ids.own}/feedback", headers=env.headers).json()
    assert body == {"items": saved["items"]}  # source·model 은 내려가지 않음
    summary = env.c.get(f"/reports/{env.ids.own}/overview", headers=env.headers).json()["summary"]
    assert summary == saved["summary"]


def test_feedback_tab_falls_back_when_not_saved(env):
    env.start()
    report_id, job = env.jobs.pop(0)
    worker.run_job(report_id, job, lambda p, s: make_result(), lambda: Session(env.engine, expire_on_commit=False),
                   lambda result: (_ for _ in ()).throw(RuntimeError("feedback bug")))
    assert env.c.get(f"/reports/{env.ids.own}", headers=env.headers).json()["status"] == "COMPLETED"
    items = env.c.get(f"/reports/{env.ids.own}/feedback", headers=env.headers).json()["items"]
    assert 1 <= len(items) <= 3 and [i["rank"] for i in items] == list(range(1, len(items) + 1))
    for item in items:
        assert item["quote"] == TEXT[item["start_idx"]:item["end_idx"] + 1]


def test_feedback_tab_requires_completion_and_login(env):
    env.start()
    assert error_code(env.c.get(f"/reports/{env.ids.own}/feedback", headers=env.headers)) == "REPORT_NOT_COMPLETED"
    assert env.c.get(f"/reports/{env.ids.own}/feedback").status_code == 401
    assert error_code(env.c.get(f"/reports/{env.ids.foreign}/feedback", headers=env.headers)) == "RECORDING_NOT_FOUND"


def test_corrupt_stored_result_is_reported_not_crashed(env):
    env.start()
    env.run_pending_job(lambda p, s: make_result())
    with Session(env.engine) as db:
        report = db.query(Report).one()
        report.analysis_result = {"unexpected": True}
        db.commit()
    response = env.c.get(f"/reports/{env.ids.own}/overview", headers=env.headers)
    assert response.status_code == 500 and error_code(response) == "REPORT_DATA_INVALID"


# ---- 재생 ----

def test_audio_url_serves_file_with_range_and_no_login_header(env):
    body = env.c.get(f"/reports/{env.ids.own}/audio-url", headers=env.headers).json()
    assert body["expires_in_seconds"] == 1800
    url = body["audio_url"]
    assert url.startswith(f"/reports/{env.ids.own}/audio?token=")

    full = env.c.get(url)  # Authorization 헤더 없이
    assert full.status_code == 200 and full.content == WAV_BYTES
    assert full.headers["content-type"].startswith("audio/wav")
    ranged = env.c.get(url, headers={"Range": "bytes=0-3"})
    assert ranged.status_code == 206 and ranged.content == b"0123"


def test_audio_url_for_foreign_recording_is_hidden(env):
    assert error_code(env.c.get(f"/reports/{env.ids.foreign}/audio-url", headers=env.headers)) == "RECORDING_NOT_FOUND"


def test_audio_rejects_bad_tokens(env):
    own, foreign = env.ids.own, env.ids.foreign
    url = env.c.get(f"/reports/{own}/audio-url", headers=env.headers).json()["audio_url"]
    token = url.split("token=")[1]

    for bad in ("garbage", token[:-3] + "abc"):
        response = env.c.get(f"/reports/{own}/audio", params={"token": bad})
        assert response.status_code == 401 and error_code(response) == "AUDIO_TOKEN_INVALID"
    # 다른 녹음 주소에 쓸 수 없음
    assert error_code(env.c.get(f"/reports/{foreign}/audio", params={"token": token})) == "AUDIO_TOKEN_INVALID"
    # 로그인 토큰은 재생 토큰으로 쓸 수 없음
    access = env.headers["Authorization"].split(" ")[1]
    assert error_code(env.c.get(f"/reports/{own}/audio", params={"token": access})) == "AUDIO_TOKEN_INVALID"
    assert env.c.get(f"/reports/{own}/audio").status_code == 422  # token 누락


def test_audio_token_expires(env, monkeypatch):
    monkeypatch.setattr(audio, "AUDIO_URL_TTL_SECONDS", -10)
    url = env.c.get(f"/reports/{env.ids.own}/audio-url", headers=env.headers).json()["audio_url"]
    response = env.c.get(url)
    assert response.status_code == 401 and error_code(response) == "AUDIO_TOKEN_INVALID"


def test_audio_missing_file_is_404(env):
    url = env.c.get(f"/reports/{env.ids.own}/audio-url", headers=env.headers).json()["audio_url"]
    env.audio_file.unlink()
    response = env.c.get(url)
    assert response.status_code == 404 and error_code(response) == "AUDIO_FILE_NOT_FOUND"
