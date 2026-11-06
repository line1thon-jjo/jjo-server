"""Actual migrated PostgreSQL, isolated schema and temporary recording volume."""
import importlib.util
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from threading import Event
from uuid import uuid4

import jwt
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient
from sqlalchemy import MetaData, Table, create_engine, select, text
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema, DropSchema

from app.core.config import settings
from app.core.database import get_db
from app.core.security import issue, key
from app.core.storage import LocalRecordingStorage, get_recording_storage
from app.domains.auth.models import User
from app.domains.practice.account_deletion import (
    DeletionJournal, DeletionUnconfirmed, delete_account,
    get_deletion_journal, recover_deletions,
)
from app.domains.practice.models import Practice, Recording
from app.domains.practice.service import RecordingNotFoundError, upload_recording
from app.main import app

AUDIO = b'RIFF0000WAVE' + b'abc'


@pytest.fixture
def env(tmp_path, monkeypatch):
    url = os.environ.get('AUTH_TEST_DATABASE_URL')
    if not url:
        pytest.skip('requires isolated PostgreSQL test DB')
    monkeypatch.setattr(settings, 'signup_token_secret', 's' * 40)
    monkeypatch.setattr(settings, 'access_token_secret', 'a' * 40)
    admin = create_engine(url)
    assert admin.dialect.name == 'postgresql'
    schema = 'settings_test_' + uuid4().hex
    with admin.begin() as conn:
        conn.execute(CreateSchema(schema))
    engine = create_engine(url, connect_args={'options': f'-csearch_path={schema}'})
    try:
        with engine.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                for name in ('0001_init_tables', '0002_report_status_check'):
                    spec = importlib.util.spec_from_file_location(name, Path('alembic/versions') / (name + '.py'))
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    module.upgrade()
        reports = Table('reports', MetaData(), autoload_with=engine)
        storage = LocalRecordingStorage(tmp_path / 'recordings')
        journal = DeletionJournal(storage)
        with Session(engine) as db:
            users = [User(google_sub=str(i), name='Owner', email=f'{i}@test.invalid',
                          terms_agreed_at=datetime.now(timezone.utc), onboarding_completed=True)
                     for i in range(2)]
            db.add_all(users)
            db.flush()
            ids = [u.id for u in users]
            practices = [Practice(user_id=u.id, title='test', script='script') for u in users]
            db.add_all(practices)
            db.flush()
            pids = [p.id for p in practices]
            paths = [storage.save(BytesIO(AUDIO)) for _ in users]
            recordings = [Recording(practice_id=p.id, attempt_no=1, script_snapshot='script',
                                    duration_ms=1, audio_path=path) for p, path in zip(practices, paths)]
            db.add_all(recordings)
            db.flush()
            for recording in recordings:
                db.execute(reports.insert().values(recording_id=recording.id, status='ANALYZING',
                           analysis_result={'test': True}, feedback={'text': 'feedback'}))
            db.commit()
        def session():
            with Session(engine, expire_on_commit=False) as db:
                yield db
        app.dependency_overrides[get_db] = session
        app.dependency_overrides[get_deletion_journal] = lambda: journal
        app.dependency_overrides[get_recording_storage] = lambda: storage
        with TestClient(app) as client:
            yield client, engine, ids, pids, paths, journal, reports
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(DropSchema(schema, cascade=True))
        admin.dispose()


def headers(uid):
    return {'Authorization': 'Bearer ' + issue('access', uid)}


def test_delete_cascade_isolation_and_revocation(env):
    c, engine, ids, pids, paths, journal, reports = env
    response = c.delete('/auth/me', headers=headers(ids[0]))
    assert response.status_code == 204, response.text
    assert not response.content
    assert not Path(paths[0]).exists() and Path(paths[1]).exists()
    assert not list(journal.root.glob('*.json'))
    with Session(engine) as db:
        assert list(db.scalars(select(User.id))) == [ids[1]]
        assert list(db.scalars(select(Practice.id))) == [pids[1]]
        remaining = db.scalar(select(Recording))
        assert remaining.practice_id == pids[1]
        assert list(db.scalars(select(reports.c.recording_id))) == [remaining.id]
        assert db.scalar(select(reports.c.feedback)) == {'text': 'feedback'}
    for path in ('/auth/me', '/practices', f'/practices/{pids[0]}'):
        assert c.get(path, headers=headers(ids[0])).status_code == 401
    assert c.post(f'/practices/{pids[0]}/recordings', headers=headers(ids[0]),
                  files={'file': ('a.wav', AUDIO)}, data={'duration_ms': '1'}).status_code == 401
    assert c.delete('/auth/me', headers=headers(ids[0])).status_code == 401
    assert c.get('/auth/me', headers=headers(ids[1])).status_code == 200


