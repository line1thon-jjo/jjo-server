from typing import Any

from app.contracts.analysis_result import (
    AnalysisResult,
    Habit,
    HabitType,
    IdxRange,
    Pause,
    PauseBoundary,
    Phrase,
    SpeedLabel,
    SpokenDifference,
    SpokenKind,
    Summary,
    Syllable,
    UnclearRange,
    UnclearReason,
)
from app.domains.report import transform


def make_result(text: str = "", **overrides: Any) -> AnalysisResult:
    """text 의 한글 글자마다 100ms 짜리 음절을 하나씩 만든다."""
    syllables = [
        Syllable(idx=i, char=ch, start_ms=i * 100, end_ms=i * 100 + 100)
        for i, ch in enumerate(text)
        if "가" <= ch <= "힣"
    ]
    data: dict[str, Any] = dict(
        duration_ms=max(len(text) * 100, 1500), spoken_text=text, syllables=syllables,
        pauses=[], phrases=[], habits=[],
        summary=Summary(avg_syllables_per_sec=5.0, articulation_syllables_per_sec=6.0, aligned_ratio=1.0),
    )
    data.update(overrides)
    return AnalysisResult(**data)


def phrase(start: int, end: int, label: SpeedLabel) -> Phrase:
    return Phrase(start_idx=start, end_idx=end, speed_syll_per_sec=5.0, speed_ratio=1.0, speed_label=label)


def pause(after: int, ms: int, *, expected: bool = False, relative: float = 1.0) -> Pause:
    return Pause(after_idx=after, duration_ms=ms, boundary=PauseBoundary.WORD,
                 expected=expected, relative_length=relative)


# ---- marks ----

def test_marks_only_extreme_pitch_and_stretch():
    syllables = [
        Syllable(idx=0, char="가", start_ms=0, end_ms=100, pitch_level=4),
        Syllable(idx=1, char="나", start_ms=100, end_ms=200, pitch_level=7),
        Syllable(idx=2, char="다", start_ms=200, end_ms=300, pitch_level=3, stretch=True, stretch_ratio=2.8),
        Syllable(idx=3, char="라", start_ms=300, end_ms=400, pitch_level=1, stretch=True, stretch_ratio=2.3),
        Syllable(idx=4, char="마", start_ms=400, end_ms=500, pitch_level=5, stretch_ratio=1.9),
    ]
    marks = transform.build_marks(make_result("가나다라마", syllables=syllables))
    assert [(m.idx, m.pitch, m.stretch) for m in marks] == [(1, 7, None), (2, None, 2.8), (3, 1, 2.3)]


def test_marks_empty_when_nothing_notable():
    assert transform.build_marks(make_result("가나다")) == []


# ---- paces / speed ----

def test_paces_exclude_normal_and_speed_counts_include_all():
    phrases = [
        phrase(0, 4, SpeedLabel.FAST), phrase(6, 9, SpeedLabel.NORMAL),
        phrase(11, 15, SpeedLabel.SLOW), phrase(17, 20, SpeedLabel.NORMAL),
    ]
    result = make_result("가", phrases=phrases)
    assert [(p.start_idx, p.end_idx, p.label) for p in transform.build_paces(result)] == [
        (0, 4, "FAST"), (11, 15, "SLOW"),
    ]
    speed = transform.build_speed_counts(result)
    assert (speed.fast, speed.normal, speed.slow, speed.measured) == (1, 2, 1, 4)


def test_speed_counts_zero_when_no_phrases():
    speed = transform.build_speed_counts(make_result("가"))
    assert speed.measured == 0


# ---- habits ----

def habit(kind: HabitType, count: int) -> Habit:
    return Habit(type=kind, count=count, occurrences=[IdxRange(start_idx=0, end_idx=1)], first_idx=0)


def test_habits_sorted_by_count_and_limited():
    habits = [habit(HabitType.END_FADE, 3), habit(HabitType.END_RISE, 8), habit(HabitType.MID_PAUSE, 3),
              habit(HabitType.NO_BREAK, 1)]
    out = transform.build_habits(make_result("가", habits=habits))
    assert [(h.type, h.count) for h in out] == [
        (HabitType.END_RISE, 8), (HabitType.END_FADE, 3), (HabitType.MID_PAUSE, 3),
    ]


# ---- pauses ----

