"""더듬음·끼어든 소리 탐지. ① Whisper 가 인식했지만 대본에 없는 말(어/음, 반복, 군말) ② 대본 글자 사이에 있는 사람 목소리 구간(Whisper 가 놓친 소리).
쉼 안에 이런 소리가 있으면 그 자리는 '조용한 쉼'이 아니므로 쉼 판정에서 뺀다.
"""
from __future__ import annotations

import numpy as np

from app.contracts.analysis_result import Disfluency, DisfluencyType

from .align import WordTiming, match_script
from .config import SpeechConfig
from .features import ms_to_frame
from .text import Unit
from .types import Syl

FILLERS = frozenset(
    {"어", "음", "으", "아", "엄", "흠", "에", "으음", "어어", "음음", "아아", "그", "저", "뭐", "이제", "막", "그니까"}
)


def find_insertions(units: list[Unit], words: list[WordTiming], cfg: SpeechConfig) -> list[Disfluency]:
    if not words:
        return []
    rec, hangul_pos, blocks = match_script(units, words, cfg)
    a_chars = [units[i].char for i in hangul_pos]

    out: list[Disfluency] = []
    for prev, nxt in zip(blocks, blocks[1:]):
        if nxt.a != prev.a + prev.size:
            continue
        extra = rec[prev.b + prev.size : nxt.b]
        if not extra:
            continue
        text = "".join(r[0] for r in extra)
        before = "".join(a_chars[max(0, nxt.a - len(text)) : nxt.a])
        after = "".join(a_chars[nxt.a : nxt.a + len(text)])
        if text in FILLERS:
            kind = DisfluencyType.FILLER
        elif before == text or after.startswith(text):
            kind = DisfluencyType.REPEAT
        else:
            kind = DisfluencyType.INSERTION
        out.append(
            Disfluency(
                type=kind,
                before_idx=units[hangul_pos[nxt.a]].idx,
                start_ms=int(round(extra[0][1])),
                end_ms=int(round(extra[-1][2])),
                text=text,
            )
        )
    return out


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    start = None
    for i, v in enumerate(mask):
        if v and start is None:
            start = i
        elif not v and start is not None:
            runs.append((start, i))
            start = None
    if start is not None:
        runs.append((start, len(mask)))
    return runs


def find_stray_sounds(
    sylls: list[Syl], db: np.ndarray, f0: np.ndarray, cfg: SpeechConfig
) -> list[Disfluency]:
    if len(db) == 0:
        return []
    peak = float(np.percentile(db, 95))
    thr = peak - cfg.stray_below_peak_db
    voiced = (db > thr) & ~np.isnan(f0[: len(db)])
    if len(voiced) < len(db):
        voiced = np.pad(voiced, (0, len(db) - len(voiced)))

    min_len = ms_to_frame(cfg.stray_min_ms, cfg)
    margin = ms_to_frame(cfg.stray_margin_ms, cfg)
    join = ms_to_frame(cfg.gap_merge_ms, cfg)

    hang = [s for s in sylls if s.unit.is_hangul]
    out: list[Disfluency] = []
    for a, b in zip(hang, hang[1:]):
        if not (a.aligned and b.aligned and not a.inferred and not b.inferred):
            continue
        if b.start_ms - a.end_ms < cfg.pause_min_ms:
            continue
        # 앞 어절의 인식 확신이 낮으면 그 어절의 끝 시각이 부정확해, 바로 뒤의 소리는 그 어절의 끝소리일 수 있다
        if a.confidence is not None and a.confidence < cfg.low_prob:
            continue
        # 같은 어절 안의 소리는 Whisper 가 그 어절의 일부로 들은 것이다
        if a.group is not None and a.group == b.group:
            continue
        lo = ms_to_frame(a.end_ms, cfg) + margin
        hi = ms_to_frame(b.start_ms, cfg) - margin
        if hi - lo < min_len:
            continue
        energetic = (db[lo:hi] > thr)
        merged: list[list[int]] = []
        for s, e in _runs(energetic):
            if merged and s - merged[-1][1] < join:
                merged[-1][1] = e
            else:
                merged.append([s, e])
        span = hi - lo
        for s, e in merged:
            if e - s < min_len:
                continue
            # 앞 음절 끝이나 뒤 음절 시작에 붙어 있으면(사이에 조용한 틈이 없으면) 그 음절 자체의 꼬리/첫소리다
            if s == 0 or e == span:
                continue
            loud = float(np.mean(db[lo + s : lo + e])) >= peak - cfg.stray_loud_below_peak_db
            if float(np.mean(voiced[lo + s : lo + e])) < cfg.stray_voiced_ratio and not loud:
                continue  # 목소리도 아니고 크지도 않으면(숨소리·잡음) 끼어든 소리로 세지 않는다. 기침처럼 큰 소리는 센다
            out.append(
                Disfluency(
                    type=DisfluencyType.STRAY_SOUND,
                    before_idx=b.unit.idx,
                    start_ms=int((lo + s) * cfg.hop_ms),
                    end_ms=int((lo + e) * cfg.hop_ms),
                )
            )
    return out


def merge_disfluencies(by_words: list[Disfluency], by_sound: list[Disfluency]) -> list[Disfluency]:
    def overlaps(x: Disfluency, y: Disfluency) -> bool:
        return x.start_ms < y.end_ms and y.start_ms < x.end_ms

    kept = [d for d in by_sound if not any(overlaps(d, w) for w in by_words)]
    return sorted(by_words + kept, key=lambda d: d.start_ms)


def pauses_with_disfluency(sylls: list[Syl], found: list[Disfluency], cfg: SpeechConfig) -> set[int]:
    skip: set[int] = set()
    hang = [s for s in sylls if s.unit.is_hangul]
    for a, b in zip(hang, hang[1:]):
        if not (a.aligned and b.aligned):
            continue
        if any(d.start_ms < b.start_ms and d.end_ms > a.end_ms for d in found):
            skip.add(a.unit.idx)
    return skip
