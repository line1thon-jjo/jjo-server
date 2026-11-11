"""BE3 음성 분석 모듈 테스트. 합성 음성(정답을 아는 소리)과 가짜 Whisper 모델로 모델 없이 전체 흐름을 검증한다."""
from dataclasses import replace
from types import SimpleNamespace as NS

import numpy as np
import pytest

from app.contracts.analysis_result import (
    AnalysisError,
    AnalysisResult,
    ErrorCode,
    HabitType,
    SpeedLabel,
    UnclearReason,
)
from app.speech.align import (
    EnergyAligner,
    PrecomputedWordsAligner,
    WhisperAligner,
    WordTiming,
    align_words_to_script,
    retime_degenerate_words,
    split_by_energy,
)
from app.speech.analyze import analyze
from app.speech.features import _longest_quiet_ms, suppress_blips
from app.speech.config import DEFAULT_CONFIG
from app.speech.text import parse_script

from app.speech.tests.synth import DEMO_SCRIPT, demo_spec, synthesize, words_from_truth, write_wav


def run(tmp_path, script, spoken=None, overrides=None, gaps=None, words_filter=None, words_map=None, config=DEFAULT_CONFIG):
    y, truth, units = synthesize(spoken or script, overrides=overrides, gaps=gaps)
    wav = tmp_path / "a.wav"
    write_wav(wav, y)
    words = words_from_truth(units, truth)
    if words_filter:
        words = [w for w in words if words_filter(w)]
    if words_map:
        words = [words_map(w) for w in words]
    return analyze(wav, script, aligner=PrecomputedWordsAligner(words), config=config), truth


def test_parse_script_boundaries():
    script = "안녕, 반가워요. 오늘 3.5점이에요?\n네"
    units = parse_script(script)
    assert "".join(u.char for u in units) == "안녕반가워요오늘35점이에요네"
    assert units[1].boundary_after == "COMMA"
    assert script[units[1].idx] == "녕"
    assert [u for u in units if u.is_sentence_end][0].char == "요"
    q = [u for u in units if u.is_question_end]
    assert len(q) == 1 and q[0].char == "요"
    assert units[-1].is_sentence_end


def test_parse_script_without_punctuation_uses_line_breaks():
    units = parse_script("오늘은 발표를 합니다\n내일은 쉽니다")
    assert [u.char for u in units if u.is_sentence_end] == ["다", "다"]


def test_infers_sentence_ends_without_punctuation():
    units = parse_script("오늘은 발표를 시작하겠습니다 이번 주제는 결제 문화입니다 감사합니다")
    ends = [u for u in units if u.is_sentence_end]
    assert [u.char for u in ends] == ["다", "다", "다"]
    assert all(u.strength == 3 for u in ends)


def test_infers_clause_breaks_and_questions():
    units = parse_script("비가 오는데 우산을 챙겼어요 이해가 되셨나요")
    by_char = {u.idx: u for u in units}
    clause = [u for u in units if u.boundary_after == "COMMA"]
    assert [u.char for u in clause] == ["데"]
    assert clause[0].strength == 2
    ends = [u for u in units if u.is_sentence_end]
    assert [u.char for u in ends] == ["요", "요"]
    assert ends[0].is_question_end is False and ends[1].is_question_end is True
    assert by_char[ends[0].idx].strength == 3


def test_noun_ending_in_yo_is_not_sentence_end():
    units = parse_script("이것이 필요 합니다")
    assert [u.char for u in units if u.is_sentence_end] == ["다"]


def test_explicit_punctuation_disables_sentence_inference():
    units = parse_script("저는 갑니다 그리고 옵니다. 끝")
    assert [u.char for u in units if u.is_sentence_end] == ["다", "끝"]


def test_align_words_marks_skipped_part():
    units = parse_script("저는 오늘 발표를 합니다")
    words = [
        WordTiming("저는", 0.0, 0.4),
        WordTiming("발표를", 0.5, 1.1),
        WordTiming("합니다", 1.2, 1.8),
    ]
    sylls = align_words_to_script(units, words)
    flags = {s.unit.char: s.aligned for s in sylls}
    assert flags["저"] and flags["합"] and flags["발"]
    assert [s.unit.char for s in sylls if not s.aligned] == ["오", "늘"]
    starts = [s.start_ms for s in sylls]
    assert starts == sorted(starts)


def test_zero_length_word_is_kept_not_skipped():
    # Whisper(medium)가 시작=끝 으로 준 단어("발표를")를 버리면 말한 글자가 건너뜀으로 잡힌다
    units = parse_script("저는 오늘 발표를 합니다")
    words = [
        WordTiming("저는", 0.0, 0.4),
        WordTiming("오늘", 0.5, 0.9),
        WordTiming("발표를", 1.0, 1.0),
        WordTiming("합니다", 1.2, 1.8),
    ]
    sylls = align_words_to_script(units, words)
    assert all(s.aligned for s in sylls)
    starts = [s.start_ms for s in sylls]
    assert starts == sorted(starts)
    assert sylls[4].end_ms <= sylls[7].start_ms


