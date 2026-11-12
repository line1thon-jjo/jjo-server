"""ffmpeg 가 없는 환경(CI 등)에서도 테스트가 돌도록, 테스트가 만드는 wav 파일만 파이썬으로 읽어 주는 대체 경로.

실제 서비스는 ffmpeg 가 필요하다. 여기서는 ffmpeg 가 없을 때에만 audio_io 가 부르는 subprocess.run 을 가짜로 바꾼다.
"""
from __future__ import annotations

import shutil
import subprocess
import wave

import numpy as np
import pytest


def _fake_ffmpeg(cmd, *args, **kwargs):
    if not cmd or cmd[0] != "ffmpeg":
        return subprocess.run(cmd, *args, **kwargs)
    path = cmd[cmd.index("-i") + 1]
    rate = int(cmd[cmd.index("-ar") + 1])
    try:
        with wave.open(path, "rb") as w:
            raw = w.readframes(w.getnframes())
            ch, width, src_rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
    except (wave.Error, EOFError):
        return subprocess.CompletedProcess(cmd, 1, b"", "wav 가 아니라 읽을 수 없습니다".encode())
    if width != 2:
        return subprocess.CompletedProcess(cmd, 1, b"", "16비트 wav 만 지원합니다".encode())
    y = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        y = y.reshape(-1, ch).mean(axis=1)
    if src_rate != rate and len(y):
        x = np.arange(int(len(y) * rate / src_rate)) * src_rate / rate
        y = np.interp(x, np.arange(len(y)), y).astype(np.float32)
    return subprocess.CompletedProcess(cmd, 0, y.astype("<f4").tobytes(), b"")


@pytest.fixture(autouse=True, scope="session")
def _wav_reader_without_ffmpeg():
    # 모듈 단위 fixture(demo 등)에서도 쓰이므로 세션 단위로 바꿔 둔다
    if shutil.which("ffmpeg") is not None:
        yield
        return
    from app.speech import audio_io

    mp = pytest.MonkeyPatch()
    mp.setattr(audio_io.subprocess, "run", _fake_ffmpeg)
    yield
    mp.undo()


@pytest.fixture(autouse=True)
def _no_ctc_model_in_tests(monkeypatch):
    # 보조 모델(torch/transformers)이 깔린 PC 에서도 테스트가 모델을 내려받거나 돌리지 않게 기본으로 끈다.
    # 보조 모델을 시험하는 test_ctc_repeat.py 는 스스로 켠다.
    monkeypatch.setenv("SPEECH_CTC_REPEAT", "0")
