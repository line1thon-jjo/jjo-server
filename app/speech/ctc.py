"""한국어 CTC 보조 모델(wav2vec2): 소리를 문맥 보정 없이 그대로 받아쓴 글자열에서 '반복(더듬기)'을 찾는다.

Whisper 는 같은 말을 두 번 하면 한 번으로 줄여 적어서(문맥 보정) 반복을 놓친다. CTC 모델은 들린 소리를 그대로 적으므로
같은 구절이 두 번 이어 나오는 것이 글자열에 남는다. 발음 점수로 흐림을 가르는 용도로는 쓸 수 없었다(구분력 낮음) — 반복 탐지에만 쓴다.

기본은 켜져 있고, 끄려면 환경변수 SPEECH_CTC_REPEAT=0 (또는 SpeechConfig.ctc_repeat=False). 환경변수가 있으면 그것이 우선한다.
torch / transformers 가 없거나 모델을 못 불러오면 경고만 남기고 건너뛰어 기존 분석 결과 그대로 낸다.
장치는 SPEECH_DEVICE (auto / cpu / cuda, 기본 auto), 모델은 SPEECH_CTC_MODEL 로 바꿀 수 있다.
"""
from __future__ import annotations

import logging
import os
import threading
from functools import lru_cache
from typing import Optional

import numpy as np

from app.contracts.analysis_result import Disfluency, DisfluencyType

from .config import SpeechConfig
from .phonetic import sound_distance
from .types import Syl

log = logging.getLogger(__name__)

_CHUNK_S = 20.0  # 긴 녹음은 이 길이로 잘라 모델에 넣는다
_CONTEXT_S = 2.0  # 잘라낸 경계 효과를 줄이려고 앞뒤로 더 넣었다가 버리는 길이
_FRAME = 320  # 16kHz 에서 20ms
_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")

Token = tuple[float, str]  # (시각(초), 음절)


def enabled(cfg: SpeechConfig) -> bool:
    v = os.environ.get("SPEECH_CTC_REPEAT", "").strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    return cfg.ctc_repeat


# ---- 모델 ------------------------------------------------------------------
class CtcTranscriber:
    def __init__(self, model_name: str, device: str = "auto"):
        import importlib
        import sys

        import torch  # 지연 불러오기: 이 기능을 켜지 않으면 torch 가 필요 없다

        try:
            importlib.import_module("torchaudio")  # 불러와지는지만 확인한다
        except Exception:
            # torchaudio 가 깨진 환경(예: 윈도우 보안 정책이 C++ 파일을 막음)에서는 transformers 가 그것을 건드리지 않게 숨긴다
            sys.modules["torchaudio"] = None
        from transformers import Wav2Vec2ForCTC, Wav2Vec2Processor

        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
        self._torch = torch
        self.device = device
        processor = Wav2Vec2Processor.from_pretrained(model_name)
        self._model = Wav2Vec2ForCTC.from_pretrained(model_name).eval().to(device)
        vocab = processor.tokenizer.get_vocab()
        self._id2tok = {i: t for t, i in vocab.items()}
        self._blank = processor.tokenizer.pad_token_id

    def tokens(self, y: np.ndarray, sample_rate: int) -> list[Token]:
        torch = self._torch
        n, step, ctx = len(y), int(_CHUNK_S * sample_rate), int(_CONTEXT_S * sample_rate)
        out: list[Token] = []
        for s in range(0, n, step):
            e = min(n, s + step)
            a, b = max(0, s - ctx), min(n, e + ctx)
            seg = y[a:b].astype(np.float32)
            seg = (seg - seg.mean()) / np.sqrt(seg.var() + 1e-7)
            with torch.inference_mode():
                logits = self._model(torch.from_numpy(seg).unsqueeze(0).to(self.device)).logits[0]
            ids = logits.argmax(dim=-1).cpu().numpy()
            left = (s - a) // _FRAME
            take = (e - s + _FRAME - 1) // _FRAME
            prev = -1
            for t, i in enumerate(ids[: left + take]):
                if i != prev and i != self._blank and t >= left:
                    c = self._id2tok.get(int(i), "")
                    if "가" <= c <= "힣":
                        out.append(((s / sample_rate) + (t - left) * _FRAME / sample_rate, c))
                prev = i
        return out

    def warm_up(self) -> None:
        self.tokens(np.zeros(int(1.0 * 16000), dtype=np.float32), 16000)


_lock = threading.Lock()
_transcriber: Optional[CtcTranscriber] = None
_failed = False