def test_zero_length_words_in_a_row_do_not_collapse():
    units = parse_script("저는 오늘 발표를 합니다")
    words = [
        WordTiming("저는", 0.0, 0.4),
        WordTiming("오늘", 0.5, 0.5),
        WordTiming("발표를", 0.5, 0.5),
        WordTiming("합니다", 1.2, 1.8),
    ]
    sylls = align_words_to_script(units, words)
    assert all(s.aligned for s in sylls)
    assert all(s.end_ms >= s.start_ms for s in sylls)


def test_single_char_match_is_not_an_anchor():
    units = parse_script("저는 오늘 발표를 합니다")
    words = [
        WordTiming("저는", 0.0, 0.4),
        WordTiming("늘", 0.5, 0.7),
        WordTiming("합니다", 1.2, 1.8),
    ]
    sylls = align_words_to_script(units, words)
    assert [s.unit.char for s in sylls if not s.aligned] == ["오", "늘", "발", "표", "를"]


def test_word_probability_is_carried_to_syllables():
    units = parse_script("저는 오늘 발표를 합니다")
    words = [
        WordTiming("저는", 0.0, 0.4, 0.95),
        WordTiming("오늘", 0.5, 0.9, 0.3),
        WordTiming("발표를", 1.0, 1.5),
        WordTiming("합니다", 1.6, 2.2),
    ]
    sylls = align_words_to_script(units, words)
    assert sylls[0].confidence == 0.95
    assert sylls[2].confidence == 0.3 and sylls[3].confidence == 0.3


def test_split_by_energy_snaps_to_dips():
    bcost = np.ones(50)
    bcost[10] = bcost[20] = 0.2
    b = split_by_energy(bcost, [1, 1, 1], lam=0.3)
    assert abs(b[1] - 10) <= 1 and abs(b[2] - 20) <= 1


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    ov, gp, marks = demo_spec()
    y, truth, units = synthesize(DEMO_SCRIPT, overrides=ov, gaps=gp)
    wav = tmp_path_factory.mktemp("audio") / "demo.wav"
    write_wav(wav, y)
    aligner = PrecomputedWordsAligner(words_from_truth(units, truth))
    result = analyze(wav, DEMO_SCRIPT, aligner=aligner)
    return result, truth, marks, wav


def test_contract_roundtrip(demo):
    result, *_ = demo
    d = result.to_json_dict()
    assert d["version"] == "1.1"
    s0 = d["syllables"][0]
    for key in ("startMs", "pitchLevel", "pitchSt", "volumeDb", "lowVolume", "inferred"):
        assert key in s0
    assert "unclearRanges" in d and "typicalPauseMs" in d["summary"]
    assert "expected" in d["pauses"][0] and "relativeLength" in d["pauses"][0]
    AnalysisResult.model_validate(d)


def test_syllable_timing_close_to_truth(demo):
    result, truth, *_ = demo
    by_idx = {s.idx: s for s in result.syllables}
    errs = [abs(by_idx[t.idx].start_ms - t.start_ms) for t in truth]
    assert np.median(errs) < 30
    assert np.mean(np.array(errs) < 80) > 0.85


def test_detects_planted_habits(demo):
    result, _, marks, _ = demo
    habits = {h.type: h for h in result.habits}
    assert set(habits) == {HabitType.END_STRETCH, HabitType.END_RISE, HabitType.MID_PAUSE}
    assert habits[HabitType.END_STRETCH].count == 2
    assert {o.start_idx for o in habits[HabitType.END_STRETCH].occurrences} == {marks["end1"], marks["end3"]}
    assert habits[HabitType.END_RISE].count == 2
    assert {o.start_idx for o in habits[HabitType.END_RISE].occurrences} == {marks["end2"], marks["end3"]}
    assert habits[HabitType.MID_PAUSE].count == 2
    assert habits[HabitType.MID_PAUSE].first_idx == DEMO_SCRIPT.index("오늘") + 1


def test_pauses_expected_vs_unexpected(demo):
    result, *_ = demo
    mid = [p for p in result.pauses if p.is_habit]
    assert len(mid) == 2 and all(650 - 60 <= p.duration_ms <= 700 + 60 for p in mid)
    assert all(not p.expected and p.habit_type == HabitType.MID_PAUSE for p in mid)
    sentence = [p for p in result.pauses if p.boundary.value == "SENTENCE"]
    assert sentence and all(p.expected and not p.is_habit for p in sentence)
    assert result.summary.typical_pause_ms == pytest.approx(450, abs=60)
    assert all(p.relative_length > 1.2 for p in mid)


