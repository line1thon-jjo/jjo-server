"""한국어 CTC 보조 모델로 반복(더듬기)을 찾는 선택 기능 테스트. 실제 모델 없이 가짜 받아쓰기 결과로 확인한다."""
from dataclasses import replace

import pytest

from app.speech import ctc
from app.speech.align import PrecomputedWordsAligner
from app.speech.analyze import analyze
from app.speech.config import DEFAULT_CONFIG

from app.speech.tests.synth import synthesize, words_from_truth, write_wav

SCRIPT = "저는 오늘 발표를 시작하겠습니다. 이번 주제는 결제 문화입니다."


def spaced(text: str, start: float, step: float = 0.2) -> list[tuple[float, str]]:
    return [(start + i * step, c) for i, c in enumerate(text)]


def test_find_repeats_detects_phrase_said_twice_with_noise():
    # 실제 test3 21번 녹음의 받아쓰기: '마음에 드는 길을' 을 두 번 읽음
    toks = spaced("일고마음이드는", 103.5) + spaced("일을마음이되는", 105.0) + spaced("길를한정을", 106.6)
    found = ctc.find_repeats(toks, "그리고 마음에 드는 길을 하나 정해 두면", DEFAULT_CONFIG)
    assert len(found) == 1
    start, end, text = found[0]
    assert 105.0 <= start <= 105.3 and end > start


def test_clean_reading_is_not_a_repeat():
    script = "저는 매일 저녁 집 앞 공원을 한 바퀴 걷습니다. 걷다 보면 하루 동안 쌓인 생각이 조금씩 정리됩니다."
    toks = spaced("".join(c for c in script if "가" <= c <= "힣"), 1.0, 0.18)
    assert ctc.find_repeats(toks, script, DEFAULT_CONFIG) == []


def test_slightly_misheard_clean_reading_is_not_a_repeat():
    script = "안녕하세요 오늘은 제가 요즘 즐겨 하는 산책에 대해 이야기해 보려고 합니다"
    heard = "아니스요오늘은제하제이와한체대이야기불겁니져는"  # 실제 CTC 가 잘못 받아쓴 결과(반복 아님)
    assert ctc.find_repeats(spaced(heard, 1.0, 0.2), script, DEFAULT_CONFIG) == []


def test_phrase_repeated_in_the_script_itself_is_not_a_stutter():
    script = "정말 좋아요 정말 좋아요 그리고 또 갑니다"
    toks = spaced("정말좋아요정말좋아요그리고또갑니다", 0.5, 0.2)
    assert ctc.find_repeats(toks, script, DEFAULT_CONFIG) == []


def test_repeats_too_far_apart_are_ignored():
    toks = spaced("시작해요", 1.0) + spaced("시작해요", 6.0)
    assert ctc.find_repeats(toks, SCRIPT, DEFAULT_CONFIG) == []


def test_enabled_by_default_in_config(monkeypatch):
    monkeypatch.delenv("SPEECH_CTC_REPEAT", raising=False)
    assert DEFAULT_CONFIG.ctc_repeat is True
    assert ctc.enabled(DEFAULT_CONFIG)


