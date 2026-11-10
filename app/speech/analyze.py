"""BE3 진입점. analyze(녹음 경로, 대본) -> AnalysisResult 를 만들고, 서버 시작 시 모델을 미리 올리는 warm_up() 을 제공한다.
FastAPI·DB 를 모르는 순수 모듈이며, 실패는 AnalysisError 로 알린다.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from app.contracts.analysis_result import (
    AnalysisError,
    AnalysisResult,
    ErrorCode,
    IdxRange,
    Summary,
    Syllable,
)

from .align import Aligner, PrecomputedWordsAligner, WhisperAligner
from .audio_io import load_audio
from .clarity import detect_unclear_ranges
from .config import DEFAULT_CONFIG, SpeechConfig
from .disfluency import find_insertions, find_stray_sounds, merge_disfluencies, pauses_with_disfluency
from .features import (
    snap_to_silence,
    apply_pitch,
    apply_stretch,
    apply_volume,
    build_phrases,
    detect_pauses,
    energy_db,
    suppress_blips,
    pitch_track,
)
from .habits import detect_habits
from .numerals import normalize_numerals
from .spoken import build_spoken_text
from .text import parse_script

_default_aligner: Optional[WhisperAligner] = None
_lock = threading.Lock()


def get_default_aligner() -> WhisperAligner:
    global _default_aligner
    with _lock:
        if _default_aligner is None:
            _default_aligner = WhisperAligner()
        return _default_aligner


def warm_up() -> None:
    get_default_aligner().warm_up()


def _round_or_none(v: Optional[float], nd: int) -> Optional[float]:
    return None if v is None else round(v, nd)


def analyze(
    audio_path: str | Path,
    script: str,
    *,
    aligner: Optional[Aligner] = None,
    config: SpeechConfig = DEFAULT_CONFIG,
) -> AnalysisResult:
    cfg = config

    units = parse_script(script)
    n_hangul = sum(u.is_hangul for u in units)
    if n_hangul < 3:
        raise AnalysisError(ErrorCode.EMPTY_SCRIPT, "분석할 대본(한글)이 너무 짧거나 비어 있습니다.")

    y = load_audio(audio_path, cfg)
    duration_ms = int(round(len(y) * 1000 / cfg.sample_rate))

    al = aligner or get_default_aligner()
    words = al.get_words(y, units, cfg) if hasattr(al, "get_words") else None
    spoken_diffs: list = []
    match_ratio = None
    text = script
    if words is not None:
        words = normalize_numerals(words, script)
        text, spoken_diffs, match_ratio = build_spoken_text(script, units, words, cfg)
        if text != script:
            units = parse_script(text)
            spoken_hangul = sum(u.is_hangul for u in units)
            if match_ratio < cfg.min_align_coverage and spoken_hangul < cfg.min_align_coverage * n_hangul:
                raise AnalysisError(
                    ErrorCode.ALIGNMENT_LOW_COVERAGE,
                    f"녹음에서 인식된 내용이 너무 적어 분석할 수 없습니다 (대본 일치율 {match_ratio:.0%}).",
                )
            n_hangul = spoken_hangul
        sylls = PrecomputedWordsAligner(words).align(y, units, cfg)
    else:
        sylls = al.align(y, units, cfg)
        words = []

    n_aligned = sum(1 for s in sylls if s.unit.is_hangul and s.aligned)
    aligned_ratio = n_aligned / n_hangul
    if aligned_ratio < cfg.min_align_coverage:
        raise AnalysisError(
            ErrorCode.ALIGNMENT_LOW_COVERAGE,
            f"대본과 녹음이 많이 달라 분석할 수 없습니다 (일치율 {aligned_ratio:.0%}).",
        )

    db = suppress_blips(energy_db(y, cfg), cfg)
    snap_to_silence(sylls, db, cfg)
    f0 = pitch_track(y, cfg, len(db))
    median_hz = apply_pitch(sylls, f0, cfg)
    apply_volume(sylls, db, cfg)
    apply_stretch(sylls, cfg)
    disfluencies = merge_disfluencies(find_insertions(units, words, cfg), find_stray_sounds(sylls, db, f0, cfg))
    pauses, typical_pause_ms = detect_pauses(sylls, cfg, pauses_with_disfluency(sylls, disfluencies, cfg), db)
    phrases, articulation_rate = build_phrases(sylls, cfg)
    habits = detect_habits(sylls, pauses, cfg)
    unclear = detect_unclear_ranges(sylls, cfg)

    out_syllables = [
        Syllable(
            idx=s.unit.idx,
            char=s.unit.char,
            start_ms=int(round(s.start_ms)),
            end_ms=int(round(s.end_ms)),
            pitch_hz=_round_or_none(s.pitch_hz, 1),
            pitch_st=_round_or_none(s.pitch_st, 1),
            pitch_level=s.pitch_level,
            volume_db=_round_or_none(s.volume_db, 1),
            low_volume=s.low_volume,
            stretch=s.stretch,
            stretch_ratio=s.stretch_ratio,
            aligned=s.aligned,
            inferred=s.inferred,
            confidence=_round_or_none(s.confidence, 2),
            is_habit=bool(s.habit_types),
            habit_types=s.habit_types,
        )
        for s in sylls
    ]

    mismatched: list[IdxRange] = []
    run: list[int] = []
    for s in sylls:
        if s.unit.is_hangul and not s.aligned:
            run.append(s.unit.idx)
        elif run:
            mismatched.append(IdxRange(start_idx=run[0], end_idx=run[-1]))
            run = []
    if run:
        mismatched.append(IdxRange(start_idx=run[0], end_idx=run[-1]))

    warnings: list[str] = []
    if aligned_ratio < cfg.warn_align_coverage:
        warnings.append("PARTIAL_ALIGNMENT")
    if median_hz is None:
        warnings.append("PITCH_UNAVAILABLE")
    if match_ratio is not None and match_ratio < cfg.script_match_warn:
        warnings.append("SCRIPT_DEVIATION")

    ok = [s for s in sylls if s.unit.is_hangul and s.aligned]
    span_s = (ok[-1].end_ms - ok[0].start_ms) / 1000 if ok else 0.0
    avg_rate = len(ok) / span_s if span_s > 0 else 0.0

    return AnalysisResult(
        duration_ms=duration_ms,
        spoken_text=text,
        syllables=out_syllables,
        pauses=pauses,
        phrases=phrases,
        habits=habits,
        mismatched_ranges=mismatched,
        unclear_ranges=unclear,
        disfluencies=disfluencies,
        spoken_differences=spoken_diffs,
        warnings=warnings,
        summary=Summary(
            avg_syllables_per_sec=round(avg_rate, 2),
            articulation_syllables_per_sec=round(articulation_rate, 2),
            median_pitch_hz=_round_or_none(median_hz, 1),
            typical_pause_ms=int(round(typical_pause_ms)),
            aligned_ratio=round(aligned_ratio, 3),
            script_match_ratio=None if match_ratio is None else round(match_ratio, 3),
        ),
    )
