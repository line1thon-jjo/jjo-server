"""대본-음성 정렬: 대본 각 글자의 시작·끝 시각을 찾는다. 인식되지 않은 구간은 에너지로 위치를 추정한다.
Aligner 인터페이스와 구현체(WhisperAligner / PrecomputedWordsAligner / EnergyAligner)를 둔다.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from types import SimpleNamespace
from typing import Optional, Protocol

import numpy as np

from app.contracts.analysis_result import AnalysisError, ErrorCode

from .config import DEFAULT_CONFIG, SpeechConfig
from .features import energy_db, ms_to_frame, speech_threshold, suppress_blips
from .phonetic import sound_distance
from .text import Unit, is_hangul_syllable
from .types import Syl


class Aligner(Protocol):
    def align(self, y: np.ndarray, units: list[Unit], cfg: SpeechConfig) -> list[Syl]: ...


@dataclass(frozen=True)
class WordTiming:
    text: str
    start: float
    end: float
    prob: float = 1.0


ZERO_LENGTH_CHAR_S = 0.08  # 길이가 0으로 나온 단어에 글자당 임시로 주는 시간. 이후 단계(소리 기준 보정)에서 다시 맞춘다.


def fix_zero_length_words(words: list[WordTiming]) -> list[WordTiming]:
    """Whisper 가 시작=끝 으로 준 단어를 버리지 않고 짧은 길이를 준다 (버리면 말한 글자가 '건너뜀'으로 잡힌다)."""
    out: list[WordTiming] = []
    for i, w in enumerate(words):
        n = sum(1 for c in w.text if is_hangul_syllable(c))
        if n == 0 or w.end > w.start:
            out.append(w)
            continue
        end = w.start + ZERO_LENGTH_CHAR_S * n
        nxt = next((x.start for x in words[i + 1 :] if x.start > w.start), None)
        if nxt is not None:
            end = min(end, nxt)
        out.append(replace(w, end=end))
    return out


def retime_degenerate_words(words: list[WordTiming], db: np.ndarray, cfg: SpeechConfig) -> list[WordTiming]:
    """Whisper 가 글자 수에 비해 말도 안 되게 짧은 시간(글자당 min_word_ms_per_char 미만)을 준 단어는 시각을 믿을 수 없다.
    앞뒤 단어와 묶어서, 그 구간의 '소리가 있는 부분'을 글자 수에 비례해 다시 나눈다."""
    if len(db) == 0 or not words:
        return words
    thr = speech_threshold(db)
    out = list(words)
    n = len(out)

    def n_hangul(w: WordTiming) -> int:
        return sum(1 for c in w.text if is_hangul_syllable(c))

    i = 0
    while i < n:
        w = out[i]
        c = n_hangul(w)
        if c == 0 or (w.end - w.start) * 1000 >= cfg.min_word_ms_per_char * c:
            i += 1
            continue
        lo = i - 1 if i > 0 else i
        hi = i + 1 if i + 1 < n else i
        while hi + 1 < n and (n_hangul(out[hi]) == 0 or (out[hi].end - out[hi].start) * 1000 < cfg.min_word_ms_per_char * n_hangul(out[hi])):
            hi += 1
        group = out[lo : hi + 1]
        counts = [n_hangul(g) for g in group]
        t0, t1 = group[0].start, group[-1].end
        segs = voiced_segments(db, thr, ms_to_frame(t0 * 1000, cfg), ms_to_frame(t1 * 1000, cfg), cfg)
        if not segs or sum(counts) == 0:
            i = hi + 1
            continue
        seg_len = [b - a for a, b in segs]
        total_v = float(sum(seg_len))
        cum_v = np.concatenate([[0.0], np.cumsum(seg_len)])

        def frame_at(v: float, is_end: bool) -> float:
            k = int(np.searchsorted(cum_v, v, side="left" if is_end else "right")) - 1
            k = min(max(k, 0), len(segs) - 1)
            return segs[k][0] + min(max(v - cum_v[k], 0.0), seg_len[k])

        total_c = float(sum(counts))
        acc = 0
        for gi, g in enumerate(group):
            if counts[gi] == 0:
                continue
            v0, v1 = total_v * acc / total_c, total_v * (acc + counts[gi]) / total_c
            acc += counts[gi]
            out[lo + gi] = replace(
                g, start=frame_at(v0, False) * cfg.hop_ms / 1000, end=frame_at(v1, True) * cfg.hop_ms / 1000
            )
        i = hi + 1
    return out


def match_script(units: list[Unit], words: list[WordTiming], cfg: SpeechConfig = DEFAULT_CONFIG):
    words = fix_zero_length_words(words)
    rec: list[tuple[str, float, float, int, float]] = []
    for wi, w in enumerate(words):
        chars = [c for c in w.text if is_hangul_syllable(c)]
        if not chars or w.end <= w.start:
            continue
        step = (w.end - w.start) * 1000 / len(chars)
        for k, c in enumerate(chars):
            rec.append((c, w.start * 1000 + k * step, w.start * 1000 + (k + 1) * step, wi, w.prob))

    hangul_pos = [i for i, u in enumerate(units) if u.is_hangul]
    sm = SequenceMatcher(a=[units[i].char for i in hangul_pos], b=[r[0] for r in rec], autojunk=False)
    blocks = [b for b in sm.get_matching_blocks() if b.size >= cfg.anchor_min_chars]
    return rec, hangul_pos, blocks


def align_words_to_script(
    units: list[Unit], words: list[WordTiming], cfg: SpeechConfig = DEFAULT_CONFIG
) -> list[Syl]:
    rec, hangul_pos, blocks = match_script(units, words, cfg)

    sylls = [Syl(unit=u) for u in units]
    for block in blocks:
        for k in range(block.size):
            s = sylls[hangul_pos[block.a + k]]
            _, st, en, wi, prob = rec[block.b + k]
            s.start_ms, s.end_ms, s.aligned, s.group, s.confidence = st, en, True, wi, prob

    a_chars = [units[i].char for i in hangul_pos]
    edges = [SimpleNamespace(a=0, b=0, size=0)] + list(blocks) + [SimpleNamespace(a=len(a_chars), b=len(rec), size=0)]
    for prev, nxt in zip(edges, edges[1:]):
        a_lo, a_hi = prev.a + prev.size, nxt.a
        b_lo, b_hi = prev.b + prev.size, nxt.b
        if a_hi <= a_lo or b_hi <= b_lo:
            continue
        part = rec[b_lo:b_hi]
        span = (part[0][1], part[-1][2])
        mean_prob = sum(r[4] for r in part) / len(part)
        dist = sound_distance("".join(a_chars[a_lo:a_hi]), "".join(r[0] for r in part))
        for k in range(a_lo, a_hi):
            sylls[hangul_pos[k]].hint = span
            sylls[hangul_pos[k]].mismatch = (dist, mean_prob)

    fill_unaligned(sylls)
    return sylls


def fill_unaligned(sylls: list[Syl]) -> None:
    n = len(sylls)
    i = 0
    while i < n:
        if sylls[i].timed:
            i += 1
            continue
        j = i
        while j < n and not sylls[j].timed:
            j += 1
        left = sylls[i - 1].end_ms if i > 0 else None
        right = sylls[j].start_ms if j < n else None
        if left is None and right is None:
            lo = hi = 0.0
        elif left is None:
            lo = hi = right
        elif right is None:
            lo = hi = left
        else:
            lo, hi = left, max(left, right)
        step = (hi - lo) / (j - i)
        for k in range(i, j):
            sylls[k].start_ms = lo + (k - i) * step
            sylls[k].end_ms = lo + (k - i + 1) * step
        i = j


def voiced_segments(db: np.ndarray, thr: float, a: int, b: int, cfg: SpeechConfig) -> list[tuple[int, int]]:
    a, b = max(0, a), min(len(db), b)
    segs: list[list[int]] = []
    start = None
    for i in range(a, b):
        if db[i] > thr:
            if start is None:
                start = i
        elif start is not None:
            segs.append([start, i])
            start = None
    if start is not None:
        segs.append([start, b])
    merged: list[list[int]] = []
    gap = ms_to_frame(cfg.gap_merge_ms, cfg)
    for s in segs:
        if merged and s[0] - merged[-1][1] < gap:
            merged[-1][1] = s[1]
        else:
            merged.append(s)
    min_len = ms_to_frame(cfg.gap_min_segment_ms, cfg)
    return [(x, y) for x, y in merged if y - x >= min_len]


def spread(run: list[Syl], segs: list[tuple[int, int]], cfg: SpeechConfig) -> list[int]:
    weights = [1.0 + (cfg.batchim_weight if s.unit.has_batchim else 0.0) for s in run]
    seg_len = [b - a for a, b in segs]
    total_v = float(sum(seg_len))
    total_w = float(sum(weights))
    cum_v = np.concatenate([[0.0], np.cumsum(seg_len)])

    def locate(v: float) -> tuple[int, float]:
        k = int(np.searchsorted(cum_v, v, side="right")) - 1
        k = min(max(k, 0), len(segs) - 1)
        return k, segs[k][0] + (v - cum_v[k])

    acc = 0.0
    ks: list[int] = []
    for s, w in zip(run, weights):
        v0, v1 = total_v * acc / total_w, total_v * (acc + w) / total_w
        acc += w
        k, f = locate((v0 + v1) / 2)
        lo, hi = segs[k]
        half = (v1 - v0) / 2
        s.start_ms = max(lo, min(hi, f - half)) * cfg.hop_ms
        s.end_ms = max(lo, min(hi, f + half)) * cfg.hop_ms
        ks.append(k)
    return ks


def fill_gaps(sylls: list[Syl], db: np.ndarray, cfg: SpeechConfig) -> None:
    if len(db) == 0:
        return
    thr = speech_threshold(db)
    durs = [s.dur_ms for s in sylls if s.aligned and s.dur_ms > 0]
    typical = float(np.median(durs)) if durs else float(cfg.default_syllable_ms)
    total_ms = len(db) * cfg.hop_ms
    n = len(sylls)
    i = 0
    while i < n:
        if sylls[i].aligned:
            i += 1
            continue
        j = i
        while j < n and not sylls[j].aligned:
            j += 1
        run = [s for s in sylls[i:j] if s.unit.is_hangul]
        if run:
            lo = sylls[i - 1].end_ms if i > 0 else 0.0
            hi = sylls[j].start_ms if j < n else float(total_ms)
            hint = next((s.hint for s in run if s.hint), None)
            if hint:
                lo2, hi2 = max(lo, hint[0] - cfg.hint_pad_ms), min(hi, hint[1] + cfg.hint_pad_ms)
                if hi2 <= lo2:
                    lo2, hi2 = hint
                segs = voiced_segments(db, thr, ms_to_frame(lo2, cfg), ms_to_frame(hi2, cfg), cfg)
                if segs:
                    spread(run, segs, cfg)
                else:
                    step = (hi2 - lo2) / len(run)
                    for k, s in enumerate(run):
                        s.start_ms, s.end_ms = lo2 + k * step, lo2 + (k + 1) * step
                for s in run:
                    s.inferred = True
            else:
                segs = voiced_segments(db, thr, ms_to_frame(lo, cfg), ms_to_frame(hi, cfg), cfg)
                voiced_ms = sum(b - a for a, b in segs) * cfg.hop_ms
                if segs and voiced_ms >= cfg.gap_min_fill_ratio * typical * len(run):
                    spread(run, segs, cfg)
                    for s in run:
                        s.inferred = True
        i = j


def valley_cost(db: np.ndarray, w: int = 6, cap_db: float = 10.0) -> np.ndarray:
    n = len(db)
    p = np.pad(db, (w, w), mode="edge")
    win = np.lib.stride_tricks.sliding_window_view(p, w + 1)
    left_max = win[:n].max(axis=1)
    right_max = win[w : w + n].max(axis=1)
    depth = np.clip(np.minimum(left_max, right_max) - db, 0.0, cap_db)
    return 1.0 - depth / cap_db


def split_by_energy(bcost: np.ndarray, weights: list[float], lam: float, min_len: int = 2) -> list[int]:
    n, T = len(weights), len(bcost)
    if n <= 1:
        return [0, T]
    total_w = float(sum(weights))
    exp_len = [T * w / total_w for w in weights]
    even = [int(round(T * sum(weights[:k]) / total_w)) for k in range(n + 1)]
    if T < n * min_len:
        return even

    INF = 1e18
    best = np.full((n + 1, T + 1), INF)
    back = np.zeros((n + 1, T + 1), dtype=int)
    best[0, 0] = 0.0
    for k in range(1, n + 1):
        t_lo = k * min_len
        t_hi = T - (n - k) * min_len
        ts = range(t_lo, t_hi + 1) if k < n else [T]
        for t in ts:
            s = np.arange((k - 1) * min_len, t - min_len + 1)
            if len(s) == 0:
                continue
            cost = best[k - 1, s] + lam * ((t - s - exp_len[k - 1]) / exp_len[k - 1]) ** 2
            j = int(np.argmin(cost))
            best[k, t] = cost[j] + (bcost[t] if k < n else 0.0)
            back[k, t] = s[j]
    if best[n, T] >= INF:
        return even
    bounds = [T]
    t = T
    for k in range(n, 0, -1):
        t = int(back[k, t])
        bounds.append(t)
    return bounds[::-1]


def _cut_detached_sound(
    db: np.ndarray, a: int, b: int, thr: float, cfg: SpeechConfig, min_keep_frames: int = 0
) -> tuple[int, int]:
    voiced = db[a:b] > thr
    runs: list[list[int]] = []
    start = None
    for i, v in enumerate(voiced):
        if v and start is None:
            start = i
        elif not v and start is not None:
            runs.append([start, i])
            start = None
    if start is not None:
        runs.append([start, len(voiced)])
    if len(runs) < 2:
        return a, b

    gap = ms_to_frame(cfg.detach_gap_ms, cfg)
    merged: list[list[int]] = [runs[0]]
    for r in runs[1:]:
        if r[0] - merged[-1][1] < gap:
            merged[-1][1] = r[1]
        else:
            merged.append(r)
    if len(merged) < 2:
        return a, b

    voiced_len = [sum(int(x) for x in voiced[m[0] : m[1]]) for m in merged]
    main = int(np.argmax(voiced_len))
    min_len = ms_to_frame(cfg.stray_min_ms, cfg)
    lo, hi = merged[main]
    left_extra = sum(voiced_len[:main])
    right_extra = sum(voiced_len[main + 1 :])
    # 한 음절(min_syllable_ms)도 안 되는 짧은 소리가 쉼(pause_min_ms 이상)을 건너 붙어 있으면, 한 어절의 일부가 아니라
    # 이웃 어절의 시작소리가 Whisper 어절 구간에 섞인 것이다. 이럴 때도 잘라낸다.
    syl_frames = ms_to_frame(cfg.min_syllable_ms, cfg)
    pause_frames = ms_to_frame(cfg.pause_min_ms, cfg)
    left_gap = merged[main][0] - merged[main - 1][1] if main > 0 else 0
    right_gap = merged[main + 1][0] - merged[main][1] if main + 1 < len(merged) else 0
    big_left, big_right = left_extra >= min_len, right_extra >= min_len
    cut_left = big_left or (0 < left_extra < syl_frames and left_gap >= pause_frames)
    cut_right = big_right or (0 < right_extra < syl_frames and right_gap >= pause_frames)
    # 자르고 남는 소리가 어절의 글자 수에 비해 너무 짧으면(글자가 비정상적으로 빨라진다) 떨어진 소리도 이 어절의 일부다.
    # 문장 끝 "…었어~요"처럼 어절 안에서 숨이 잠깐 끊긴 경우를 끼어든 소리로 오해하지 않기 위해서다.
    # (한 음절도 안 되는 짧은 소리를 쉼 너머에서 잘라내는 경우는 이웃 어절의 시작소리이므로 항상 자른다.)
    if (cut_left and big_left) or (cut_right and big_right):
        kept_a = a + lo if cut_left else a
        kept_b = a + hi if cut_right else b
        kept_voiced = int((db[kept_a:kept_b] > thr).sum())
        if kept_voiced < min_keep_frames:
            return a, b
    new_a = a + lo if cut_left else a
    new_b = a + hi if cut_right else b
    return new_a, new_b


def refine_groups(sylls: list[Syl], db: np.ndarray, cfg: SpeechConfig) -> None:
    if not cfg.refine_by_energy or len(db) == 0:
        return
    thr = speech_threshold(db)
    valley = valley_cost(db)
    ext = ms_to_frame(cfg.edge_extend_ms, cfg)
    n_frames = len(db)

    groups: list[list[int]] = []
    for i, s in enumerate(sylls):
        if not (s.aligned and s.group is not None):
            continue
        if groups and sylls[groups[-1][-1]].group == s.group and i == groups[-1][-1] + 1:
            groups[-1].append(i)
        else:
            groups.append([i])

    per_char = [
        (ms_to_frame(sylls[g[-1]].end_ms, cfg) - ms_to_frame(sylls[g[0]].start_ms, cfg)) / len(g)
        for g in groups
        if len(g) >= 2
    ]
    typ_char_frames = float(np.median(per_char)) if per_char else float(ms_to_frame(cfg.default_syllable_ms, cfg))

    prev_end = 0
    for gi, g in enumerate(groups):
        first, last = sylls[g[0]], sylls[g[-1]]
        a = max(0, min(n_frames - 1, ms_to_frame(first.start_ms, cfg)))
        b = max(a + 1, min(n_frames, ms_to_frame(last.end_ms, cfg)))
        upper = min(n_frames, ms_to_frame(sylls[groups[gi + 1][0]].start_ms, cfg)) if gi + 1 < len(groups) else n_frames
        upper = max(upper, b)

        local_thr = max(thr, float(db[a:b].max()) - cfg.edge_trim_db)
        left_open = g[0] == 0 or sylls[g[0] - 1].aligned
        right_open = g[-1] == len(sylls) - 1 or sylls[g[-1] + 1].aligned

        a2, b2 = a, b
        while a2 < b2 and db[a2] <= local_thr:
            a2 += 1
        while b2 > a2 and db[b2 - 1] <= local_thr:
            b2 -= 1
        if a2 >= b2:
            prev_end = b
            continue
        a, b = a2, b2
        a, b = _cut_detached_sound(db, a, b, local_thr, cfg, int(cfg.keep_char_ratio * typ_char_frames * len(g)))

        k = 0
        while left_open and a - 1 >= prev_end and db[a - 1] > local_thr and k < ext:
            a -= 1
            k += 1
        k = 0
        while right_open and b < upper and db[b] > local_thr and k < ext:
            b += 1
            k += 1
        prev_end = b

        if len(g) == 1:
            bounds = [0, b - a]
        else:
            weights = [1.0 + (cfg.batchim_weight if sylls[i].unit.has_batchim else 0.0) for i in g]
            bounds = split_by_energy(
                valley[a:b], weights, cfg.refine_lambda, min_len=max(2, ms_to_frame(cfg.min_syllable_ms, cfg))
            )
        for pos, i in enumerate(g):
            sylls[i].start_ms = (a + bounds[pos]) * cfg.hop_ms
            sylls[i].end_ms = (a + bounds[pos + 1]) * cfg.hop_ms


class PrecomputedWordsAligner:
    def __init__(self, words: list[WordTiming]):
        self.words = words

    def align(self, y: np.ndarray, units: list[Unit], cfg: SpeechConfig) -> list[Syl]:
        db = suppress_blips(energy_db(y, cfg), cfg)
        words = retime_degenerate_words(fix_zero_length_words(self.words), db, cfg)
        sylls = align_words_to_script(units, words, cfg)
        refine_groups(sylls, db, cfg)
        fill_gaps(sylls, db, cfg)
        fill_unaligned(sylls)
        return sylls

    def get_words(self, y: np.ndarray, units: list[Unit], cfg: SpeechConfig) -> list[WordTiming]:
        return self.words


class WhisperAligner:
    def __init__(
        self,
        model_size: str = "small",
        device: str = "auto",
        compute_type: str = "int8",
        use_script_prompt: bool = False,
        filler_prompt: bool = False,
    ):
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.use_script_prompt = use_script_prompt
        self.filler_prompt = filler_prompt
        self._model = None

    def _get_model(self):
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as e:
                raise AnalysisError(
                    ErrorCode.ENGINE_UNAVAILABLE, "faster-whisper 가 필요합니다: pip install faster-whisper"
                ) from e
            self._model = WhisperModel(self.model_size, device=self.device, compute_type=self.compute_type)
        return self._model

    def warm_up(self) -> None:
        self._get_model()

    def transcribe_words(self, y: np.ndarray, script: Optional[str] = None) -> list[WordTiming]:
        model = self._get_model()
        prompt = ("음... 어, 그, 저는 음... 아, 네. " if self.filler_prompt else "") + (
            script if (self.use_script_prompt and script) else ""
        )
        segments, _ = model.transcribe(
            y.astype(np.float32),
            language="ko",
            word_timestamps=True,
            beam_size=5,
            vad_filter=False,
            condition_on_previous_text=False,
            initial_prompt=prompt or None,
        )
        words: list[WordTiming] = []
        for seg in segments:
            for w in seg.words or []:
                words.append(
                    WordTiming(
                        text=w.word.strip(),
                        start=float(w.start),
                        end=float(w.end),
                        prob=float(getattr(w, "probability", 1.0)),
                    )
                )
        return words

    def get_words(self, y: np.ndarray, units: list[Unit], cfg: SpeechConfig) -> list[WordTiming]:
        words = self.transcribe_words(y, "".join(u.char for u in units))
        if not words:
            raise AnalysisError(ErrorCode.NO_SPEECH, "녹음에서 말소리를 인식하지 못했습니다.")
        return words

    def align(self, y: np.ndarray, units: list[Unit], cfg: SpeechConfig) -> list[Syl]:
        return PrecomputedWordsAligner(self.get_words(y, units, cfg)).align(y, units, cfg)


class EnergyAligner:
    def align(self, y: np.ndarray, units: list[Unit], cfg: SpeechConfig) -> list[Syl]:
        db = suppress_blips(energy_db(y, cfg), cfg)
        segs = voiced_segments(db, speech_threshold(db), 0, len(db), cfg)
        if not segs:
            raise AnalysisError(ErrorCode.NO_SPEECH, "녹음에서 말소리를 찾지 못했습니다.")

        sylls = [Syl(unit=u) for u in units]
        hang = [s for s in sylls if s.unit.is_hangul]
        ks = spread(hang, segs, cfg)
        for s, k in zip(hang, ks):
            s.aligned, s.group = True, k
        fill_unaligned(sylls)
        refine_groups(sylls, db, cfg)
        return sylls
