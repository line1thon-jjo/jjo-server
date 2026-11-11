"""AI 피드백 후보 만들기 (BE2 담당). DB와 LLM을 모르는 순수 함수.

후보 하나 = 분석에서 나온 "발생" 하나다.
- BE3 습관(2번 이상 반복된 종류)의 발생 위치 하나하나
- 빠름·느림 구절 하나하나
- 눈에 띄는 쉼 하나하나 (같은 위치의 쉼 습관과는 한 번만 센다)
인용 구절과 재생 구간은 여기서 계산한다. LLM은 후보 번호만 고르고 위치·시간을 만들지 않는다.
"""
import re
from dataclasses import dataclass, field
from typing import Any

from app.contracts.analysis_result import AnalysisResult, HabitType, SpeedLabel, Syllable
from app.domains.report import transform

QUOTE_MAX = 40
PAUSE_KINDS = frozenset({"PAUSE", HabitType.MID_PAUSE.value, HabitType.LONG_PAUSE.value})
# 문제 위치가 글자가 아니라 "이 글자 뒤"(쉼, 쉬지 않음)인 종류
GAP_KINDS = PAUSE_KINDS | {HabitType.NO_BREAK.value}
_KIND_ORDER = {"habit": 0, "PACE": 1, "PAUSE": 2}


@dataclass(frozen=True)
class Candidate:
    id: int
    kind: str  # 습관 코드 7종 / PACE / PAUSE
    quote: str
    start_idx: int
    end_idx: int
    start_ms: int
    end_ms: int
    metrics: dict[str, Any] = field(default_factory=dict)  # 프롬프트와 규칙 문구에 쓰는 수치
    # 인용 안에서 실제 문제 위치. LLM 이 인용의 다른 단어를 문제 위치로 지어내지 않게 넘긴다.
    # 글자: {"char", "word"} / 글자 뒤 사이: {"word_before", "word_after"} / 구절: {"phrase"}
    focus: dict[str, str] = field(default_factory=dict)


@dataclass
class _Event:
    group: str  # habit / PACE / PAUSE (정렬용)
    kind: str
    anchor_start: int
    anchor_end: int
    base: tuple[int, int] | None  # 인용 기준 구간. None 이면 anchor 를 담은 구절
    severity: float
    metrics: dict[str, Any]


def _trim(text: str, start: int, end: int) -> tuple[int, int]:
    while start <= end and text[start].isspace():
        start += 1
    while end >= start and text[end].isspace():
        end -= 1
    return start, end


def quote_window(text: str, base_start: int, base_end: int, anchor_start: int, anchor_end: int) -> tuple[int, int]:
    """인용 범위. 40자를 넘으면 앞부분만 쓰되, 문제 위치가 밖으로 밀리면 그 위치가 들어가게 자른다."""
    s, e = _trim(text, base_start, base_end)
    if s > e or e - s + 1 <= QUOTE_MAX:
        return s, e
    if anchor_end - s + 1 <= QUOTE_MAX:
        e = s + QUOTE_MAX - 1
        space = text.rfind(" ", anchor_end + 1, e + 1)  # 단어 중간에서 끊기지 않게
        if space != -1:
            e = space - 1
    else:
        s = anchor_end - QUOTE_MAX + 1
        space = text.find(" ", s, anchor_start)
        if space != -1:
            s = space + 1
        e = min(base_end, anchor_end + (QUOTE_MAX - (anchor_end - s + 1)))
        space = text.rfind(" ", anchor_end + 1, e + 1)
        if space != -1:
            e = space - 1
    return _trim(text, s, e)


def _word_at(text: str, i: int) -> str:
    """i 번째 글자가 든 단어(공백으로 나뉜 덩어리). 앞뒤 문장부호는 뗀다."""
    s, e = i, i
    while s > 0 and not text[s - 1].isspace():
        s -= 1
    while e < len(text) - 1 and not text[e + 1].isspace():
        e += 1
    return re.sub(r"^\W+|\W+$", "", text[s:e + 1])


def _focus(text: str, ev: _Event) -> dict[str, str]:
    if ev.kind == "PACE" and ev.base is not None:
        start, end = _trim(text, *ev.base)
        return {"phrase": text[start:end + 1].replace("\n", " ")}
    i = ev.anchor_end
    while i > 0 and text[i].isspace():
        i -= 1
    if ev.kind in GAP_KINDS:
        j = i + 1
        while j < len(text) and not text[j].isspace():  # 단어 중간이면 그 단어 끝까지 건너뛴다
            j += 1
        while j < len(text) and text[j].isspace():
            j += 1
        return {"word_before": _word_at(text, i), "word_after": _word_at(text, j) if j < len(text) else ""}
    return {"char": text[i], "word": _word_at(text, i)}


