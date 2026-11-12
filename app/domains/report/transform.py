"""BE3 분석 결과(AnalysisResult)를 화면용 응답으로 바꾸는 순수 함수 모음.

DB와 FastAPI를 모른다. 규칙과 기준값은 docs/REPORT_API.md 와 같다.
글자 번호(idx)는 모두 spoken_text 기준 0부터이며 양 끝을 포함한다.
"""
import unicodedata

from app.contracts.analysis_result import (
    AnalysisResult,
    Pause,
    SpeedLabel,
    SpokenKind,
    UnclearReason,
)
from app.domains.report.schemas import (
    Added,
    HabitCount,
    Mark,
    NotablePause,
    OverviewStats,
    Pace,
    Replaced,
    Segment,
    Skipped,
    SpeedCounts,
    UtteranceAnalysis,
)

# 본인 평균보다 4반음 이상 낮거나 높은 글자. 낮음(1·2)은 BE3 설정(pitch_mark_low=False)으로 일부러 꺼 두어
# 현재는 높음(6·7)만 온다. 실제로 들었을 때 맞는 비율이 낮아(1/6) 검증 전까지 끈 것이라 여기서는 그대로 둔다.
MARK_PITCH_LEVELS = frozenset({1, 2, 6, 7})
MID_PAUSE_MIN_MS = 400  # 문장 중간에서 표시할 최소 쉼
LONG_PAUSE_MIN_MS = 700  # 숨 고를 자리에서 표시할 최소 쉼
LONG_PAUSE_MIN_RELATIVE = 2.0  # 숨 고를 자리에서 평소(본인 중앙값)의 몇 배 이상이어야 하는지
SKIPPED_MIN_LENGTH = 2  # 건너뜀으로 보여줄 최소 길이(글자 번호 기준)
MAX_HABITS = 7
MIN_HABIT_COUNT = 2
SEGMENT_CUT_PAUSE_MS = 200  # 이 길이 이상 쉬면 구절을 나눈다
SEGMENT_MIN_SYLLABLES = 4  # 한글 글자가 이보다 적은 구절은 이웃 구절과 합친다
_SEGMENT_CUT_CHARS = frozenset(",.?!\n")


def build_marks(result: AnalysisResult) -> list[Mark]:
    marks: list[Mark] = []
    for s in result.syllables:
        pitch = s.pitch_level if s.pitch_level in MARK_PITCH_LEVELS else None
        stretch = s.stretch_ratio if s.stretch else None
        if pitch is not None or stretch is not None:
            marks.append(Mark(idx=s.idx, pitch=pitch, stretch=stretch))
    return sorted(marks, key=lambda m: m.idx)


def build_paces(result: AnalysisResult) -> list[Pace]:
    paces = [
        Pace(start_idx=p.start_idx, end_idx=p.end_idx, label=p.speed_label.value)
        for p in result.phrases
        if p.speed_label in (SpeedLabel.FAST, SpeedLabel.SLOW)
    ]
    return sorted(paces, key=lambda p: p.start_idx)


def build_speed_counts(result: AnalysisResult) -> SpeedCounts:
    labels = [p.speed_label for p in result.phrases]
    fast = labels.count(SpeedLabel.FAST)
    normal = labels.count(SpeedLabel.NORMAL)
    slow = labels.count(SpeedLabel.SLOW)
    return SpeedCounts(fast=fast, normal=normal, slow=slow, measured=fast + normal + slow)


def build_habits(result: AnalysisResult) -> list[HabitCount]:
    habits = [HabitCount(type=h.type, count=h.count) for h in result.habits if h.count >= MIN_HABIT_COUNT]
    habits.sort(key=lambda h: h.count, reverse=True)  # 정렬은 안정적이라 동률은 BE3 순서를 유지
    return habits[:MAX_HABITS]


def is_notable_pause(p: Pause) -> bool:
    """화면과 피드백에 보여줄 만한 쉼인지. 문장 중간은 400ms, 숨 고를 자리는 700ms·평소의 2배 이상."""
    if p.expected:
        return p.duration_ms >= LONG_PAUSE_MIN_MS and p.relative_length >= LONG_PAUSE_MIN_RELATIVE
    return p.duration_ms >= MID_PAUSE_MIN_MS


def build_pauses(result: AnalysisResult) -> list[NotablePause]:
    """BE3의 is_habit(2번 이상 반복돼야 참)과 상관없이, 한 번만 있어도 보여줄 쉼을 고른다."""
    pauses = [
        NotablePause(after_idx=p.after_idx, duration_ms=p.duration_ms)
        for p in result.pauses
        if is_notable_pause(p)
    ]
    return sorted(pauses, key=lambda p: p.after_idx)