def test_pitch_levels_and_speed(demo):
    result, _, marks, _ = demo
    by_idx = {s.idx: s for s in result.syllables}
    top = by_idx[marks["end2"]]
    # 문장 끝 글자는 +5반음이어도 '높음' 표시 대상에서 빠진다(5단계). 측정값과 말끝 올림 습관 판정은 그대로다.
    assert top.pitch_level == 5 and top.pitch_st == pytest.approx(5.0, abs=0.7)
    assert top.is_habit
    assert by_idx[marks["end1"]].stretch and by_idx[marks["end1"]].stretch_ratio > 2
    mids = [s for s in result.syllables if s.pitch_level == 4]
    assert len(mids) > len(result.syllables) // 2
    last = [p for p in result.phrases if p.start_idx == marks["start4"]]
    assert last and last[0].speed_label == SpeedLabel.FAST
    assert any(p.speed_label == SpeedLabel.NORMAL for p in result.phrases)
    assert result.summary.aligned_ratio == 1.0
    assert result.summary.median_pitch_hz == pytest.approx(120, abs=6)
    assert result.unclear_ranges == []


def test_skipped_part_with_no_sound_is_reported_as_skipped(tmp_path):
    script = "저는 오늘 발표를 시작하겠습니다. 이번 주제는 결제 문화입니다."
    spoken = "저는 오늘 발표를 시작하겠습니다. 이번 주제는 문화입니다."
    result, _ = run(tmp_path, script, spoken=spoken)
    k = script.index("결제")
    assert [(r.start_idx, r.end_idx) for r in result.mismatched_ranges] == [(k, k + 1)]
    assert [(r.start_idx, r.end_idx, r.reason) for r in result.unclear_ranges] == [
        (k, k + 1, UnclearReason.SKIPPED)
    ]
    bad = [s for s in result.syllables if not s.aligned]
    assert [s.char for s in bad] == ["결", "제"] and not any(s.inferred for s in bad)


def test_mumbled_part_with_sound_is_located_by_energy(tmp_path):
    script = "저는 오늘 발표를 시작하겠습니다. 이번 주제는 결제 문화입니다."
    result, truth = run(tmp_path, script, words_filter=lambda w: w.text != "결제")
    k = script.index("결제")
    guessed = [s for s in result.syllables if s.idx in (k, k + 1)]
    assert all(s.inferred and not s.aligned for s in guessed)
    t = {x.idx: x for x in truth}
    for s in guessed:
        assert abs(s.start_ms - t[s.idx].start_ms) < 100
    assert [(r.start_idx, r.end_idx, r.reason) for r in result.unclear_ranges] == [
        (k, k + 1, UnclearReason.LOW_RECOGNITION)
    ]
    assert not any(s.stretch for s in guessed)


def test_syllable_heard_differently_inside_word_is_located_not_skipped(tmp_path):
    script = "저는 오늘 발표를 시작하겠습니다. 이번 주제는 결제 문화입니다."
    swap = lambda w: WordTiming("발포를", w.start, w.end, w.prob) if w.text == "발표를" else w
    result, truth = run(tmp_path, script, words_map=swap)
    k = script.index("표")
    by_idx = {s.idx: s for s in result.syllables}
    assert by_idx[k - 1].aligned and by_idx[k + 1].aligned
    assert by_idx[k].inferred and not by_idx[k].aligned
    t = {x.idx: x for x in truth}
    assert abs(by_idx[k].start_ms - t[k].start_ms) < 100
    # 비슷한 소리의 다른 글자로 적힌 것만으로는 흐림의 증거가 약해서 기본 설정에서는 불분명으로 내지 않는다
    assert result.unclear_ranges == []
    strict = replace(DEFAULT_CONFIG, low_recognition_min_conf=0.0)
    result, _ = run(tmp_path, script, words_map=swap, config=strict)
    reasons = [(r.start_idx, r.end_idx, r.reason) for r in result.unclear_ranges]
    assert reasons == [(k, k, UnclearReason.LOW_RECOGNITION)]


def test_penultimate_pitch_peak_is_not_end_rise(tmp_path):
    script = "하나입니다. 둘입니다. 셋입니다. 넷입니다."
    up = 120 * 2 ** (5 / 12)
    penult = {script.index(w) + len(w) - 2: {"f0": (up, up)} for w in ("하나입니다", "둘입니다", "셋입니다")}
    result, _ = run(tmp_path, script, overrides=penult)
    assert HabitType.END_RISE not in {h.type for h in result.habits}