def _play_range(syllables: list[Syllable], start: int, end: int, *, extend_after: int | None,
                duration_ms: int) -> tuple[int, int] | None:
    inside = [s for s in syllables if start <= s.idx <= end]
    if not inside:
        return None
    start_ms = min(s.start_ms for s in inside)
    end_ms = max(s.end_ms for s in inside)
    if extend_after is not None:  # 쉼은 쉰 뒤 첫 글자까지 들려준다
        after = next((s for s in syllables if s.idx > extend_after), None)
        if after is not None:
            end_ms = max(end_ms, after.end_ms)
    start_ms = max(0, start_ms)
    end_ms = min(duration_ms, end_ms) if duration_ms > 0 else end_ms
    return (start_ms, end_ms) if end_ms > start_ms else None


def _events(result: AnalysisResult) -> list[_Event]:
    by_idx = {s.idx: s for s in result.syllables}
    events: list[_Event] = []
    pause_habit_spots: set[int] = set()

    for habit in transform.build_habits(result):
        source = next(h for h in result.habits if h.type == habit.type)
        for occ in source.occurrences:
            metrics: dict[str, Any] = {"habit_count": habit.count}
            # 많이 반복된 종류가 먼저, 같은 종류 안에서는 더 길게 쉬거나 더 많이 늘인 곳이 먼저.
            severity = float(habit.count) * 1_000_000
            if habit.type.value in PAUSE_KINDS:
                pause = next((p for p in result.pauses if occ.start_idx <= p.after_idx <= occ.end_idx), None)
                if pause is not None:
                    metrics["duration_ms"] = pause.duration_ms
                    severity += pause.duration_ms
                pause_habit_spots.update(range(occ.start_idx, occ.end_idx + 1))
            else:
                syl = by_idx.get(occ.end_idx)
                if syl is not None and syl.stretch:
                    metrics["stretch_ratio"] = syl.stretch_ratio
                    severity += syl.stretch_ratio * 1000
            events.append(_Event("habit", habit.type.value, occ.start_idx, occ.end_idx, None, severity, metrics))

    for phrase in result.phrases:
        if phrase.speed_label not in (SpeedLabel.FAST, SpeedLabel.SLOW):
            continue
        ratio = phrase.speed_ratio
        severity = ratio if phrase.speed_label == SpeedLabel.FAST else (1 / ratio if ratio > 0 else 0.0)
        events.append(_Event(
            "PACE", "PACE", phrase.start_idx, phrase.start_idx, (phrase.start_idx, phrase.end_idx), severity,
            {"speed": phrase.speed_label.value, "speed_ratio": ratio},
        ))

    for pause in result.pauses:
        if not transform.is_notable_pause(pause) or pause.after_idx in pause_habit_spots:
            continue
        events.append(_Event(
            "PAUSE", "PAUSE", pause.after_idx, pause.after_idx, None, float(pause.duration_ms),
            {"duration_ms": pause.duration_ms},
        ))

    # 반복된 습관(많이 나온 종류 먼저) → 빠름·느림 → 쉼. 같은 묶음 안에서는 심한 것, 그다음 앞쪽 순.
    events.sort(key=lambda ev: (_KIND_ORDER[ev.group], -ev.severity, ev.anchor_start))
    return events


def build_candidates(result: AnalysisResult) -> list[Candidate]:
    text = result.spoken_text
    if not text:
        return []
    syllables = sorted(result.syllables, key=lambda s: s.idx)
    segments = transform.build_segments(result)
    candidates: list[Candidate] = []
    for ev in _events(result):
        if ev.base is not None:
            base = ev.base
        else:
            seg = next((g for g in segments if g.start_idx <= ev.anchor_start <= g.end_idx), None)
            if seg is None:
                continue
            base = (seg.start_idx, seg.end_idx)
        base = (max(0, base[0]), min(len(text) - 1, base[1]))
        s, e = quote_window(text, base[0], base[1], ev.anchor_start, ev.anchor_end)
        if s > e:
            continue
        if any(c.start_idx == s and c.end_idx == e for c in candidates):
            continue  # 같은 구절을 두 번 인용하지 않는다(앞선, 더 심한 후보만 남김)
        play = _play_range(
            syllables, s, e, extend_after=ev.anchor_end if ev.kind in PAUSE_KINDS else None,
            duration_ms=result.duration_ms,
        )
        if play is None:  # 재생할 수 없는 후보는 쓰지 않는다
            continue
        candidates.append(Candidate(
            id=len(candidates) + 1, kind=ev.kind, quote=text[s:e + 1].replace("\n", " "),
            start_idx=s, end_idx=e, start_ms=play[0], end_ms=play[1], metrics=ev.metrics, focus=_focus(text, ev),
        ))
    return candidates