def build_replaced(result: AnalysisResult) -> list[Replaced]:
    items = [
        Replaced(start_idx=d.start_idx, end_idx=d.end_idx, spoken=d.spoken, script_text=d.script_text)
        for d in result.spoken_differences
        if d.kind == SpokenKind.REPLACED
    ]
    return sorted(items, key=lambda d: d.start_idx)


def build_added(result: AnalysisResult) -> list[Added]:
    items = [
        Added(start_idx=d.start_idx, end_idx=d.end_idx, spoken=d.spoken)
        for d in result.spoken_differences
        if d.kind == SpokenKind.ADDED
    ]
    return sorted(items, key=lambda d: d.start_idx)


def build_skipped(result: AnalysisResult) -> list[Skipped]:
    items = [
        Skipped(start_idx=u.start_idx, end_idx=u.end_idx)
        for u in result.unclear_ranges
        if u.reason == UnclearReason.SKIPPED and u.end_idx - u.start_idx + 1 >= SKIPPED_MIN_LENGTH
    ]
    return sorted(items, key=lambda s: s.start_idx)


def _extend_over_trailing(text: str, idx: int) -> int:
    """자르는 위치 뒤에 붙은 공백과 닫는 문장부호는 앞 구절에 포함한다."""
    while idx + 1 < len(text):
        nxt = text[idx + 1]
        if nxt.isspace() or unicodedata.category(nxt) in ("Pe", "Pf", "Po"):
            idx += 1
        else:
            break
    return idx


def build_segments(result: AnalysisResult) -> list[Segment]:
    """본문 전체를 빈틈없이 구절로 나누고 구절마다 재생 구간(ms)을 붙인다.

    BE3의 phrases 는 속도를 못 잰 구절을 빼므로 본문을 다 덮지 못해서 직접 나눈다.
    문장부호·줄바꿈 뒤와 SEGMENT_CUT_PAUSE_MS 이상 쉰 곳 뒤에서 자르고,
    한글 글자가 SEGMENT_MIN_SYLLABLES 개 미만인 구절은 앞 구절(첫 구절이면 뒤 구절)에 합친다.
    """
    text = result.spoken_text
    n = len(text)
    syllables = sorted(result.syllables, key=lambda s: s.idx)
    if n == 0 or not syllables:
        return []

    cuts: set[int] = set()
    for i, ch in enumerate(text):
        if ch in _SEGMENT_CUT_CHARS:
            cuts.add(_extend_over_trailing(text, i))
    for p in result.pauses:
        if p.duration_ms >= SEGMENT_CUT_PAUSE_MS and 0 <= p.after_idx < n:
            cuts.add(_extend_over_trailing(text, p.after_idx))

    def count_in(start: int, end: int) -> int:
        return sum(1 for s in syllables if start <= s.idx <= end)

    ranges: list[tuple[int, int]] = []
    start = 0
    for cut in sorted(cuts):
        if cut < n - 1:
            ranges.append((start, cut))
            start = cut + 1
    ranges.append((start, n - 1))

    merged: list[tuple[int, int]] = []
    carry_start: int | None = None  # 첫 구절들이 작을 때 뒤 구절로 넘겨 합친다
    for s, e in ranges:
        if carry_start is not None:
            s, carry_start = carry_start, None
        if count_in(s, e) >= SEGMENT_MIN_SYLLABLES:
            merged.append((s, e))
        elif merged:
            merged[-1] = (merged[-1][0], e)
        else:
            carry_start = s
    if carry_start is not None:  # 뒤 구절이 없었다는 뜻이므로 전체가 한 구절
        merged.append((carry_start, n - 1))

    segments: list[Segment] = []
    for s, e in merged:
        inside = [x for x in syllables if s <= x.idx <= e]
        if not inside:
            continue
        segments.append(Segment(
            start_idx=s, end_idx=e,
            start_ms=min(x.start_ms for x in inside),
            end_ms=max(x.end_ms for x in inside),
        ))
    return segments


def build_overview_stats(result: AnalysisResult) -> OverviewStats:
    return OverviewStats(speed=build_speed_counts(result), habits=build_habits(result))


def build_utterance(result: AnalysisResult) -> UtteranceAnalysis:
    return UtteranceAnalysis(
        spoken_text=result.spoken_text,
        marks=build_marks(result),
        paces=build_paces(result),
        pauses=build_pauses(result),
        segments=build_segments(result),
        replaced=build_replaced(result),
        added=build_added(result),
        skipped=build_skipped(result),
    )