def test_unclear_ranges_do_not_cross_sentence_boundaries(tmp_path):
    script = "저는 오늘 발표를 시작하겠습니다. 이번 주제는 결제 문화입니다. 먼저 배경을 설명하겠습니다."
    end1 = script.index("시작하겠습니다") + 6
    quiet = {i: {"amp": 0.04} for i in (end1 - 1, end1, end1 + 3, end1 + 4)}
    result, _ = run(tmp_path, script, overrides=quiet, config=replace(DEFAULT_CONFIG, low_volume_is_unclear=True))
    low = [r for r in result.unclear_ranges if r.reason == UnclearReason.LOW_VOLUME]
    assert len(low) == 2 and low[0].end_idx == end1 and low[1].start_idx > end1


def test_low_volume_run_is_reported(tmp_path):
    script = "저는 오늘 발표를 시작하겠습니다. 이번 주제는 결제 문화입니다. 먼저 배경을 설명하겠습니다."
    end = script.index("시작하겠습니다") + 6
    quiet = {i: {"amp": 0.04} for i in range(end - 2, end + 1)}
    result, _ = run(tmp_path, script, overrides=quiet)
    # 작은 소리는 발음이 흐린 것과 다르다(문장 끝에서 작아지는 건 자연스럽다). 기본은 불분명으로 내지 않고 음절 표시만 남긴다.
    assert not [r for r in result.unclear_ranges if r.reason == UnclearReason.LOW_VOLUME]
    by_idx = {s.idx: s for s in result.syllables}
    assert by_idx[end].low_volume and by_idx[end].volume_db < -14
    assert by_idx[script.index("저")].low_volume is False
    result, _ = run(tmp_path, script, overrides=quiet, config=replace(DEFAULT_CONFIG, low_volume_is_unclear=True))
    low = [r for r in result.unclear_ranges if r.reason == UnclearReason.LOW_VOLUME]
    assert len(low) == 1 and low[0].end_idx == end and low[0].start_idx <= end - 1


def test_low_whisper_probability_is_reported(tmp_path):
    script = "저는 오늘 발표를 시작하겠습니다. 이번 주제는 결제 문화입니다."
    y, truth, units = synthesize(script)
    wav = tmp_path / "p.wav"
    write_wav(wav, y)
    words = [WordTiming(w.text, w.start, w.end, 0.2 if w.text == "결제" else 0.95) for w in words_from_truth(units, truth)]
    k = script.index("결제")
    # 모델이 대본과 같은 글자로 알아들었는데 확신만 낮은 경우는 근거를 절반만 인정하고, 하한(0.65)에 못 미치면 불분명으로 내지 않는다
    result = analyze(wav, script, aligner=PrecomputedWordsAligner(words))
    assert not [r for r in result.unclear_ranges if r.reason == UnclearReason.LOW_RECOGNITION]
    loose = replace(DEFAULT_CONFIG, low_recognition_min_conf=0.0)
    result = analyze(wav, script, aligner=PrecomputedWordsAligner(words), config=loose)
    rng = [r for r in result.unclear_ranges if r.reason == UnclearReason.LOW_RECOGNITION]
    assert [(r.start_idx, r.end_idx) for r in rng] == [(k, k + 1)]
    assert rng[0].confidence == pytest.approx(0.4, abs=0.01)


def test_end_fade_and_long_pause_and_no_break(tmp_path):
    script = "하나입니다. 둘입니다. 셋입니다. 넷입니다. 다섯입니다. 여섯입니다. 일곱입니다."
    ends = [script.index(w) + len(w) - 1 for w in ("하나입니다", "둘입니다", "셋입니다", "넷입니다", "다섯입니다", "여섯입니다", "일곱입니다")]

    long_gaps = {ends[0]: 1500, ends[2]: 1500}
    result, _ = run(tmp_path, script, gaps=long_gaps)
    habits = {h.type: h for h in result.habits}
    assert habits[HabitType.LONG_PAUSE].count == 2
    assert {o.start_idx for o in habits[HabitType.LONG_PAUSE].occurrences} == {ends[0], ends[2]}
    assert HabitType.MID_PAUSE not in habits
    long_p = [p for p in result.pauses if p.habit_type == HabitType.LONG_PAUSE]
    assert all(p.expected and p.relative_length >= 2 for p in long_p)

    tight = {ends[1]: 20, ends[3]: 20}
    result, _ = run(tmp_path, script, gaps=tight)
    habits = {h.type: h for h in result.habits}
    assert habits[HabitType.NO_BREAK].count == 2
    assert {o.start_idx for o in habits[HabitType.NO_BREAK].occurrences} == {ends[1], ends[3]}

    quiet = {ends[0]: {"amp": 0.04}, ends[4]: {"amp": 0.04}}
    result, _ = run(tmp_path, script, overrides=quiet)
    habits = {h.type: h for h in result.habits}
    assert habits[HabitType.END_FADE].count == 2