def get_transcriber(cfg: SpeechConfig) -> Optional[CtcTranscriber]:
    """모델은 한 번만 불러온다. 불러오지 못하면 None(이후 다시 시도하지 않음)."""
    global _transcriber, _failed
    with _lock:
        if _transcriber is None and not _failed:
            try:
                _transcriber = CtcTranscriber(
                    os.environ.get("SPEECH_CTC_MODEL", cfg.ctc_model),
                    os.environ.get("SPEECH_DEVICE", "auto").strip().lower() or "auto",
                )
            except Exception as e:  # torch/transformers 없음, 내려받기 실패 등
                _failed = True
                log.warning("CTC 보조 모델을 쓸 수 없어 반복 탐지를 건너뜁니다: %s: %s", type(e).__name__, e)
        return _transcriber


def warm_up(cfg: SpeechConfig) -> None:
    if enabled(cfg):
        t = get_transcriber(cfg)
        if t is not None:
            t.warm_up()


# ---- 반복 찾기 ---------------------------------------------------------------
@lru_cache(maxsize=32)
def _script_self_repeats(hangul: str, cfg: SpeechConfig) -> frozenset[str]:
    """대본 자체에 같은 구절이 이어 나오는 곳(예: '자꾸 자꾸')은 읽은 그대로가 반복이므로 후보에서 뺀다."""
    bad: set[str] = set()
    for k in range(cfg.ctc_repeat_min_syllables, cfg.ctc_repeat_max_syllables + 1):
        for gap in (0, 1):  # 반복 찾기와 같게, 두 구절 사이에 글자 하나까지 허용
            for p in range(len(hangul) - 2 * k - gap + 1):
                a = hangul[p : p + k]
                if sound_distance(a, hangul[p + k + gap : p + 2 * k + gap]) <= cfg.ctc_repeat_distance:
                    bad.add(a)
    return frozenset(bad)


def find_repeats(tokens: list[Token], script: str, cfg: SpeechConfig) -> list[tuple[float, float, str]]:
    """받아쓴 글자열에서 같은 구절이 이어 두 번 나오는 곳을 (두 번째 시작 초, 두 번째 끝 초, 두 번째 글자열)로 돌려준다."""
    hangul = "".join(c for c in script if "가" <= c <= "힣")
    bad = _script_self_repeats(hangul, cfg)
    n = len(tokens)
    found: list[tuple[int, int, int]] = []  # (구절 길이, 첫 구절 시작 위치, 두 번째 구절 시작 위치)
    for k in range(cfg.ctc_repeat_max_syllables, cfg.ctc_repeat_min_syllables - 1, -1):
        for i in range(n - 2 * k + 1):
            a = "".join(c for _, c in tokens[i : i + k])
            if len(set(a)) <= 1 or a in bad:
                continue
            for gap in (0, 1):  # 두 구절 사이에 군더더기 글자 하나까지 허용
                j = i + k + gap
                if j + k > n:
                    break
                if tokens[j][0] - tokens[i + k - 1][0] > cfg.ctc_repeat_max_gap_s:
                    continue
                if sound_distance(a, "".join(c for _, c in tokens[j : j + k])) <= cfg.ctc_repeat_distance:
                    found.append((k, i, j))
                    break
    # 겹치는 후보는 더 긴 것만 남긴다
    found.sort(key=lambda f: (-f[0], f[1]))
    kept: list[tuple[int, int, int]] = []
    for k, i, j in found:
        if all(j + k - 1 < ki or i > kj + kk - 1 for kk, ki, kj in kept):
            kept.append((k, i, j))
    out = [(tokens[j][0], tokens[j + k - 1][0], "".join(c for _, c in tokens[j : j + k])) for k, _, j in kept]
    return sorted(out)


def ctc_repeat_disfluencies(
    y: np.ndarray, script: str, sylls: list[Syl], existing: list[Disfluency], cfg: SpeechConfig
) -> list[Disfluency]:
    if not enabled(cfg):
        return []
    tr = get_transcriber(cfg)
    if tr is None:
        return []
    try:
        reps = find_repeats(tr.tokens(y, cfg.sample_rate), script, cfg)
    except Exception as e:  # 이 보조 기능 때문에 분석 전체가 실패하면 안 된다
        log.warning("CTC 반복 탐지 실패, 건너뜁니다: %s: %s", type(e).__name__, e)
        return []

    hang = [s for s in sylls if s.unit.is_hangul and s.aligned]
    out: list[Disfluency] = []
    for start_s, end_s, _ in reps:
        start_ms = max(0, int(round(start_s * 1000)) - cfg.ctc_repeat_pad_ms)
        end_ms = int(round(end_s * 1000)) + cfg.ctc_repeat_pad_ms
        # Whisper 쪽에서 이미 반복·군말 등으로 잡은 자리면 그대로 둔다
        if any(d.start_ms < end_ms and start_ms < d.end_ms for d in existing):
            continue
        nxt = next((s for s in hang if s.start_ms >= end_ms - cfg.ctc_repeat_pad_ms), None)
        if nxt is None:
            continue
        out.append(Disfluency(type=DisfluencyType.REPEAT, before_idx=nxt.unit.idx, start_ms=start_ms, end_ms=end_ms))
    return out