@pytest.mark.parametrize('kind', ['missing', 'invalid', 'expired', 'signup'])
def test_invalid_credentials_no_changes(env, kind):
    c, engine, ids, _, paths, _, _ = env
    if kind == 'missing':
        auth = {}
    elif kind == 'invalid':
        auth = {'Authorization': 'Bearer invalid'}
    elif kind == 'signup':
        auth = {'Authorization': 'Bearer ' + issue('signup', 'sub', email='a@test', name='a')}
    else:
        claims = jwt.decode(issue('access', ids[0]), options={'verify_signature': False})
        claims['exp'] = 1
        auth = {'Authorization': 'Bearer ' + jwt.encode(claims, key('access'), algorithm='HS256')}
    assert c.delete('/auth/me', headers=auth).status_code == 401
    assert all(Path(p).exists() for p in paths)
    with Session(engine) as db:
        assert len(list(db.scalars(select(User)))) == 2


def test_file_failure_is_not_success_and_recovery_survives_restart(env, monkeypatch):
    c, engine, ids, _, paths, journal, _ = env
    original = journal.storage.delete
    monkeypatch.setattr(journal.storage, 'delete', lambda _: (_ for _ in ()).throw(OSError('disk failure')))
    result = c.delete('/auth/me', headers=headers(ids[0]))
    assert result.status_code == 503
    assert result.json()['error']['code'] == 'ACCOUNT_DELETED_CLEANUP_PENDING'
    assert Path(paths[0]).exists()
    assert c.get('/auth/me', headers=headers(ids[0])).status_code == 401
    assert recover_deletions(lambda: Session(engine), journal) == (0, 1)
    monkeypatch.setattr(journal.storage, 'delete', original)
    restarted = DeletionJournal(LocalRecordingStorage(journal.storage.root))
    assert recover_deletions(lambda: Session(engine), restarted) == (1, 0)
    assert recover_deletions(lambda: Session(engine), restarted) == (0, 0)
    assert not Path(paths[0]).exists() and Path(paths[1]).exists()


@pytest.mark.parametrize('failure', ['journal', 'delete', 'commit', 'commit_ack'])
def test_db_failures_preserve_files_until_committed_absence(env, monkeypatch, failure):
    _, engine, ids, _, paths, journal, _ = env
    with Session(engine) as db:
        def fail(*args, **kwargs):
            raise OSError('injected failure')
        if failure == 'journal':
            monkeypatch.setattr(journal, 'prepare', fail)
        elif failure == 'delete':
            original = db.execute
            def execute(statement, *args, **kwargs):
                if statement.is_delete:
                    fail()
                return original(statement, *args, **kwargs)
            monkeypatch.setattr(db, 'execute', execute)
        else:
            original = db.commit
            def commit():
                if failure == 'commit_ack':
                    original()  # server committed, acknowledgement was lost
                fail()
            monkeypatch.setattr(db, 'commit', commit)
        with pytest.raises(DeletionUnconfirmed):
            delete_account(db, user_id=ids[0], journal=journal)
    assert all(Path(p).exists() for p in paths)
    completed, pending = recover_deletions(lambda: Session(engine), journal)
    assert completed == (1 if failure == 'commit_ack' else 0)
    if failure in ('delete', 'commit'):
        assert pending == 1
    with Session(engine) as db:
        assert (db.get(User, ids[0]) is None) == (failure == 'commit_ack')
    if failure != 'commit_ack':
        # Same authenticated account can retry. Old jobs are also idempotent.
        with Session(engine) as db:
            # undo injected journal fault before retry
            if failure == 'journal':
                monkeypatch.undo()
            delete_account(db, user_id=ids[0], journal=journal)
        recover_deletions(lambda: Session(engine), journal)
        assert not Path(paths[0]).exists()
    assert Path(paths[1]).exists()


def test_upload_copy_overlaps_deletion(env):
    _, engine, ids, pids, paths, journal, _ = env
    saved, release = Event(), Event()
    class SlowStorage(LocalRecordingStorage):
        def save(self, source):
            result = super().save(source)
            saved.set()
            assert release.wait(10)
            return result
    storage = SlowStorage(journal.storage.root)
    def upload():
        with Session(engine) as db:
            with pytest.raises(RecordingNotFoundError):
                upload_recording(db, practice_id=pids[0], user_id=ids[0],
                                 source=BytesIO(AUDIO), duration_ms=1, storage=storage)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(upload)
        try:
            assert saved.wait(10)
            with Session(engine) as db:
                delete_account(db, user_id=ids[0], journal=journal)
        finally:
            release.set()
        future.result(timeout=10)
    assert list(storage.root.glob('*.wav')) == [Path(paths[1])]


