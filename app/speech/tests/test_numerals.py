from app.speech.align import WordTiming
from app.speech.numerals import native_forms, normalize_numerals, sino
from app.speech.phonetic import sound_distance


def _w(t):
    return WordTiming(t, 0.0, 1.0, 0.9)


def test_sino_native_readings():
    assert sino(200) == "이백" and sino(100) == "백" and sino(3) == "삼" and sino(15000) == "만오천"
    assert "일곱" in native_forms(7) and "열" in native_forms(10) and "한" in native_forms(1)
    assert "스물하나" in native_forms(21) and native_forms(20) == ["스무"]


def test_numbers_follow_script_reading():
    script = "아침 일곱 시에 눈을 떴고 이백 쪽쯤 되는 책을 삼 주째 한 시간 열 시가 백 쪽을"
    words = [_w("7시에"), _w("200쪽쯤"), _w("3주째"), _w("1시간"), _w("10시가"), _w("100쪽을")]
    got = [w.text for w in normalize_numerals(words, script)]
    assert got == ["일곱시에", "이백쪽쯤", "삼주째", "한시간", "열시가", "백쪽을"]


def test_numbers_left_alone_when_script_has_no_such_reading():
    words = [_w("7시에")]
    assert normalize_numerals(words, "아침에 눈을 떴다")[0].text == "7시에"
    assert normalize_numerals(words, "아침 7시에 눈을 떴다")[0].text == "7시에"


def test_contractions_sound_the_same():
    for a, b in [("되어서야", "돼서야"), ("것인지는", "건지는"), ("보여 주는", "보여준"), ("그것은", "그건"), ("하여", "해")]:
        assert sound_distance(a, b) == 0.0
    assert sound_distance("사례", "살해") > 0.3


def test_snap_to_silence_fixes_cut_tail_and_leading_silence():
    import numpy as np

    from app.speech.config import DEFAULT_CONFIG as cfg
    from app.speech.features import detect_pauses, snap_to_silence
    from app.speech.text import parse_script
    from app.speech.types import Syl

    db = np.full(300, -60.0)  # 10ms 프레임: 0~1.0s 소리, 1.0~1.6s 무음, 1.6~2.6s 소리
    db[0:100] = -20.0
    db[160:260] = -20.0
    units = [u for u in parse_script("가나다라") if u.is_hangul]
    a1, a2, b1, b2 = (Syl(unit=u, aligned=True) for u in units)
    a1.start_ms, a1.end_ms, a1.group = 0, 500, 0
    a2.start_ms, a2.end_ms, a2.group = 500, 800, 0  # Whisper 가 끝소리를 800ms 에서 잘랐다 (실제는 1000ms 까지)
    b1.start_ms, b1.end_ms, b1.group = 800, 1300, 1  # 어절 시작에 무음이 섞여 있다 (실제 소리는 1600ms 부터)
    b2.start_ms, b2.end_ms, b2.group = 1300, 2600, 1
    sylls = [a1, a2, b1, b2]
    assert snap_to_silence(sylls, db, cfg) == 1
    assert b1.start_ms >= 1590
    pauses, _ = detect_pauses(sylls, cfg)
    assert len(pauses) == 1 and abs(pauses[0].duration_ms - 600) <= 30


def test_long_replacement_across_two_words_is_adopted():
    from app.speech.config import DEFAULT_CONFIG as cfg
    from app.speech.spoken import build_spoken_text
    from app.speech.text import parse_script

    script = "발표하실 수 있을 것입니다."
    words = [
        WordTiming("발표하실", 0.0, 0.6, 0.9),
        WordTiming("수", 0.6, 0.8, 0.9),
        WordTiming("있도록", 0.8, 1.2, 0.8),
        WordTiming("노력하겠습니다.", 1.2, 2.2, 0.75),
    ]
    text, diffs, _ = build_spoken_text(script, parse_script(script), words, cfg)
    assert "있도록 노력하겠습니다" in text
    assert len(diffs) == 1 and diffs[0].script_text and "것입" in diffs[0].script_text
