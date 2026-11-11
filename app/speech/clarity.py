"""불분명한 구간 탐지: 건너뜀 · 인식 실패 · 작은 소리를 확신도와 함께 연속 구간으로 묶는다."""
from __future__ import annotations

from typing import Optional

from app.contracts.analysis_result import UnclearRange, UnclearReason

from .config import SpeechConfig
from .types import Syl


def _classify(s: Syl, cfg: SpeechConfig) -> Optional[tuple[UnclearReason, float, int]]:
    if not s.aligned and not s.inferred:
        return UnclearReason.SKIPPED, 0.9, 1
    if s.inferred:
        if s.mismatch is not None:
            dist, prob = s.mismatch
            return UnclearReason.LOW_RECOGNITION, 0.3 + 0.7 * (0.5 * dist + 0.5 * (1.0 - prob)), 1
        return UnclearReason.LOW_RECOGNITION, 0.7, 1
    if s.confidence is not None and s.confidence < cfg.low_prob:
        # 모델이 대본과 같은 글자로 알아들었는데 확신만 낮은 경우: 소리가 알아들을 만했다는 뜻이라 근거를 절반만 인정한다
        return UnclearReason.LOW_RECOGNITION, cfg.matched_low_prob_weight * (1.0 - s.confidence), cfg.min_unclear_run
    if cfg.low_volume_is_unclear and s.low_volume and s.volume_db is not None:
        conf = min(1.0, 0.5 + (-s.volume_db - cfg.low_volume_db) / 16)
        return UnclearReason.LOW_VOLUME, conf, cfg.min_unclear_run
    return None


def detect_unclear_ranges(sylls: list[Syl], cfg: SpeechConfig) -> list[UnclearRange]:
    hang = [s for s in sylls if s.unit.is_hangul]
    labels = [_classify(s, cfg) for s in hang]
    out: list[UnclearRange] = []
    i = 0
    while i < len(hang):
        lab = labels[i]
        if lab is None:
            i += 1
            continue
        j = i
        while (
            j + 1 < len(hang)
            and labels[j + 1] is not None
            and labels[j + 1][0] == lab[0]
            and hang[j + 1].unit.sentence_id == hang[j].unit.sentence_id
        ):
            j += 1
        run = labels[i : j + 1]
        if len(run) >= max(r[2] for r in run):
            conf = sum(r[1] for r in run) / len(run)
            # 모델이 비슷한 소리의 다른 단어로 적은 것만으로는 흐림의 증거가 약하다. 근거가 충분할 때만 불분명으로 낸다.
            if lab[0] == UnclearReason.LOW_RECOGNITION and conf < cfg.low_recognition_min_conf:
                i = j + 1
                continue
            out.append(
                UnclearRange(
                    start_idx=hang[i].unit.idx,
                    end_idx=hang[j].unit.idx,
                    reason=lab[0],
                    confidence=round(max(0.0, min(1.0, conf)), 2),
                )
            )
        i = j + 1
    return out