def test_deletion_waits_for_locked_upload_and_collects_its_file(env):
    _, engine, ids, pids, paths, journal, _ = env
    started = Event()
    def withdraw():
        with Session(engine) as db:
            started.set()
            delete_account(db, user_id=ids[0], journal=journal)
    with Session(engine) as upload_db:
        upload_db.execute(select(Practice).where(Practice.id == pids[0]).with_for_update())
        new_path = journal.storage.save(BytesIO(AUDIO))
        upload_db.add(Recording(practice_id=pids[0], attempt_no=2, script_snapshot='new',
                                duration_ms=1, audio_path=new_path))
        upload_db.flush()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(withdraw)
            try:
                assert started.wait(10)
                # Observe the real lock wait before releasing the upload.
                from time import monotonic, sleep
                deadline = monotonic() + 5
                while True:
                    with engine.connect() as conn:
                        blocked = conn.scalar(text('SELECT count(*) FROM pg_stat_activity WHERE '
                            "datname=current_database() AND cardinality(pg_blocking_pids(pid)) > 0"))
                    if blocked:
                        break
                    assert monotonic() < deadline
                    sleep(0.02)
            finally:
                upload_db.commit()
            future.result(timeout=10)
    assert not Path(new_path).exists() and not Path(paths[0]).exists()
    assert Path(paths[1]).exists()


def test_partial_cleanup_missing_file_and_database_unavailability(env, monkeypatch):
    c, engine, ids, pids, paths, journal, _ = env
    extra = journal.storage.save(BytesIO(AUDIO))
    with Session(engine) as db:
        db.add(Recording(practice_id=pids[0], attempt_no=2, script_snapshot='test',
                         duration_ms=1, audio_path=extra))
        db.commit()
    original = journal.storage.delete
    calls = []
    def partial(path):
        calls.append(path)
        if len(calls) == 2:
            raise OSError('second unlink failed')
        original(path)
    monkeypatch.setattr(journal.storage, 'delete', partial)
    assert c.delete('/auth/me', headers=headers(ids[0])).status_code == 503
    assert len(calls) == 2 and not Path(calls[0]).exists() and Path(calls[1]).exists()
    def unavailable():
        raise OSError('database is unavailable')
    assert recover_deletions(unavailable, journal) == (0, 1)
    assert Path(calls[1]).exists()
    monkeypatch.setattr(journal.storage, 'delete', original)
    assert recover_deletions(lambda: Session(engine), journal) == (1, 0)
    assert not Path(extra).exists() and not Path(paths[0]).exists()
    assert Path(paths[1]).exists()


def test_late_analysis_cannot_recreate_deleted_report(env):
    from sqlalchemy.exc import IntegrityError
    c, engine, ids, pids, paths, journal, reports = env
    with Session(engine) as db:
        rid = db.scalar(select(Recording.id).where(Recording.practice_id == pids[0]))
    assert c.delete('/auth/me', headers=headers(ids[0])).status_code == 204
    with Session(engine) as db:
        # A worker that loaded an old DTO cannot resurrect results/feedback.
        assert db.execute(reports.update().where(reports.c.recording_id == rid)
                          .values(status='COMPLETED')).rowcount == 0
        db.rollback()
        with pytest.raises(IntegrityError):
            db.execute(reports.insert().values(recording_id=rid, status='PENDING'))
            db.commit()
        db.rollback()
    assert Path(paths[1]).exists()


def test_repeated_concurrent_deletion_is_safe(env):
    from app.domains.practice.account_deletion import AccountNotFound
    _, engine, ids, _, paths, journal, _ = env
    def withdraw():
        with Session(engine) as db:
            try:
                delete_account(db, user_id=ids[0], journal=journal)
                return 'deleted'
            except AccountNotFound:
                return 'absent'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: withdraw(), range(2)))
    assert sorted(results) == ['absent', 'deleted']
    assert not Path(paths[0]).exists() and Path(paths[1]).exists()


