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
    min_voiced_frames: int = 3
    pitch_level_edges_st: tuple[float, ...] = (-4.0, -2.0, -0.7, 0.7, 2.0, 4.0)

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

    end_rise_st: float = 3.0
    end_rise_min_pitched: int = 4
    min_habit_count: int = 2

    @property
    def pitch_mid_level(self) -> int:
        return len(self.pitch_level_edges_st) // 2 + 1


DEFAULT_CONFIG = SpeechConfig()