def test_pauses_follow_display_rules_regardless_of_habit_flag():
    pauses = [
        pause(1, 399),  # 문장 중간, 기준 미만
        pause(2, 400),  # 문장 중간, 한 번만 있어도 표시
        pause(3, 800, expected=True, relative=1.9),  # 숨 고를 자리, 평소의 2배 미만
        pause(4, 699, expected=True, relative=3.0),  # 숨 고를 자리, 700ms 미만
        pause(5, 700, expected=True, relative=2.0),  # 표시
    ]
    out = transform.build_pauses(make_result("가", pauses=pauses))
    assert [(p.after_idx, p.duration_ms) for p in out] == [(2, 400), (5, 700)]


# ---- 대본 일치 ----

def test_replaced_added_skipped():
    diffs = [
        SpokenDifference(kind=SpokenKind.ADDED, start_idx=20, end_idx=24, spoken="정말 열심히"),
        SpokenDifference(kind=SpokenKind.REPLACED, start_idx=5, end_idx=11, spoken="프레젠테이션을", script_text="발표를"),
    ]
    unclear = [
        UnclearRange(start_idx=30, end_idx=30, reason=UnclearReason.SKIPPED, confidence=0.9),  # 1글자
        UnclearRange(start_idx=40, end_idx=42, reason=UnclearReason.SKIPPED, confidence=0.9),
        UnclearRange(start_idx=50, end_idx=53, reason=UnclearReason.LOW_VOLUME, confidence=0.9),
    ]
    result = make_result("가", spoken_differences=diffs, unclear_ranges=unclear)
    assert [(r.start_idx, r.end_idx, r.spoken, r.script_text) for r in transform.build_replaced(result)] == [
        (5, 11, "프레젠테이션을", "발표를"),
    ]
    assert [(a.start_idx, a.end_idx, a.spoken) for a in transform.build_added(result)] == [(20, 24, "정말 열심히")]
    assert [(s.start_idx, s.end_idx) for s in transform.build_skipped(result)] == [(40, 42)]


# ---- segments ----

def test_segments_cut_after_punctuation_and_cover_text():
    text = "안녕하세요 여러분, 반갑습니다."
    segs = transform.build_segments(make_result(text))
    assert [(s.start_idx, s.end_idx) for s in segs] == [(0, 10), (11, 16)]
    assert (segs[0].start_ms, segs[0].end_ms) == (0, 900)  # 여러분(분=8)의 끝
    assert (segs[1].start_ms, segs[1].end_ms) == (1100, 1600)  # 반(11)부터 다(15)의 끝


def test_segments_cut_after_long_pause():
    text = "저는 오늘 발표를 시작합니다"
    segs = transform.build_segments(make_result(text, pauses=[pause(8, 300)]))
    assert [(s.start_idx, s.end_idx) for s in segs] == [(0, 9), (10, 14)]


def test_segments_ignore_short_pause():
    text = "저는 오늘 발표를 시작합니다"
    segs = transform.build_segments(make_result(text, pauses=[pause(8, 199)]))
    assert [(s.start_idx, s.end_idx) for s in segs] == [(0, 14)]


def test_segments_merge_small_first_segment_forward():
    text = "네, 알겠습니다."
    segs = transform.build_segments(make_result(text))
    assert [(s.start_idx, s.end_idx) for s in segs] == [(0, len(text) - 1)]


def test_segments_merge_small_segment_into_previous():
    text = "오늘 발표를 시작합니다. 네."
    segs = transform.build_segments(make_result(text))
    assert [(s.start_idx, s.end_idx) for s in segs] == [(0, len(text) - 1)]


def test_segments_cover_whole_text_without_gaps():
    text = "첫 번째 문장입니다.\n두 번째 문장은 조금 더 길게 이어집니다, 그리고 계속됩니다. 마지막입니다!"
    segs = transform.build_segments(make_result(text, pauses=[pause(30, 500)]))
    assert segs[0].start_idx == 0 and segs[-1].end_idx == len(text) - 1
    for prev, cur in zip(segs, segs[1:]):
        assert cur.start_idx == prev.end_idx + 1


def test_segments_empty_without_syllables_or_text():
    assert transform.build_segments(make_result("")) == []
    assert transform.build_segments(make_result("...", syllables=[])) == []


# ---- 조립 ----

def test_build_utterance_and_overview():
    result = make_result("안녕하세요 여러분, 반갑습니다.", habits=[habit(HabitType.END_RISE, 2)],
                         phrases=[phrase(0, 4, SpeedLabel.NORMAL)])
    utterance = transform.build_utterance(result)
    assert utterance.spoken_text == "안녕하세요 여러분, 반갑습니다."
    assert utterance.marks == [] and utterance.paces == [] and utterance.pauses == []
    assert len(utterance.segments) == 2
    overview = transform.build_overview_stats(result)
    assert overview.speed.measured == 1 and overview.habits[0].count == 2