def test_postgresql_cascade_failure_rolls_back_everything_and_http_retry(env):
    c, engine, ids, _, paths, journal, reports = env
    with engine.begin() as conn:
        conn.execute(text("CREATE FUNCTION refuse_report_delete() RETURNS trigger LANGUAGE plpgsql AS $$ "
                          "BEGIN RAISE EXCEPTION 'injected cascade failure'; END; $$"))
        conn.execute(text('CREATE TRIGGER refuse_report_delete BEFORE DELETE ON reports '
                          'FOR EACH ROW EXECUTE FUNCTION refuse_report_delete()'))
    result = c.delete('/auth/me', headers=headers(ids[0]))
    assert result.status_code == 503
    assert result.json()['error']['code'] == 'ACCOUNT_DELETION_UNCONFIRMED'
    assert 'injected' not in result.text
    assert all(Path(path).exists() for path in paths)
    with Session(engine) as db:
        assert len(list(db.scalars(select(User.id)))) == 2
        assert len(list(db.scalars(select(Practice.id)))) == 2
        assert len(list(db.scalars(select(Recording.id)))) == 2
        assert len(list(db.scalars(select(reports.c.id)))) == 2
    assert recover_deletions(lambda: Session(engine), journal) == (0, 1)
    with engine.begin() as conn:
        conn.execute(text('DROP TRIGGER refuse_report_delete ON reports'))
    assert c.delete('/auth/me', headers=headers(ids[0])).status_code == 204
    assert recover_deletions(lambda: Session(engine), journal) == (1, 0)
    assert not Path(paths[0]).exists() and Path(paths[1]).exists()


@pytest.mark.parametrize('fault', ['parent_sync', 'storage_sync', 'file_sync', 'journal_sync', 'rename'])
def test_real_journal_io_failure_never_deletes_database(env, monkeypatch, fault):
    from app.domains.practice import account_deletion as module
    c, engine, ids, _, paths, journal, _ = env
    original = module.os.fsync
    calls = 0
    def fsync(fd):
        nonlocal calls
        calls += 1
        if calls == {'parent_sync': 1, 'storage_sync': 2, 'file_sync': 3,
                     'journal_sync': 4}.get(fault):
            raise OSError('injected sync failure')
        original(fd)
    def rename(*args, **kwargs):
        raise OSError('injected rename failure')
    with monkeypatch.context() as patch:
        if fault == 'rename':
            patch.setattr(module.os, 'rename', rename)
        else:
            patch.setattr(module.os, 'fsync', fsync)
        result = c.delete('/auth/me', headers=headers(ids[0]))
    assert result.status_code == 503
    assert result.json()['error']['code'] == 'ACCOUNT_DELETION_UNCONFIRMED'
    with Session(engine) as db:
        assert db.get(User, ids[0]) is not None
        assert len(list(db.scalars(select(Recording.id)))) == 2
    assert all(Path(p).exists() for p in paths)


def rewrite_job(journal, job, transform, *, recompute=True):
    import json
    data = json.loads(job.read_text())
    transform(data)
    if recompute:
        data['checksum'] = journal.checksum({k: v for k, v in data.items() if k != 'checksum'})
    job.write_text(json.dumps(data))


@pytest.mark.parametrize('damage', ['outside', 'traversal', 'other_owner', 'checksum',
                                    'bad_type', 'symlink', 'hardlink', 'job_symlink', 'bad_json'])
def test_damaged_job_never_deletes_any_file(env, damage, tmp_path):
    _, engine, ids, _, paths, journal, _ = env
    # Keep files but commit the cascade, as if the process stopped after commit.
    job = journal.prepare(ids[0], [paths[0]])
    with Session(engine) as db:
        db.execute(text('DELETE FROM users WHERE id=:id'), {'id': ids[0]})
        db.commit()
    outside = tmp_path / 'outside.wav'
    outside.write_bytes(AUDIO)
    if damage == 'outside':
        rewrite_job(journal, job, lambda d: d['paths'].append(str(outside)))
    elif damage == 'traversal':
        rewrite_job(journal, job, lambda d: d['paths'].append(str(journal.storage.root) + '/../outside.wav'))
    elif damage == 'other_owner':
        rewrite_job(journal, job, lambda d: d['paths'].append(paths[1]))
    elif damage == 'checksum':
        rewrite_job(journal, job, lambda d: d.update(user_id=ids[0] + 999), recompute=False)
    elif damage == 'bad_type':
        rewrite_job(journal, job, lambda d: d.update(user_id=True))
    elif damage in ('symlink', 'hardlink'):
        linked = journal.storage.root / ('f' * 32 + '.wav')
        if damage == 'symlink':
            linked.symlink_to(outside)
        else:
            os.link(outside, linked)
        rewrite_job(journal, job, lambda d: d['paths'].append(str(linked)))
    elif damage == 'job_symlink':
        moved = tmp_path / 'job.json'
        job.rename(moved)
        job.symlink_to(moved)
    else:
        job.write_text('{broken')
    assert recover_deletions(lambda: Session(engine), journal) == (0, 1)
    assert all(Path(p).exists() for p in paths)
    assert outside.read_bytes() == AUDIO
    assert job.exists()


