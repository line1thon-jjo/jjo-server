"""본인 평균 기준으로 말하는 습관을 판정하고 횟수·위치를 모은다.
같은 종류가 min_habit_count 회 이상 반복될 때만 습관으로 보고한다.
"""
from __future__ import annotations

from collections import defaultdict

import numpy as np

from app.contracts.analysis_result import Habit, HabitType, IdxRange, Pause

from .config import SpeechConfig
from .text import B_NONE
from .types import Syl


def detect_habits(sylls: list[Syl], pauses: list[Pause], cfg: SpeechConfig) -> list[Habit]:
    found: dict[HabitType, list[IdxRange]] = defaultdict(list)
    marks: dict[int, list[HabitType]] = defaultdict(list)

    def add(htype: HabitType, idx: int, mark: bool = True) -> None:
        found[htype].append(IdxRange(start_idx=idx, end_idx=idx))
        if mark:
            marks[idx].append(htype)

    by_sentence: dict[int, list[Syl]] = defaultdict(list)
    for s in sylls:
        if s.unit.is_hangul and s.aligned:
            by_sentence[s.unit.sentence_id].append(s)

    for members in by_sentence.values():
        last = members[-1]
        if not last.unit.is_sentence_end:
            continue

        if last.stretch:
            add(HabitType.END_STRETCH, last.unit.idx)

        if last.low_volume:
            add(HabitType.END_FADE, last.unit.idx)

        if not last.unit.is_question_end and last.pitch_st is not None:
            pitched = [s for s in members if s.pitch_st is not None]
            head = [s.pitch_st for s in members[:-2] if s.pitch_st is not None]
            if len(pitched) >= cfg.end_rise_min_pitched and head:
                if last.pitch_st - float(np.median(head)) >= cfg.end_rise_st:
                    add(HabitType.END_RISE, last.unit.idx)

    for s in sylls:
        if s.stretch and not s.unit.is_sentence_end and s.unit.boundary_after != B_NONE:
            add(HabitType.MID_STRETCH, s.unit.idx)

    hang = [s for s in sylls if s.unit.is_hangul]
    for a, b in zip(hang, hang[1:]):
        if a.aligned and b.aligned and a.unit.strength >= 3 and (b.start_ms - a.end_ms) < cfg.no_break_ms:
            add(HabitType.NO_BREAK, a.unit.idx)

    pause_type: dict[int, HabitType] = {}
    for p in pauses:
        if not p.expected and p.duration_ms >= cfg.habit_pause_ms:
            htype = HabitType.MID_PAUSE
        elif p.expected and p.duration_ms >= cfg.long_pause_ms and p.relative_length >= cfg.long_pause_relative:
            htype = HabitType.LONG_PAUSE
        else:
            continue
        pause_type[p.after_idx] = htype
        add(htype, p.after_idx, mark=False)

    habits: list[Habit] = []
    keep: set[HabitType] = set()
    for htype, occ in found.items():
        if len(occ) < cfg.min_habit_count:
            continue
        keep.add(htype)
        occ.sort(key=lambda r: r.start_idx)
        habits.append(Habit(type=htype, count=len(occ), occurrences=occ, first_idx=occ[0].start_idx))
    habits.sort(key=lambda h: (-h.count, h.first_idx))

    for p in pauses:
        htype = pause_type.get(p.after_idx)
        if htype in keep:
            p.is_habit = True
            p.habit_type = htype
    for s in sylls:
        s.habit_types = [t.value for t in marks.get(s.unit.idx, []) if t in keep]
    return habits