def test_low_coverage_fails(tmp_path):
    script = "저는 오늘 발표를 시작하겠습니다. 이번 주제는 결제 문화입니다."
    y, truth, units = synthesize(script)
    wav = tmp_path / "a.wav"
    write_wav(wav, y)
    words = [WordTiming("전혀", 0.5, 0.9), WordTiming("다른", 1.0, 1.4)]
    with pytest.raises(AnalysisError) as e:
        analyze(wav, script, aligner=PrecomputedWordsAligner(words))
    assert e.value.code == ErrorCode.ALIGNMENT_LOW_COVERAGE


def test_error_cases(tmp_path):
    with pytest.raises(AnalysisError) as e:
        analyze(tmp_path / "none.wav", "")
    assert e.value.code == ErrorCode.EMPTY_SCRIPT
    with pytest.raises(AnalysisError) as e:
        analyze(tmp_path / "none.wav", "안녕하세요 반갑습니다")
    assert e.value.code == ErrorCode.AUDIO_NOT_FOUND
    short = tmp_path / "short.wav"
    write_wav(short, np.zeros(8000, dtype=np.float32))
    with pytest.raises(AnalysisError) as e:
        analyze(short, "안녕하세요 반갑습니다")
    assert e.value.code == ErrorCode.AUDIO_TOO_SHORT


def test_energy_aligner_finds_long_pause(tmp_path):
    script = "저는 오늘 발표를 시작하겠습니다 이번 주제는 결제 문화입니다"
    cut = script.index("발표")
    y, truth, units = synthesize(script, gaps={cut - 2: 800})
    wav = tmp_path / "e.wav"
    write_wav(wav, y)
    result = analyze(wav, script, aligner=EnergyAligner())
    long_pauses = [p for p in result.pauses if p.duration_ms >= 600]
    assert len(long_pauses) == 1
    assert abs(long_pauses[0].after_idx - (cut - 2)) <= 1


def test_whisper_aligner_glue_with_fake_model(tmp_path):
    ov, gp, _ = demo_spec()
    y, truth, units = synthesize(DEMO_SCRIPT, overrides=ov, gaps=gp)
    wav = tmp_path / "w.wav"
    write_wav(wav, y)
    words = words_from_truth(units, truth)

    class FakeModel:
        def transcribe(self, audio, **kw):
            assert kw["language"] == "ko" and kw["word_timestamps"] is True
            seg = NS(words=[NS(word=" " + w.text, start=w.start, end=w.end, probability=0.9) for w in words])
            return iter([seg]), None

    aligner = WhisperAligner()
    aligner._model = FakeModel()
    result = analyze(wav, DEMO_SCRIPT, aligner=aligner)
    assert {h.type for h in result.habits} == {HabitType.END_STRETCH, HabitType.END_RISE, HabitType.MID_PAUSE}
    assert result.syllables[0].confidence == 0.9


def test_whisper_no_words_means_no_speech(tmp_path):
    class EmptyModel:
        def transcribe(self, audio, **kw):
            return iter([]), None

    y, _, _ = synthesize("저는 오늘 발표를 시작하겠습니다")
    wav = tmp_path / "w.wav"
    write_wav(wav, y)
    aligner = WhisperAligner()
    aligner._model = EmptyModel()
    with pytest.raises(AnalysisError) as e:
        analyze(wav, "저는 오늘 발표를 시작하겠습니다", aligner=aligner)
    assert e.value.code == ErrorCode.NO_SPEECH


SCRIPT_D = "저는 오늘 발표를 시작하겠습니다. 이번 주제는 결제 문화입니다."


def run_with_insert(tmp_path, insert, words_edit=None, script=SCRIPT_D):
    k = script.index("오늘") + 1
    y, truth, units = synthesize(script, inserts={k: insert})
    wav = tmp_path / "d.wav"
    write_wav(wav, y)
    words = words_from_truth(units, truth)
    t = {x.idx: x for x in truth}
    nxt = script.index("발")
    gap_start = t[k].end_ms
    if words_edit:
        words = words_edit(words, t[nxt].start_ms - insert.get("post", 100) - insert.get("dur", 250))
    return analyze(wav, script, aligner=PrecomputedWordsAligner(words)), t, k, nxt


def test_voice_between_words_that_whisper_missed_is_stray_sound(tmp_path):
    result, t, k, nxt = run_with_insert(tmp_path, {"dur": 300})
    stray = [d for d in result.disfluencies if d.type.value == "STRAY_SOUND"]
    assert len(stray) == 1
    assert stray[0].before_idx == nxt
    assert abs(stray[0].end_ms - stray[0].start_ms - 300) < 80
    assert not any(p.after_idx == k for p in result.pauses)
    by_idx = {s.idx: s for s in result.syllables}
    assert not by_idx[k].stretch
    assert abs(by_idx[k].end_ms - t[k].end_ms) < 80


def test_breath_like_noise_in_pause_is_not_a_disfluency(tmp_path):
    result, _, k, _ = run_with_insert(tmp_path, {"dur": 300, "noise": True, "amp": 0.05})
    assert result.disfluencies == []
    assert any(p.after_idx == k for p in result.pauses)