def test_directory_permissions_and_symlinks_fail_closed(tmp_path):
    storage = LocalRecordingStorage(tmp_path / 'audio')
    journal = DeletionJournal(storage)
    job = journal.prepare(1, [])
    assert journal.root.stat().st_mode & 0o777 == 0o700
    assert job.stat().st_mode & 0o777 == 0o600
    import json
    assert set(json.loads(job.read_text())) == {'version', 'job_id', 'user_id', 'paths', 'checksum'}
    journal.root.chmod(0o755)
    with pytest.raises(ValueError):
        journal.prepare(1, [])
    journal.root.chmod(0o700)
    moved = tmp_path / 'moved'
    journal.root.rename(moved)
    journal.root.symlink_to(moved, target_is_directory=True)
    with pytest.raises(OSError):
        journal.prepare(1, [])
    assert recover_deletions(lambda: None, journal) == (0, 1)


def test_storage_delete_rejects_symlinks_and_parent_replacement(tmp_path):
    storage = LocalRecordingStorage(tmp_path / 'audio')
    path = Path(storage.save(BytesIO(AUDIO)))
    outside = tmp_path / 'outside.wav'
    outside.write_bytes(AUDIO)
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(ValueError):
        storage.delete(str(path))
    assert outside.exists()
    path.unlink()
    storage.delete(str(path))  # missing file is safe
    moved = tmp_path / 'moved'
    storage.root.rename(moved)
    storage.root.symlink_to(moved, target_is_directory=True)
    with pytest.raises(OSError):
        storage.delete(str(path))


def recovery_process(url, schema, root, barrier):
    engine = create_engine(url, connect_args={'options': f'-csearch_path={schema}'})
    try:
        journal = DeletionJournal(LocalRecordingStorage(Path(root)))
        original = journal.finish
        def finish(db, job):
            # Both processes have enumerated the same job before either unlinks.
            barrier.wait(timeout=10)
            return original(db, job)
        journal.finish = finish
        return recover_deletions(lambda: Session(engine), journal)
    finally:
        engine.dispose()


def test_two_recovery_processes(env):
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor
    _, engine, ids, _, paths, journal, _ = env
    journal.prepare(ids[0], [paths[0]])
    with engine.begin() as conn:
        schema = conn.scalar(text('SELECT current_schema()'))
        conn.execute(text('DELETE FROM users WHERE id=:id'), {'id': ids[0]})
    context = multiprocessing.get_context('spawn')
    with context.Manager() as manager:
        barrier = manager.Barrier(2)
        with ProcessPoolExecutor(max_workers=2, mp_context=context) as pool:
            futures = [pool.submit(recovery_process, engine.url.render_as_string(hide_password=False),
                                   schema, str(journal.storage.root), barrier) for _ in range(2)]
            results = [future.result(timeout=20) for future in futures]
    assert results == [(1, 0), (1, 0)]
    assert not Path(paths[0]).exists() and Path(paths[1]).exists()
    assert not list(journal.root.glob('*.json'))


def test_api_and_recovery_overlap_returns_204_after_cleanup(env, monkeypatch):
    c, engine, ids, _, paths, journal, _ = env
    committed, recovered = Event(), Event()
    original = journal.finish
    def finish(db, job):
        committed.set()
        assert recovered.wait(10)
        return original(db, job)
    monkeypatch.setattr(journal, 'finish', finish)
    with ThreadPoolExecutor(max_workers=1) as pool:
        response = pool.submit(c.delete, '/auth/me', headers=headers(ids[0]))
        try:
            assert committed.wait(10)
            other = DeletionJournal(LocalRecordingStorage(journal.storage.root))
            assert recover_deletions(lambda: Session(engine), other) == (1, 0)
            assert not Path(paths[0]).exists()
        finally:
            recovered.set()
        assert response.result(timeout=10).status_code == 204
    assert Path(paths[1]).exists()


def test_openapi_and_route_registration_preserves_existing_routes():
    schema = app.openapi()
    assert {'get', 'delete'} <= schema['paths']['/auth/me'].keys()
    for path in ['/practices', '/practices/script-upload', '/practices/{practice_id}/recordings']:
        assert 'post' in schema['paths'][path]
    operations = [operation['operationId'] for path in schema['paths'].values()
                  for operation in path.values() if isinstance(operation, dict) and 'operationId' in operation]
    assert len(operations) == len(set(operations))
    assert '204' in schema['paths']['/auth/me']['delete']['responses']
