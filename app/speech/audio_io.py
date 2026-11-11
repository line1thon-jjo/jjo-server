"""녹음 파일(m4a, webm, wav 등)을 ffmpeg 로 읽어 16kHz 모노 float32 배열로 통일한다. ffmpeg 가 PATH 에 있어야 한다."""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

from app.contracts.analysis_result import AnalysisError, ErrorCode

from .config import SpeechConfig


def load_audio(path: str | Path, cfg: SpeechConfig) -> np.ndarray:
    path = Path(path)
    if not path.is_file():
        raise AnalysisError(ErrorCode.AUDIO_NOT_FOUND, f"녹음 파일을 찾을 수 없습니다: {path}")

    cmd = [
        "ffmpeg", "-v", "error", "-nostdin",
        "-i", str(path),
        "-vn", "-ac", "1", "-ar", str(cfg.sample_rate),
        "-f", "f32le", "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=300)
    except FileNotFoundError as e:
        raise AnalysisError(
            ErrorCode.ENGINE_UNAVAILABLE, "ffmpeg 를 찾을 수 없습니다. ffmpeg 를 설치하고 PATH 에 추가하세요."
        ) from e
    except subprocess.TimeoutExpired as e:
        raise AnalysisError(ErrorCode.AUDIO_DECODE_FAILED, "오디오 디코딩 시간이 초과되었습니다.") from e

    if proc.returncode != 0 or not proc.stdout:
        err = proc.stderr.decode("utf-8", errors="ignore").strip()[:200]
        raise AnalysisError(ErrorCode.AUDIO_DECODE_FAILED, f"오디오를 읽을 수 없습니다: {err}")

    y = np.frombuffer(proc.stdout, dtype=np.float32).copy()
    if not np.isfinite(y).all():
        y = np.nan_to_num(y)

    duration_ms = len(y) * 1000 / cfg.sample_rate
    if duration_ms < cfg.min_audio_ms:
        raise AnalysisError(ErrorCode.AUDIO_TOO_SHORT, f"녹음이 너무 짧습니다 ({duration_ms / 1000:.1f}초).")
    if duration_ms > cfg.max_audio_s * 1000:
        raise AnalysisError(ErrorCode.AUDIO_TOO_LONG, f"녹음이 너무 깁니다 ({duration_ms / 1000:.0f}초).")
    return y
