"""GPU·모델 환경변수 설정과 CPU 폴백 테스트. 실제 GPU·모델 없이 가짜 faster_whisper 로 확인한다."""
import sys
import types

import numpy as np
import pytest

from app.speech import align, analyze
from app.speech.align import WhisperAligner, whisper_settings


def test_no_environment_keeps_current_behavior(monkeypatch):
    monkeypatch.setattr(align, "cuda_available", lambda: False)
    assert whisper_settings({}) == ("medium", "auto", "int8")


def test_gpu_present_uses_float16_by_default(monkeypatch):
    monkeypatch.setattr(align, "cuda_available", lambda: True)
    assert whisper_settings({}) == ("medium", "auto", "float16")
    assert whisper_settings({"SPEECH_DEVICE": "cuda"}) == ("medium", "cuda", "float16")


def test_cpu_forced_even_if_gpu_present(monkeypatch):
    monkeypatch.setattr(align, "cuda_available", lambda: True)
    assert whisper_settings({"SPEECH_DEVICE": "cpu"}) == ("medium", "cpu", "int8")


def test_explicit_values_win(monkeypatch):
    monkeypatch.setattr(align, "cuda_available", lambda: True)
    env = {"SPEECH_MODEL": "large-v3", "SPEECH_DEVICE": "CUDA", "SPEECH_COMPUTE_TYPE": "int8_float16"}
    assert whisper_settings(env) == ("large-v3", "cuda", "int8_float16")


def test_blank_values_are_ignored(monkeypatch):
    monkeypatch.setattr(align, "cuda_available", lambda: False)
    assert whisper_settings({"SPEECH_MODEL": " ", "SPEECH_DEVICE": "", "SPEECH_COMPUTE_TYPE": " "}) == (
        "medium",
        "auto",
        "int8",
    )


def test_default_aligner_follows_environment(monkeypatch):
    monkeypatch.setattr(align, "cuda_available", lambda: False)
    monkeypatch.setattr(analyze, "_default_aligner", None)
    monkeypatch.setenv("SPEECH_MODEL", "small")
    monkeypatch.setenv("SPEECH_DEVICE", "cpu")
    a = analyze.get_default_aligner()
    assert (a.model_size, a.device, a.compute_type) == ("small", "cpu", "int8")
    monkeypatch.setattr(analyze, "_default_aligner", None)


def _fake_faster_whisper(monkeypatch, fail_on_load=False, fail_on_run=False):
    calls = []

    class Word:
        word, start, end, probability = " 안녕", 0.1, 0.5, 0.9

    class Seg:
        words = [Word()]

    class Model:
        def __init__(self, size, device, compute_type):
            calls.append((device, compute_type))
            if fail_on_load and device != "cpu":
                raise RuntimeError("CUDA 라이브러리를 찾을 수 없음")
            self.device = device

        def transcribe(self, *a, **k):
            if fail_on_run and self.device != "cpu":
                raise RuntimeError("Library libcudnn_ops.so.9 is not found")
            return iter([Seg()]), None

    mod = types.ModuleType("faster_whisper")
    mod.WhisperModel = Model
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)
    return calls


def test_gpu_load_failure_falls_back_to_cpu(monkeypatch):
    calls = _fake_faster_whisper(monkeypatch, fail_on_load=True)
    al = WhisperAligner(device="cuda", compute_type="float16")
    words = al.transcribe_words(np.zeros(16000, dtype=np.float32))
    assert [w.text for w in words] == ["안녕"]
    assert calls == [("cuda", "float16"), ("cpu", "int8")]
    assert (al.device, al.compute_type) == ("cpu", "int8")


def test_gpu_failure_during_recognition_falls_back_to_cpu_and_retries(monkeypatch):
    calls = _fake_faster_whisper(monkeypatch, fail_on_run=True)
    al = WhisperAligner(device="cuda", compute_type="float16")
    words = al.transcribe_words(np.zeros(16000, dtype=np.float32))
    assert [w.text for w in words] == ["안녕"]
    assert calls == [("cuda", "float16"), ("cpu", "int8")]


def test_cpu_failure_is_not_hidden(monkeypatch):
    _fake_faster_whisper(monkeypatch, fail_on_load=False)

    class Boom:
        def transcribe(self, *a, **k):
            raise RuntimeError("진짜 오류")

    al = WhisperAligner(device="cpu", compute_type="int8")
    al._model = Boom()
    with pytest.raises(RuntimeError):
        al.transcribe_words(np.zeros(16000, dtype=np.float32))
