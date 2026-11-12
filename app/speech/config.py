"""분석 임계값 모음. 실제 녹음으로 튜닝할 값은 전부 이 파일에서 바꾼다."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SpeechConfig:
    sample_rate: int = 16000
    hop_ms: int = 10
    min_audio_ms: int = 1500
    max_audio_s: int = 600

    min_align_coverage: float = 0.5
    warn_align_coverage: float = 0.85
    anchor_min_chars: int = 2
    refine_by_energy: bool = True
    refine_lambda: float = 0.3
    edge_extend_ms: int = 150
    edge_trim_db: float = 25.0
    min_syllable_ms: int = 60

    min_word_ms_per_char: int = 40
    blip_ms: int = 90
    blip_gap_ms: int = 150
    gap_merge_ms: int = 120
    gap_min_segment_ms: int = 60
    gap_min_fill_ratio: float = 0.4
    default_syllable_ms: int = 180
    hint_pad_ms: int = 80

    pitch_floor_hz: float = 60.0
    pitch_ceiling_hz: float = 500.0
    pitch_floor_ratio: float = 0.75  # 2차 음높이 추적 범위: 아래쪽 = 평소(25%) 음높이의 이 배수
    pitch_ceiling_ratio: float = 2.5  # 위쪽 = 평소(75%) 음높이의 이 배수
    min_voiced_frames: int = 3
    # 1~7단계 경계(반음). 화면(BE2)은 1·2·6·7단계, 즉 본인 평균에서 ±4반음 이상 벗어난 글자만 표시한다.
    # 2반음은 평소 억양의 흔들림이라 글자의 절반 가까이가 표시되어, 4반음(약 14%)으로 정했다.
    pitch_level_edges_st: tuple[float, ...] = (-6.0, -4.0, -1.0, 1.0, 4.0, 6.0)
    # 문장 끝 글자는 억양이 자연스럽게 내려가거나 올라가서 '낮음/높음' 표시 대상에서 뺀다(측정값 pitchSt 는 그대로 둔다).
    pitch_mark_sentence_end: bool = False
    # 문장 중간의 '낮음' 표시는 실제 청취와 맞는 비율이 낮아(1/6) 검증 전까지 끈다. 높음 표시는 유지한다.
    pitch_mark_low: bool = False

    low_volume_db: float = 14.0
    low_volume_is_unclear: bool = False  # 작은 소리만으로는 '불분명'으로 보지 않는다 (말끝 작아짐 습관과 syllable.lowVolume 으로 따로 본다)
    low_recognition_min_conf: float = 0.65  # 인식 낮음(모델이 다른 단어로 적음)은 확신도가 이 값 이상일 때만 불분명으로 낸다
    matched_low_prob_weight: float = 0.5

    stretch_ratio: float = 2.2
    end_stretch_ratio: float = 2.6
    stretch_min_extra_ms: int = 120
    batchim_weight: float = 0.2

    pause_min_ms: int = 200
    habit_pause_ms: int = 400
    long_pause_ms: int = 700
    long_pause_relative: float = 2.0
    typical_pause_default_ms: int = 500
    no_break_ms: int = 120

    phrase_split_pause_ms: int = 300
    min_phrase_syllables: int = 4
    fast_ratio: float = 1.4
    slow_ratio: float = 0.7
    phrase_min_aligned_ratio: float = 0.8
    plausible_rate: tuple[float, float] = (1.5, 11.0)

    low_prob: float = 0.4
    min_unclear_run: int = 2

    stray_min_ms: int = 150
    stray_margin_ms: int = 30
    stray_below_peak_db: float = 25.0
    stray_voiced_ratio: float = 0.5
    stray_loud_below_peak_db: float = 12.0
    deviation_long_syllables: int = 5
    deviation_long_prob: float = 0.7
    detach_gap_ms: int = 120
    keep_char_ratio: float = 0.8  # 떨어진 소리를 잘라낼 때 남는 소리가 (어절 글자 수 × 전형적 글자 길이)의 이 비율 이상이어야 한다

    substitute_min_distance: float = 0.5
    script_match_warn: float = 0.7
    deviation_min_syllables: int = 3
    deviation_min_prob: float = 0.8
    deviation_min_word_prob: float = 0.5
    # 길게(5음절 이상) 다른 말로 인식된 구간은 한 단어가 잘못 들려도(예: 바로 뒤 단어를 엉뚱하게 인식) 통째로 버리지 않는다
    deviation_long_min_word_prob: float = 0.4

    # 한국어 CTC 보조 모델로 반복(더듬기)을 찾는 기능. 기본 켜짐. 끄려면 환경변수 SPEECH_CTC_REPEAT=0 (torch/transformers 가 없으면 자동으로 건너뜀).
    # Whisper 는 같은 말을 두 번 하면 한 번으로 줄여 적어 반복을 놓치는데, CTC 는 들린 소리를 그대로 적어 반복이 남는다.
    # 12개 녹음에서 거리 0.25~0.34 는 실제 반복 1곳만 잡고 오탐 0, 0.45 부터 오탐이 생겨 0.34 로 정했다.
    ctc_repeat: bool = True
    ctc_model: str = "kresnik/wav2vec2-large-xlsr-korean"
    ctc_repeat_distance: float = 0.34
    ctc_repeat_min_syllables: int = 3
    ctc_repeat_max_syllables: int = 7
    ctc_repeat_max_gap_s: float = 1.6
    ctc_repeat_pad_ms: int = 100

    end_rise_st: float = 3.0
    end_rise_min_pitched: int = 4
    min_habit_count: int = 2

    @property
    def pitch_mid_level(self) -> int:
        return len(self.pitch_level_edges_st) // 2 + 1


DEFAULT_CONFIG = SpeechConfig()