def test_filler_word_in_whisper_output_is_reported_as_filler(tmp_path):
    def add(words, filler_start_ms):
        out = []
        for w in words:
            out.append(w)
            if w.text == "오늘":
                out.append(WordTiming("음", filler_start_ms / 1000, (filler_start_ms + 250) / 1000, 0.8))
        return out

    result, _, k, nxt = run_with_insert(tmp_path, {"dur": 250}, words_edit=add)
    assert [(d.type.value, d.before_idx, d.text) for d in result.disfluencies] == [("FILLER", nxt, "음")]
    assert not any(p.after_idx == k for p in result.pauses)


def test_repeated_word_is_reported_as_repeat(tmp_path):
    def add(words, start_ms):
        out = []
        for w in words:
            out.append(w)
            if w.text == "오늘":
                out.append(WordTiming("오늘", start_ms / 1000, (start_ms + 250) / 1000, 0.8))
        return out

    script = "저는 오늘 발표를 시작하겠습니다. 이번 주제는 결제 문화입니다."
    result, _, _, nxt = run_with_insert(tmp_path, {"dur": 250}, words_edit=add, script=script)
    found = [(d.type.value, d.text) for d in result.disfluencies]
    assert ("REPEAT", "오늘") in found


def test_extra_word_not_in_script_becomes_part_of_spoken_text(tmp_path):
    def add(words, start_ms):
        out = []
        for w in words:
            out.append(w)
            if w.text == "오늘":
                out.append(WordTiming("정말로요", start_ms / 1000, (start_ms + 250) / 1000, 0.9))
        return out

    result, _, _, nxt = run_with_insert(tmp_path, {"dur": 250}, words_edit=add)
    assert "오늘 정말로요 발표를" in result.spoken_text
    assert [d.kind.value for d in result.spoken_differences] == ["ADDED"]
    assert result.disfluencies == []


def test_clean_reading_has_no_disfluencies(demo):
    assert demo[0].disfluencies == []


def test_different_word_is_shown_as_spoken_and_analyzed(tmp_path):
    script = "저는 오늘 발표를 시작하겠습니다. 이번 주제는 결제 문화입니다."
    spoken = "저는 오늘 발표를 시작하겠습니다. 이번 주제는 신용카드 문화입니다."
    result, _ = run(tmp_path, script, spoken=spoken)
    k = script.index("결제")
    assert result.spoken_text == spoken
    assert [(d.kind.value, d.start_idx, d.end_idx, d.spoken, d.script_text) for d in result.spoken_differences] == [
        ("REPLACED", k, k + 3, "신용카드", "결제")
    ]
    by_idx = {s.idx: s for s in result.syllables}
    assert all(by_idx[i].aligned and by_idx[i].pitch_hz is not None for i in range(k, k + 4))
    assert result.unclear_ranges == []


def test_paraphrased_phrase_is_analyzed_as_spoken(tmp_path):
    script = "저는 오늘 발표를 시작하겠습니다. 먼저 배경을 설명하여 이렇게 됐습니다. 감사합니다."
    spoken = "저는 오늘 발표를 시작하겠습니다. 먼저 배경을 설명하다 보니 이렇게 될 수도 있습니다. 감사합니다."
    result, _ = run(tmp_path, script, spoken=spoken)
    assert result.spoken_text == spoken
    assert result.summary.aligned_ratio > 0.97
    assert result.unclear_ranges == []
    assert all(s.pitch_hz is not None for s in result.syllables if s.char != " ")


def test_added_sentence_is_part_of_the_analysis_not_a_disfluency(tmp_path):
    script = "저는 오늘 발표를 시작하겠습니다. 감사합니다."
    added = "사실 이 주제는 제가 오래 고민했던 내용입니다."
    spoken = "저는 오늘 발표를 시작하겠습니다. " + added + " 감사합니다."
    result, _ = run(tmp_path, script, spoken=spoken)
    assert added in result.spoken_text
    assert [d.kind.value for d in result.spoken_differences] == ["ADDED"]
    assert result.disfluencies == []
    k = result.spoken_text.index("사실")
    assert all(s.aligned for s in result.syllables if s.idx >= k and s.char != " " and s.char != ".")
    assert not any(p.duration_ms > 2000 for p in result.pauses)


def test_mostly_rewritten_speech_still_gets_analyzed_with_warning(tmp_path):
    script = "저는 오늘 결제 문화의 변화를 설명하고 앞으로의 과제를 말씀드리겠습니다. 감사합니다."
    spoken = "오늘은 돈을 내는 방식이 요즘 어떻게 달라졌는지 이야기해 보겠습니다. 감사합니다."
    result, _ = run(tmp_path, script, spoken=spoken)
    assert "오늘은 돈을 내는 방식이 요즘 어떻게 달라졌는지 이야기해 보겠습니다. 감사합니다." in result.spoken_text
    assert "SCRIPT_DEVIATION" in result.warnings
    assert result.summary.script_match_ratio < 0.7
    assert result.summary.aligned_ratio > 0.9


