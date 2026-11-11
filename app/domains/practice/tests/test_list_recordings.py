"""BE2용 연습별 녹음 목록 함수 (이슈 #12)."""
from sqlalchemy.orm import Session

from app.domains.auth.models import User
from app.domains.practice.models import Practice
from app.domains.practice.schemas import RecordingListItem
from app.domains.practice.service import list_recordings
from app.domains.practice.tests.test_api import api  # noqa: F401  (pytest fixture)
from app.domains.practice.tests.test_recordings import post, recording_api  # noqa: F401  (pytest fixture)
from tests.test_auth_api import client  # noqa: F401  (pytest fixture)


def test_lists_recordings_in_attempt_order(recording_api):  # noqa: F811
    _, engine, _, uid, pid, _ = recording_api
    first, second = post(recording_api).json(), post(recording_api).json()
    with Session(engine) as db:
        items = list_recordings(db, user_id=uid, practice_id=pid)
    assert [i.attempt_no for i in items] == [1, 2]
    assert [i.recording_id for i in items] == [first["recording_id"], second["recording_id"]]
    assert all(isinstance(i, RecordingListItem) and i.created_at for i in items)


def test_empty_practice_returns_empty_list(recording_api):  # noqa: F811
    _, engine, _, uid, pid, _ = recording_api
    with Session(engine) as db:
        assert list_recordings(db, user_id=uid, practice_id=pid) == []


def test_missing_or_foreign_practice_returns_none(recording_api):  # noqa: F811
    _, engine, _, uid, pid, _ = recording_api
    post(recording_api)
    with Session(engine) as db:
        other = User(google_sub="list-other", name="Other", email="list-other@test.invalid")
        db.add(other)
        db.flush()
        theirs = Practice(user_id=other.id, title="Other", script="private")
        db.add(theirs)
        db.commit()
        assert list_recordings(db, user_id=uid, practice_id=pid + 999) is None
        assert list_recordings(db, user_id=uid, practice_id=theirs.id) is None
        assert list_recordings(db, user_id=other.id, practice_id=pid) is None  # 남의 녹음은 안 보인다
