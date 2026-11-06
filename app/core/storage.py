"""로컬 녹음 저장소. 헤더 검사는 디코딩/재생 가능성 검증이 아니다."""

import logging
import os
import stat
from pathlib import Path
from typing import BinaryIO, Protocol
from uuid import uuid4

MAX_AUDIO_BYTES = 20 * 1024 * 1024
CHUNK_BYTES = 64 * 1024
logger = logging.getLogger(__name__)


class AudioValidationError(ValueError):
    def __init__(self, status_code: int, code: str):
        self.status_code = status_code
        self.code = code
        super().__init__(code)


class RecordingStorage(Protocol):
    def save(self, source: BinaryIO) -> str: ...
    def delete(self, audio_path: str) -> None: ...


def audio_extension(header: bytes) -> str:
    """컨테이너/ID3/MPEG 헤더만 검사한다. MIME과 파일명은 신뢰하지 않는다.

    WebM은 선두 EBML 헤더의 DocType, M4A는 선두 ftyp의 오디오/MP4
    호환 brand를 확인한다. MP4 brand만으로 오디오 트랙 존재를 보장하지 않는다.
    """
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WAVE":
        return ".wav"
    if header[:4] == b"\x1a\x45\xdf\xa3" and b"\x42\x82\x84webm" in header:
        return ".webm"
    if len(header) >= 16 and header[4:8] == b"ftyp":
        size = int.from_bytes(header[:4], "big")
        if 16 <= size <= len(header) and size % 4 == 0:
            brands = [header[8:12]] + [header[i:i + 4] for i in range(16, size, 4)]
            if any(brand in {b"M4A ", b"M4B ", b"isom", b"iso2", b"mp41", b"mp42"} for brand in brands):
                return ".m4a"
    if (len(header) >= 10 and header[:3] == b"ID3" and header[3] in (2, 3, 4)
            and header[4] != 255 and all(byte < 128 for byte in header[6:10])):
        return ".mp3"
    if len(header) >= 4:
        bits = int.from_bytes(header[:4], "big")
        if (bits >> 21 == 0x7FF and (bits >> 19) & 3 != 1
                and (bits >> 17) & 3 == 1 and (bits >> 12) & 15 not in (0, 15)
                and (bits >> 10) & 3 != 3):
            return ".mp3"
    raise AudioValidationError(415, "UNSUPPORTED_AUDIO_FORMAT")


class LocalRecordingStorage:
    """고유한 내부 절대 경로를 반환한다. 단일 서버의 영속 볼륨에 보관한다.

    기본 위치는 프로젝트의 uploads/recordings. 운영에서는 이 디렉터리를
    영속 볼륨에 연결해야 하며 HTTP 정적 파일 경로로 공개하지 않는다.
    """

    def __init__(self, root: Path | None = None):
        self.root = (root or Path(__file__).resolve().parents[2] / "uploads" / "recordings").resolve()

    def save(self, source: BinaryIO) -> str:
        header = source.read(CHUNK_BYTES)
        if not header:
            raise AudioValidationError(422, "EMPTY_AUDIO_FILE")
        extension = audio_extension(header)
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / (uuid4().hex + extension)
        # Open outside try: an extremely unlikely key collision must not delete
        # an existing file belonging to another recording.
        target = path.open("xb")
        try:
            with target:
                total = 0
                chunk = header
                while chunk:
                    total += len(chunk)
                    if total > MAX_AUDIO_BYTES:
                        raise AudioValidationError(413, "AUDIO_FILE_TOO_LARGE")
                    target.write(chunk)
                    chunk = source.read(min(CHUNK_BYTES, MAX_AUDIO_BYTES - total + 1))
            return str(path)
        except BaseException:
            try:
                path.unlink(missing_ok=True)
            except BaseException:
                logger.exception("Failed to remove a partial recording file")
            raise

    def delete(self, audio_path: str) -> None:
        path = Path(audio_path)
        if path.parent != self.root or str(path) != audio_path:
            raise ValueError("Recording path is outside storage")
        # Pin the directory and never follow its final component or the file's
        # symlink. unlinkat removes the entry itself, never a symlink target.
        try:
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return
        try:
            try:
                info = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                return
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Recording must be a regular, unlinked file")
            try:
                os.unlink(path.name, dir_fd=fd)
            except FileNotFoundError:
                pass
        finally:
            os.close(fd)


def get_recording_storage() -> RecordingStorage:
    return LocalRecordingStorage()