def test_faithful_reading_keeps_script_text_and_no_differences(demo):
    result = demo[0]
    assert result.spoken_text == DEMO_SCRIPT
    assert result.spoken_differences == []
    assert "SCRIPT_DEVIATION" not in result.warnings


def test_similar_sounding_recognition_is_not_reported_as_a_different_word(tmp_path):
    script = "저는 오늘 발표를 시작하겠습니다. 이번 주제는 결제 문화입니다."
    swap = lambda w: WordTiming("결재", w.start, w.end, w.prob) if w.text == "결제" else w
    result, _ = run(tmp_path, script, words_map=swap)
    assert result.spoken_differences == []


def test_sound_distance_examples():
    from app.speech.spoken import sound_distance

    assert sound_distance("사례", "살해") <= 0.5
    assert sound_distance("앓은", "알은") <= 0.5
    assert sound_distance("결제", "계약") > 0.5
    assert sound_distance("결제", "결제") == 0.0


def test_filler_prompt_is_passed_to_whisper():
    seen = {}

    class FakeModel:
        def transcribe(self, audio, **kw):
            seen.update(kw)
            return iter([]), None

    al = WhisperAligner(filler_prompt=True, use_script_prompt=True)
    al._model = FakeModel()
    al.transcribe_words(np.zeros(16000, dtype=np.float32), "안녕하세요")
    assert seen["initial_prompt"].startswith("음...") and seen["initial_prompt"].endswith("안녕하세요")
    al2 = WhisperAligner()
    al2._model = FakeModel()
    al2.transcribe_words(np.zeros(16000, dtype=np.float32), "안녕하세요")
    assert seen["initial_prompt"] is None


def test_short_or_low_confidence_recognition_keeps_script_text_and_is_located_by_time(tmp_path):
    script = "저는 오늘 발표를 시작하겠습니다. 이번 주제는 결제 문화입니다."
    swap = lambda w: WordTiming("계약", w.start, w.end, 0.6) if w.text == "결제" else w
    result, truth = run(tmp_path, script, words_map=swap)
    k = script.index("결제")
    assert result.spoken_text == script and result.spoken_differences == []
    by_idx = {s.idx: s for s in result.syllables}
    t = {x.idx: x for x in truth}
    for i in (k, k + 1):
        assert by_idx[i].inferred and not by_idx[i].aligned
        assert abs(by_idx[i].start_ms - t[i].start_ms) < 120
    unclear = [r for r in result.unclear_ranges if r.start_idx == k]
    assert unclear and unclear[0].reason == UnclearReason.LOW_RECOGNITION and unclear[0].confidence >= 0.5


def test_syllables_inside_a_replaced_word_are_not_reported_as_skipped(tmp_path):
    script = "저는 오늘 소리 내어 읽겠습니다. 이번 주제는 결제 문화입니다."
    y, truth, units = synthesize(script)
    wav = tmp_path / "z.wav"
    write_wav(wav, y)
    words = words_from_truth(units, truth)
    merged = []
    for w in words:
        if w.text == "내어":
            prev = merged.pop()
            merged.append(WordTiming("소리를", prev.start, w.end, 0.9))
        else:
            merged.append(w)
    result = analyze(wav, script, aligner=PrecomputedWordsAligner(merged))
    k = script.index("내어")
    t = {x.idx: x for x in truth}
    assert not any(r.reason == UnclearReason.SKIPPED for r in result.unclear_ranges)
    by_idx = {s.idx: s for s in result.syllables}
    assert by_idx[k].inferred and abs(by_idx[k].start_ms - t[k].start_ms) < 200


def _db(parts):
    """[(프레임 수, dB), ...] 를 이어 붙인 에너지 곡선. hop_ms 는 기본 10ms."""
    return np.concatenate([np.full(n, v, dtype=float) for n, v in parts])


def test_isolated_short_blip_is_treated_as_silence_but_speech_is_kept():
    cfg = DEFAULT_CONFIG
    blip = _db([(50, -70), (5, -20), (50, -70), (30, -20), (10, -70)])  # 50ms 클릭 소리 + 300ms 말소리
    out = suppress_blips(blip, cfg)
    assert out[50:55].max() < -40          # 클릭은 무음 취급
    assert (out[105:135] > -30).all()      # 말소리는 그대로