def test_when_turned_off_the_model_is_never_loaded(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEECH_CTC_REPEAT", "0")

    def boom(cfg):
        raise AssertionError("꺼져 있으면 모델을 불러오면 안 된다")

    monkeypatch.setattr(ctc, "get_transcriber", boom)
    result, *_ = _run(tmp_path, DEFAULT_CONFIG)
    assert not any(d.type.value == "REPEAT" for d in result.disfluencies)


@pytest.fixture(autouse=True)
def _ctc_on(monkeypatch):
    # tests/conftest.py 가 다른 테스트에서는 꺼 두므로, 이 파일에서는 켠 상태로 시작한다
    monkeypatch.setenv("SPEECH_CTC_REPEAT", "1")


class FakeTranscriber:
    def __init__(self, tokens):
        self._tokens = tokens

    def tokens(self, y, sample_rate):
        return self._tokens


def _run(tmp_path, cfg, tokens_for=None, monkeypatch=None):
    k = SCRIPT.index("오늘") + 1
    y, truth, units = synthesize(SCRIPT, inserts={k: {"dur": 300}})
    wav = tmp_path / "d.wav"
    write_wav(wav, y)
    words = words_from_truth(units, truth)
    t = {x.idx: x for x in truth}
    nxt = SCRIPT.index("발")
    result = analyze(wav, SCRIPT, aligner=PrecomputedWordsAligner(words), config=cfg)
    return result, t, nxt


def test_repeat_found_by_ctc_is_reported_before_the_next_syllable(tmp_path, monkeypatch):
    k = SCRIPT.index("오늘") + 1
    y, truth, units = synthesize(SCRIPT, inserts={k: {"dur": 300}})
    t = {x.idx: x for x in truth}
    nxt = SCRIPT.index("발")
    end = t[nxt].start_ms / 1000 - 0.05
    tokens = spaced("시작해요", end - 0.8 - 0.1, 0.1) + spaced("시작해요", end - 0.3, 0.1)
    monkeypatch.setattr(ctc, "get_transcriber", lambda cfg: FakeTranscriber(tokens))
    result, _, nxt = _run(tmp_path, replace(DEFAULT_CONFIG, ctc_repeat=True))
    reps = [d for d in result.disfluencies if d.type.value == "REPEAT"]
    assert len(reps) == 1
    assert reps[0].before_idx == nxt
    assert reps[0].end_ms > reps[0].start_ms


def test_env_variable_overrides_config(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEECH_CTC_REPEAT", "0")
    assert not ctc.enabled(DEFAULT_CONFIG)
    assert not ctc.enabled(replace(DEFAULT_CONFIG, ctc_repeat=True))
    monkeypatch.setenv("SPEECH_CTC_REPEAT", "1")
    assert ctc.enabled(replace(DEFAULT_CONFIG, ctc_repeat=False))
    monkeypatch.delenv("SPEECH_CTC_REPEAT")
    assert ctc.enabled(DEFAULT_CONFIG)
    assert not ctc.enabled(replace(DEFAULT_CONFIG, ctc_repeat=False))


def test_unavailable_model_leaves_analysis_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(ctc, "get_transcriber", lambda cfg: None)
    on, *_ = _run(tmp_path, replace(DEFAULT_CONFIG, ctc_repeat=True))
    off, *_ = _run(tmp_path, DEFAULT_CONFIG)
    assert on.disfluencies == off.disfluencies


def test_failure_inside_the_model_does_not_break_analysis(tmp_path, monkeypatch):
    class Broken:
        def tokens(self, y, sample_rate):
            raise RuntimeError("모델 오류")

    monkeypatch.setattr(ctc, "get_transcriber", lambda cfg: Broken())
    on, *_ = _run(tmp_path, replace(DEFAULT_CONFIG, ctc_repeat=True))
    off, *_ = _run(tmp_path, DEFAULT_CONFIG)
    assert on.disfluencies == off.disfluencies


def test_repeat_already_found_from_whisper_words_is_not_duplicated(monkeypatch):
    from types import SimpleNamespace as NS

    import numpy as np

    from app.contracts.analysis_result import Disfluency, DisfluencyType

    tokens = spaced("시작해요", 1.0, 0.1) + spaced("시작해요", 1.6, 0.1)
    monkeypatch.setattr(ctc, "get_transcriber", lambda cfg: FakeTranscriber(tokens))
    cfg = replace(DEFAULT_CONFIG, ctc_repeat=True)
    sylls = [NS(unit=NS(is_hangul=True, idx=7), aligned=True, start_ms=3000)]
    y = np.zeros(16000, dtype=np.float32)
    assert len(ctc.ctc_repeat_disfluencies(y, SCRIPT, sylls, [], cfg)) == 1
    existing = [Disfluency(type=DisfluencyType.REPEAT, before_idx=7, start_ms=1500, end_ms=2100, text="시작")]
    assert ctc.ctc_repeat_disfluencies(y, SCRIPT, sylls, existing, cfg) == []
