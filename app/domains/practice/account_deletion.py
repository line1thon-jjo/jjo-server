"""Account deletion orchestration; auth never imports practice/report models.

The local durable journal is written BEFORE committing the cascade. Recovery
only removes files after a fresh database query confirms the user is absent.
"""

import json
import hashlib
import re
import stat
from contextlib import contextmanager
import logging
import os
from pathlib import Path
from uuid import uuid4

try:
    import fcntl
except ImportError:  # Keep unrelated APIs importable on Windows dev machines.
    fcntl = None

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.storage import LocalRecordingStorage
from app.domains.auth.models import User
from app.domains.practice.models import Practice, Recording

logger = logging.getLogger(__name__)


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class DeletionJournal:
    """Private metadata on the same persistent volume as local recordings."""

    def __init__(self, storage: LocalRecordingStorage):
        self.storage = storage
        self.root = storage.root / ".account-deletions"

    @contextmanager
    def locked(self, *, create=False):
        if fcntl is None:
            raise RuntimeError("Account deletion requires POSIX local filesystem locking")
        if create:
            self.storage.root.mkdir(parents=True, exist_ok=True)
            sync_directory(self.storage.root.parent)
        # O_NOFOLLOW rejects a substituted journal directory. The parent volume
        # and its ancestors must be writable only by trusted service operators.
        storage_fd = os.open(self.storage.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(storage_fd)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022:
                raise ValueError("Recording directory must be service-owned and not writable by others")
            if create:
                try:
                    os.mkdir(self.root.name, mode=0o700, dir_fd=storage_fd)
                except FileExistsError:
                    pass
                os.fsync(storage_fd)
            fd = os.open(self.root.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                         dir_fd=storage_fd)
            try:
                info = os.fstat(fd)
                if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                    raise ValueError("Journal directory must be service-owned and mode 0700")
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield fd
            finally:
                os.close(fd)
        finally:
            os.close(storage_fd)

    def validate_paths(self, paths):
        if not isinstance(paths, list) or any(not isinstance(p, str) for p in paths):
            raise ValueError("Invalid recording list")
        if len(paths) != len(set(paths)):
            raise ValueError("Duplicate recording path")
        for value in paths:
            path = Path(value)
            if (path.parent != self.storage.root or str(path) != value
                    or not re.fullmatch(r"[0-9a-f]{32}\.(wav|webm|m4a|mp3)", path.name)):
                raise ValueError("Invalid recording path")
            # Reject links and non-regular objects before deleting ANY file.
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Recording must be a regular, unlinked file")

    @staticmethod
    def checksum(data):
        return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"))
                              .encode("utf-8")).hexdigest()

    def prepare(self, user_id: int, paths: list[str]) -> Path:
        self.validate_paths(paths)
        with self.locked(create=True) as fd:
            job = self.root / (uuid4().hex + ".json")
            temporary = job.with_suffix(".tmp")
            data = {"version": 1, "job_id": job.stem, "user_id": user_id, "paths": paths}
            payload = dict(data, checksum=self.checksum(data))
            target_fd = os.open(temporary.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            with os.fdopen(target_fd, "w", encoding="utf-8") as target:
                json.dump(payload, target)
                target.flush()
                os.fsync(target.fileno())
            os.rename(temporary.name, job.name, src_dir_fd=fd, dst_dir_fd=fd)
            os.fsync(fd)
            return job

    def finish(self, db: Session, job: Path) -> bool:
        """Serialize API/recovery processes and validate the whole job first.

        Caller supplies a fresh READ COMMITTED transaction. A surviving user or
        ANY current recording reference prevents every unlink in this job.
        """
        if job.parent != self.root or not re.fullmatch(r"[0-9a-f]{32}\.json", job.name):
            raise ValueError("Invalid journal path")
        with self.locked() as fd:
            try:
                source_fd = os.open(job.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                    dir_fd=fd)
            except FileNotFoundError:
                # Another successful worker removed the job while we waited.
                return True
            with os.fdopen(source_fd, "r", encoding="utf-8") as source:
                info = os.fstat(source.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                        or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                    raise ValueError("Invalid journal file permissions or type")
                data = json.load(source)
            if not isinstance(data, dict) or set(data) != {
                    "version", "job_id", "user_id", "paths", "checksum"}:
                raise ValueError("Invalid journal schema")
            checksum = data.pop("checksum")
            if (type(data["version"]) is not int or data["version"] != 1
                    or data["job_id"] != job.stem or type(data["user_id"]) is not int
                    or data["user_id"] <= 0 or checksum != self.checksum(data)):
                raise ValueError("Invalid journal integrity")
            self.validate_paths(data["paths"])
            if db.scalar(select(User.id).where(User.id == data["user_id"])) is not None:
                return False
            # Also catches a damaged/misassigned job targeting another account.
            # Check the WHOLE list before any unlink, including missing files.
            if data["paths"] and db.scalar(select(Recording.id).where(
                    Recording.audio_path.in_(data["paths"])).limit(1)) is not None:
                raise ValueError("Journal contains a currently referenced recording")
            for path in data["paths"]:
                self.storage.delete(path)
            sync_directory(self.storage.root)
            os.unlink(job.name, dir_fd=fd)
            os.fsync(fd)
            return True


def get_deletion_journal() -> DeletionJournal:
    return DeletionJournal(LocalRecordingStorage())


class AccountNotFound(LookupError):
    pass


class DeletionUnconfirmed(RuntimeError):
    pass


class CleanupPending(RuntimeError):
    pass


def delete_account(db: Session, *, user_id: int, journal: DeletionJournal) -> None:
    try:
        # Parent first: block new practices via their FK key-share lock. Lock
        # practices in order to serialize with recording upload's FOR UPDATE.
        if db.scalar(select(User.id).where(User.id == user_id).with_for_update()) is None:
            raise AccountNotFound()
        list(db.scalars(select(Practice.id).where(Practice.user_id == user_id)
                        .order_by(Practice.id).with_for_update()))
        # Separate query under READ COMMITTED sees uploads committed while we
        # waited for practice locks. Cascade cannot hide an unjournaled file.
        paths = list(db.scalars(select(Recording.audio_path).join(Practice)
                               .where(Practice.user_id == user_id)))
        job = journal.prepare(user_id, paths)
        db.execute(delete(User).where(User.id == user_id))
        db.commit()
    except AccountNotFound:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        # Never unlink audio on commit failure: commit may actually have reached
        # PostgreSQL. Retain the journal for recovery against a fresh connection.
        logger.exception("Account deletion failed or commit outcome is unconfirmed")
        raise DeletionUnconfirmed() from exc
    try:
        if not journal.finish(db, job):
            raise CleanupPending()
    except Exception as exc:
        logger.exception("Account deleted; recording cleanup requires recovery")
        raise CleanupPending() from exc
    finally:
        db.rollback()  # close the post-commit verification read transaction


def recover_deletions(session_factory, journal: DeletionJournal) -> tuple[int, int]:
    """Retry every durable job. Failed/retained jobs yield a nonzero CLI exit."""
    completed = pending = 0
    if not journal.root.exists() and not journal.root.is_symlink():
        return completed, pending
    # Fail loudly for an inaccessible or unsafe queue, even if it has no jobs.
    try:
        with journal.locked():
            jobs = sorted(journal.root.glob("*.json"))
    except Exception:
        logger.exception("Cannot inspect account deletion journal")
        return 0, 1
    for job in jobs:
        try:
            with session_factory() as db:
                if journal.finish(db, job):
                    completed += 1
                else:
                    pending += 1
        except Exception:
            logger.exception("Account deletion recovery failed for job %s", job.name)
            pending += 1
    return completed, pending


if __name__ == "__main__":
    from app.core.database import SessionLocal

    completed, pending = recover_deletions(SessionLocal, get_deletion_journal())
    print(f"completed={completed} pending={pending}")
    raise SystemExit(1 if pending else 0)