def test_longest_quiet_run_is_measured_from_energy_not_timestamps():
    cfg = DEFAULT_CONFIG
    db = _db([(30, -20), (40, -70), (50, -20), (10, -70), (30, -20)])  # 말 · 400ms 무음 · "API" 소리 · 100ms 무음 · 말
    assert _longest_quiet_ms(db, 300, 1200, cfg) == pytest.approx(400, abs=20)
    assert _longest_quiet_ms(db, 700, 1200, cfg) == 0.0                         # 소리만 든 구간은 쉼이 아니다
    assert _longest_quiet_ms(db, 700, 1300, cfg) == pytest.approx(100, abs=20)


def test_word_with_impossibly_short_timestamp_is_retimed_over_voiced_part():
    cfg = DEFAULT_CONFIG
    db = _db([(20, -70), (60, -20), (20, -70), (60, -20), (20, -70)])  # 두 덩어리의 소리
    words = [WordTiming("가나다", 0.2, 0.8, 0.9), WordTiming("라마", 1.6, 1.6, 0.9)]  # 뒤 단어는 길이 0
    out = retime_degenerate_words(words, db, cfg)
    assert out[1].end - out[1].start >= 0.1
    assert out[1].start >= out[0].end - 1e-6
    assert 1.0 <= out[1].start <= 1.3 and out[1].end == pytest.approx(1.6, abs=0.03)  # 두 번째 소리 덩어리 안에 놓인다


def test_pitch_level_marks_only_four_semitones_or_more():
    from bisect import bisect_right

    edges = DEFAULT_CONFIG.pitch_level_edges_st
    level = lambda st: bisect_right(edges, st) + 1
    marked = lambda st: level(st) in (1, 2, 6, 7)  # BE2 가 화면에 표시하는 단계
    assert level(0.0) == 4 and DEFAULT_CONFIG.pitch_mid_level == 4
    assert not marked(3.9) and not marked(-3.9) and not marked(2.5) and not marked(-2.5)
    assert marked(4.0) and marked(-4.1) and marked(5.0) and marked(-7.0)
    assert level(-7.0) == 1 and level(7.0) == 7


def test_pitch_marks_exclude_sentence_end_and_low_by_default(demo):
    result, *_ = demo
    for s in result.syllables:
        assert s.pitch_level not in (1, 2)  # 낮음 표시는 검증 전까지 끈다
    ends = {u.idx for u in parse_script(DEMO_SCRIPT) if u.is_sentence_end}
    assert all(s.pitch_level not in (6, 7) for s in result.syllables if s.idx in ends)


def test_pitch_marks_can_be_turned_back_on(tmp_path):
    ov, gp, marks = demo_spec()
    y, truth, units = synthesize(DEMO_SCRIPT, overrides=ov, gaps=gp)
    wav = tmp_path / "d.wav"
    write_wav(wav, y)
    cfg = replace(DEFAULT_CONFIG, pitch_mark_sentence_end=True, pitch_mark_low=True)
    result = analyze(wav, DEMO_SCRIPT, aligner=PrecomputedWordsAligner(words_from_truth(units, truth)), config=cfg)
    top = {s.idx: s for s in result.syllables}[marks["end2"]]
    assert top.pitch_level in (6, 7)


def test_pitch_values_have_no_octave_outliers(demo):
    # 음높이를 두 번에 나눠(평소 음높이 파악 → 그 범위로 재측정) 한 옥타브씩 튀는 오류를 막는다
    result, _, _, _ = demo
    st = [s.pitch_st for s in result.syllables if s.pitch_st is not None]
    assert st and max(abs(v) for v in st) < 8

def test_filler_sounds_joined_by_whisper_are_fillers():
    from app.speech.disfluency import is_filler_sound

    assert is_filler_sound("어음") and is_filler_sound("음어어") and is_filler_sound("흠")
    assert not is_filler_sound("네") and not is_filler_sound("어렵") and not is_filler_sound("")


def test_long_replacement_is_kept_even_if_one_trailing_word_was_misheard():
    from app.speech.spoken import build_spoken_text

    script = "걷는 동안에는 휴대폰을 잠시 내려놓고 주변을 천천히 둘러보세요."
    words = [
        WordTiming("걷는", 0.0, 0.5, 0.99),
        WordTiming("동안에는", 0.5, 1.2, 0.95),
        WordTiming("스마트폰", 1.2, 1.9, 0.9),
        WordTiming("잠깐", 1.9, 2.3, 0.9),
        WordTiming("넣어두고", 2.3, 3.0, 0.9),
        WordTiming("지분을", 3.0, 3.6, 0.45),  # 바로 뒤 '주변을' 을 잘못 들음
        WordTiming("천천히", 3.6, 4.2, 0.99),
        WordTiming("둘러보세요", 4.2, 5.0, 0.97),
    ]
    text, diffs, _ = build_spoken_text(script, parse_script(script), words, DEFAULT_CONFIG)
    assert any(d.kind.value == "REPLACED" and "스마트폰" in d.spoken for d in diffs)
