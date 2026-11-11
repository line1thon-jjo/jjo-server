"""신호에서 값을 뽑는다: 에너지, 음높이(연속 반음 + 7단계), 음량, 늘임, 쉼(기대 여부·상대 길이), 구절별 빠르기.
모든 기준은 이 녹음 한 건 안의 본인 평균(중앙값)이다.
"""
from __future__ import annotations

from bisect import bisect_right

import numpy as np

from app.contracts.analysis_result import (
    AnalysisError,
    ErrorCode,
    Pause,
    PauseBoundary,
    Phrase,
    SpeedLabel,
)

from .config import SpeechConfig
from .text import B_COMMA, B_LINE, B_NONE, B_SENTENCE, B_WORD
from .types import Syl


def energy_db(y: np.ndarray, cfg: SpeechConfig) -> np.ndarray:
    sr = cfg.sample_rate
    hop = int(sr * cfg.hop_ms / 1000)
    win = int(sr * 0.025)
    yp = np.pad(y.astype(np.float64), (win // 2, win // 2))
    frames = np.lib.stride_tricks.sliding_window_view(yp, win)[::hop]
    rms = np.sqrt((frames**2).mean(axis=1)) + 1e-8
    db = 20 * np.log10(rms)
    if len(db) >= 3:
        db = np.convolve(np.pad(db, 1, mode="edge"), np.ones(3) / 3, mode="valid")
    return db


def suppress_blips(db: np.ndarray, cfg: SpeechConfig) -> np.ndarray:
    """소리 사이에 낀 아주 짧은 잡음(입 소리, 클릭 등)을 무음으로 취급한다.
    말소리 판정 문턱 위로 올라온 구간이 blip_ms 보다 짧고, 앞뒤가 모두 blip_gap_ms 이상 무음이면 잡음으로 본다."""
    if len(db) == 0:
        return db
    thr = speech_threshold(db)
    voiced = db > thr
    n = len(db)
    out = db.copy()
    max_len = ms_to_frame(cfg.blip_ms, cfg)
    min_gap = ms_to_frame(cfg.blip_gap_ms, cfg)
    i = 0
    while i < n:
        if not voiced[i]:
            i += 1
            continue
        j = i
        while j < n and voiced[j]:
            j += 1
        if j - i <= max_len:
            l = i
            while l > 0 and not voiced[l - 1]:
                l -= 1
            r = j
            while r < n and not voiced[r]:
                r += 1
            left_gap = i - l if l > 0 else min_gap  # 녹음 맨 앞/뒤의 무음도 무음으로 본다
            right_gap = r - j if r < n else min_gap
            if left_gap >= min_gap and right_gap >= min_gap:
                out[i:j] = thr - 10.0
        i = j
    return out


def speech_threshold(db: np.ndarray) -> float:
    floor = float(np.percentile(db, 5))
    peak = float(np.percentile(db, 95))
    return max(floor + 0.3 * (peak - floor), peak - 40.0)


def snap_to_silence(sylls: list[Syl], db: np.ndarray, cfg: SpeechConfig) -> int:
    """Whisper 어절 시각은 앞 어절 끝=뒤 어절 시작으로 붙어 있어, 쉼이 어절 안으로 번지거나 문장 끝소리가 잘린다.
    소리 에너지에서 실제 무음 구간을 찾아 그 앞 음절의 끝과 뒤 음절의 시작을 무음 경계에 맞춘다. 맞춘 쉼의 수를 돌려준다."""
    if len(db) == 0:
        return 0
    thr = speech_threshold(db)
    min_frames = ms_to_frame(cfg.pause_min_ms, cfg)
    quiet = db < thr
    runs: list[tuple[float, float]] = []
    start = None
    for i, q in enumerate(quiet):
        if q and start is None:
            start = i
        elif not q and start is not None:
            if i - start >= min_frames:
                runs.append((start * cfg.hop_ms, i * cfg.hop_ms))
            start = None

    timed = [s for s in sylls if s.unit.is_hangul and s.timed]
    snapped = 0
    reach = 300.0
    for r_start, r_end in runs:
        before = [s for s in timed if (s.start_ms + s.end_ms) / 2 <= r_start]
        if not before:
            continue
        a = before[-1]
        after = [s for s in timed if s.start_ms >= a.start_ms and s is not a and (s.start_ms + s.end_ms) / 2 > r_start]
        if not after:
            continue
        b = after[0]
        if not (-reach <= r_start - a.end_ms <= reach):
            continue
        if b.start_ms < r_end and not _fits(sylls, b, cfg, lo=r_end):
            continue
        if a.end_ms > r_start and not _fits(sylls, a, cfg, hi=r_start):
            continue
        if r_start - a.start_ms < cfg.min_syllable_ms / 2 and a.end_ms <= r_start:
            continue
        if a.end_ms > r_start and a.group is not None:
            _squeeze(sylls, a.group, hi=r_start)
        if a.end_ms > r_start:
            a.end_ms = r_start
        else:
            # 음절 길이(늘임 판단)는 그대로 두고, 쉼이 시작되는 시각만 실제 무음 시작으로 기록한다
            a.sil_start = r_start
        a.snap_end = True
        if b.start_ms < r_end:
            if b.group is not None:
                _squeeze(sylls, b.group, lo=r_end)
            b.start_ms = max(b.start_ms, r_end)
            b.snap_start = True
        elif abs(b.start_ms - r_end) <= reach:
            b.start_ms = r_end
            b.snap_start = True
        snapped += 1
    return snapped


def _fits(sylls: list[Syl], s: Syl, cfg: SpeechConfig, lo: float | None = None, hi: float | None = None) -> bool:
    """어절(또는 음절)을 [lo, 끝] / [시작, hi] 안으로 줄여도 음절마다 최소 길이가 남는지."""
    g = [x for x in sylls if s.group is not None and x.group == s.group and x.aligned] or [s]
    gs, ge = g[0].start_ms, g[-1].end_ms
    new_s, new_e = (lo if lo is not None else gs), (hi if hi is not None else ge)
    return new_e - new_s >= len(g) * cfg.min_syllable_ms * 2 / 3


def _squeeze(sylls: list[Syl], group: int, lo: float | None = None, hi: float | None = None) -> None:
    """같은 어절의 음절 시각을 비율은 유지한 채 [lo, 끝] 또는 [시작, hi] 안으로 줄인다."""
    g = [s for s in sylls if s.group == group and s.aligned]
    if len(g) < 1:
        return
    gs, ge = g[0].start_ms, g[-1].end_ms
    new_s, new_e = (lo if lo is not None else gs), (hi if hi is not None else ge)
    if ge <= gs or new_e <= new_s:
        return
    k = (new_e - new_s) / (ge - gs)
    for s in g:
        s.start_ms = new_s + (s.start_ms - gs) * k
        s.end_ms = new_s + (s.end_ms - gs) * k


def ms_to_frame(ms: float, cfg: SpeechConfig) -> int:
    return int(round(ms / cfg.hop_ms))


def pitch_track(y: np.ndarray, cfg: SpeechConfig, n_frames: int) -> np.ndarray:
    try:
        import parselmouth
    except ImportError as e:
        raise AnalysisError(
            ErrorCode.ENGINE_UNAVAILABLE, "praat-parselmouth 가 필요합니다: pip install praat-parselmouth"
        ) from e

    snd = parselmouth.Sound(y.astype(np.float64), sampling_frequency=float(cfg.sample_rate))

    def track(floor: float, ceiling: float):
        p = snd.to_pitch(time_step=cfg.hop_ms / 1000, pitch_floor=floor, pitch_ceiling=ceiling)
        f = p.selected_array["frequency"].astype(np.float64)
        f[f <= 0] = np.nan
        return p, f

    # 1차: 넓은 범위로 이 사람의 평소 음높이를 알아내고, 2차: 그 범위로 좁혀서 다시 잰다.
    # 사람마다 목소리 높이가 달라서 고정 범위로 한 번만 재면 한 옥타브씩 튀는 오류가 난다.
    pitch, freq = track(cfg.pitch_floor_hz, cfg.pitch_ceiling_hz)
    voiced = freq[~np.isnan(freq)]
    if len(voiced) >= 20:
        q25, q75 = np.percentile(voiced, [25, 75])
        floor = max(cfg.pitch_floor_hz, cfg.pitch_floor_ratio * q25)
        ceiling = min(cfg.pitch_ceiling_hz, cfg.pitch_ceiling_ratio * q75)
        if ceiling > floor * 1.5:
            pitch, freq = track(floor, ceiling)
    times = pitch.xs()

    f0 = np.full(n_frames, np.nan)
    idx = np.round(times * 1000 / cfg.hop_ms).astype(int)
    ok = (idx >= 0) & (idx < n_frames)
    f0[idx[ok]] = freq[ok]
    return f0


def _frame_range(s: Syl, n_frames: int, cfg: SpeechConfig) -> tuple[int, int]:
    a = max(0, min(n_frames - 1, ms_to_frame(s.start_ms, cfg)))
    b = max(a + 1, min(n_frames, ms_to_frame(s.end_ms, cfg)))
    return a, b


def apply_pitch(sylls: list[Syl], f0: np.ndarray, cfg: SpeechConfig) -> float | None:
    for s in sylls:
        if not s.timed:
            continue
        a, b = _frame_range(s, len(f0), cfg)
        seg = f0[a:b]
        seg = seg[~np.isnan(seg)]
        if len(seg) >= cfg.min_voiced_frames:
            s.pitch_hz = float(np.median(seg))

    hz = [s.pitch_hz for s in sylls if s.pitch_hz]
    if len(hz) < 3:
        return None
    median_hz = float(np.median(hz))

    edges = cfg.pitch_level_edges_st
    for s in sylls:
        if s.pitch_hz is None:
            continue
        st = 12 * np.log2(s.pitch_hz / median_hz)
        s.pitch_st = float(st)
        s.pitch_level = bisect_right(edges, st) + 1
        # 표시 대상(양 끝 단계)에서 뺄 글자는 한 칸 안쪽(3 또는 5단계)으로 내린다. 방향은 유지된다.
        marked = s.pitch_level <= 2 or s.pitch_level >= len(edges)
        if marked and (
            (s.unit.is_sentence_end and not cfg.pitch_mark_sentence_end)
            or (s.pitch_level <= 2 and not cfg.pitch_mark_low)
        ):
            s.pitch_level = 3 if s.pitch_level <= 2 else len(edges) - 1
    return median_hz


def apply_volume(sylls: list[Syl], db: np.ndarray, cfg: SpeechConfig) -> float | None:
    vals: dict[int, float] = {}
    for i, s in enumerate(sylls):
        if not (s.timed and s.unit.is_hangul) or len(db) == 0:
            continue
        a, b = _frame_range(s, len(db), cfg)
        vals[i] = float(np.percentile(db[a:b], 75))
    if len(vals) < 5:
        return None
    median_db = float(np.median(list(vals.values())))
    for i, v in vals.items():
        rel = v - median_db
        sylls[i].volume_db = rel
        sylls[i].low_volume = rel <= -cfg.low_volume_db
    return median_db


def apply_stretch(sylls: list[Syl], cfg: SpeechConfig) -> None:
    ok = [s for s in sylls if s.aligned and s.unit.is_hangul and s.dur_ms > 0]
    if len(ok) < 5:
        return

    def weight(s: Syl) -> float:
        return 1.0 + (cfg.batchim_weight if s.unit.has_batchim else 0.0)

    base = float(np.median([s.dur_ms / weight(s) for s in ok]))
    for s in ok:
        expected = base * weight(s)
        s.stretch_ratio = round(s.dur_ms / expected, 2)
        limit = cfg.end_stretch_ratio if s.unit.is_sentence_end else cfg.stretch_ratio
        s.stretch = s.stretch_ratio >= limit and (s.dur_ms - expected) >= cfg.stretch_min_extra_ms


_BOUNDARY = {
    B_NONE: PauseBoundary.WORD_INTERNAL,
    B_WORD: PauseBoundary.WORD,
    B_COMMA: PauseBoundary.COMMA,
    B_LINE: PauseBoundary.LINE,
    B_SENTENCE: PauseBoundary.SENTENCE,
}


def _longest_quiet_ms(db: np.ndarray, t0_ms: float, t1_ms: float, cfg: SpeechConfig) -> float:
    """[t0, t1] 안에서 가장 긴 무음 구간의 길이(ms). 시각이 아니라 소리 에너지로 잰다."""
    a, b = max(0, int(t0_ms // cfg.hop_ms)), min(len(db), int(t1_ms // cfg.hop_ms))
    if b <= a:
        return 0.0
    quiet = db[a:b] <= speech_threshold(db)
    best = run = 0
    for q in quiet:
        run = run + 1 if q else 0
        best = max(best, run)
    return float(best * cfg.hop_ms)


def detect_pauses(
    sylls: list[Syl], cfg: SpeechConfig, skip_after: set[int] | None = None, db: np.ndarray | None = None
) -> tuple[list[Pause], float]:
    skip_after = skip_after or set()
    raw: list[tuple[Syl, float]] = []
    timed = [s for s in sylls if s.unit.is_hangul and s.timed]
    # 한글 사이에 소리 내어 읽은 영문·숫자(예: "API")가 끼어 있으면 그 구간은 무음이 아니다. 쉼으로 재지 않는다.
    spoken_between = {
        s.unit.idx for s in sylls if not s.unit.is_hangul and s.unit.char.isalnum()
    }
    alnum_idx = sorted(spoken_between)
    for a, b in zip(timed, timed[1:]):
        spans_spoken = any(a.unit.idx < i < b.unit.idx for i in alnum_idx)
        if spans_spoken:
            # 사이에 소리 내어 읽은 영문·숫자가 있으면 시각 차이는 무음이 아니다 → 에너지로 쉼 길이를 직접 잰다
            if db is None or a.unit.idx in skip_after:
                continue
            quiet = _longest_quiet_ms(db, a.end_ms, b.start_ms, cfg)
            if quiet >= cfg.pause_min_ms:
                raw.append((a, quiet))
            continue
        # 둘 다 추정 시각이어도 소리 에너지의 무음 경계에 맞춰진 경우(앞 끝·뒤 시작이 모두 무음에 맞음)는 쉼으로 본다
        if not (a.aligned or b.aligned or (a.snap_end and b.snap_start)):
            continue
        if a.unit.idx in skip_after:
            continue
        gap = b.start_ms - (a.sil_start if a.sil_start is not None else a.end_ms)
        if gap >= cfg.pause_min_ms:
            raw.append((a, gap))

    expected_gaps = [g for a, g in raw if a.unit.strength >= 2]
    typical = float(np.median(expected_gaps)) if len(expected_gaps) >= 3 else float(cfg.typical_pause_default_ms)

    pauses = [
        Pause(
            after_idx=a.unit.idx,
            duration_ms=int(round(gap)),
            boundary=_BOUNDARY[a.unit.boundary_after],
            expected=a.unit.strength >= 2,
            relative_length=round(gap / typical, 2),
        )
        for a, gap in raw
    ]
    return pauses, typical


def build_phrases(sylls: list[Syl], cfg: SpeechConfig) -> tuple[list[Phrase], float]:
    hang = [s for s in sylls if s.unit.is_hangul]

    raw: list[list[Syl]] = []
    cur: list[Syl] = []
    for k, s in enumerate(hang):
        cur.append(s)
        nxt = hang[k + 1] if k + 1 < len(hang) else None
        hard = s.unit.boundary_after in (B_COMMA, B_LINE, B_SENTENCE) or s.unit.is_sentence_end
        long_pause = (
            nxt is not None and s.aligned and nxt.aligned and (nxt.start_ms - s.end_ms) >= cfg.phrase_split_pause_ms
        )
        if nxt is None or hard or long_pause:
            raw.append(cur)
            cur = []

    merged: list[list[Syl]] = []
    carry: list[Syl] = []
    for ph in raw:
        ph = carry + ph
        carry = []
        if len(ph) < cfg.min_phrase_syllables:
            carry = ph
        else:
            merged.append(ph)
    if carry:
        if merged:
            merged[-1] += carry
        else:
            merged.append(carry)

    measured: list[tuple[list[Syl], int, float]] = []
    for ph in merged:
        ok = [s for s in ph if s.aligned]
        if len(ok) < 3 or len(ok) < cfg.phrase_min_aligned_ratio * len(ph):
            continue
        span = ok[-1].end_ms - ok[0].start_ms
        for a, b in zip(ok, ok[1:]):
            gap = b.start_ms - a.end_ms
            if gap >= cfg.pause_min_ms:
                span -= gap
        if span <= 0:
            continue
        lo, hi = cfg.plausible_rate
        if not lo <= len(ok) / (span / 1000) <= hi:
            continue
        measured.append((ph, len(ok), span / 1000))

    if not measured:
        return [], 0.0

    baseline = sum(n for _, n, _ in measured) / sum(t for _, _, t in measured)
    phrases: list[Phrase] = []
    for ph, n, t in measured:
        rate = n / t
        ratio = rate / baseline
        label = (
            SpeedLabel.FAST if ratio >= cfg.fast_ratio else SpeedLabel.SLOW if ratio <= cfg.slow_ratio else SpeedLabel.NORMAL
        )
        phrases.append(
            Phrase(
                start_idx=ph[0].unit.idx,
                end_idx=ph[-1].unit.idx,
                speed_syll_per_sec=round(rate, 2),
                speed_ratio=round(ratio, 2),
                speed_label=label,
            )
        )
    return phrases, float(baseline)
