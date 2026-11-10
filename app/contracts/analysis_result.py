"""분석 결과 JSON 스키마. BE2 · BE3 · 프론트엔드가 함께 쓰는 계약이다.
BE3 PoC(v1.1) 기준. 필드명·구조를 바꾸는 PR은 BE2에게 알린다.
idx 는 대본 원문의 0-based 글자 인덱스(양 끝 포함), 시간은 ms, JSON 키는 camelCase 이다.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

SCHEMA_VERSION = "1.1"


class SpeedLabel(str, Enum):
    FAST = "FAST"
    NORMAL = "NORMAL"
    SLOW = "SLOW"


class HabitType(str, Enum):
    END_RISE = "END_RISE"
    END_STRETCH = "END_STRETCH"
    END_FADE = "END_FADE"
    MID_STRETCH = "MID_STRETCH"
    MID_PAUSE = "MID_PAUSE"
    LONG_PAUSE = "LONG_PAUSE"
    NO_BREAK = "NO_BREAK"


class PauseBoundary(str, Enum):
    WORD_INTERNAL = "WORD_INTERNAL"
    WORD = "WORD"
    COMMA = "COMMA"
    LINE = "LINE"
    SENTENCE = "SENTENCE"


class UnclearReason(str, Enum):
    LOW_VOLUME = "LOW_VOLUME"
    LOW_RECOGNITION = "LOW_RECOGNITION"
    SKIPPED = "SKIPPED"


class DisfluencyType(str, Enum):
    FILLER = "FILLER"
    REPEAT = "REPEAT"
    INSERTION = "INSERTION"
    STRAY_SOUND = "STRAY_SOUND"


class ErrorCode(str, Enum):
    EMPTY_SCRIPT = "EMPTY_SCRIPT"
    AUDIO_NOT_FOUND = "AUDIO_NOT_FOUND"
    AUDIO_DECODE_FAILED = "AUDIO_DECODE_FAILED"
    AUDIO_TOO_SHORT = "AUDIO_TOO_SHORT"
    AUDIO_TOO_LONG = "AUDIO_TOO_LONG"
    NO_SPEECH = "NO_SPEECH"
    ALIGNMENT_LOW_COVERAGE = "ALIGNMENT_LOW_COVERAGE"
    ENGINE_UNAVAILABLE = "ENGINE_UNAVAILABLE"
    INTERNAL = "INTERNAL"


class AnalysisError(Exception):
    def __init__(self, code: ErrorCode, message: str):
        super().__init__(f"[{code.value}] {message}")
        self.code = code
        self.message = message


class _CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class IdxRange(_CamelModel):
    start_idx: int
    end_idx: int


class UnclearRange(_CamelModel):
    start_idx: int
    end_idx: int
    reason: UnclearReason
    confidence: float = Field(ge=0.0, le=1.0)


class Syllable(_CamelModel):
    idx: int
    char: str
    start_ms: int
    end_ms: int
    pitch_hz: Optional[float] = None
    pitch_st: Optional[float] = None
    pitch_level: int = Field(4, ge=1, le=7)
    volume_db: Optional[float] = None
    low_volume: bool = False
    stretch: bool = False
    stretch_ratio: float = 1.0
    aligned: bool = True
    inferred: bool = False
    confidence: Optional[float] = None
    is_habit: bool = False
    habit_types: list[HabitType] = Field(default_factory=list)


class Pause(_CamelModel):
    after_idx: int
    duration_ms: int
    boundary: PauseBoundary
    expected: bool = False
    relative_length: float = 1.0
    is_habit: bool = False
    habit_type: Optional[HabitType] = None


class Disfluency(_CamelModel):
    type: DisfluencyType
    before_idx: int
    start_ms: int
    end_ms: int
    text: Optional[str] = None


class SpokenKind(str, Enum):
    REPLACED = "REPLACED"
    ADDED = "ADDED"


class SpokenDifference(_CamelModel):
    kind: SpokenKind
    start_idx: int
    end_idx: int
    spoken: str
    script_text: Optional[str] = None


class Phrase(_CamelModel):
    start_idx: int
    end_idx: int
    speed_syll_per_sec: float
    speed_ratio: float
    speed_label: SpeedLabel


class Habit(_CamelModel):
    type: HabitType
    count: int
    occurrences: list[IdxRange]
    first_idx: int


class Summary(_CamelModel):
    avg_syllables_per_sec: float
    articulation_syllables_per_sec: float
    median_pitch_hz: Optional[float] = None
    typical_pause_ms: Optional[int] = None
    aligned_ratio: float
    script_match_ratio: Optional[float] = None


class AnalysisResult(_CamelModel):
    version: str = SCHEMA_VERSION
    duration_ms: int
    spoken_text: str = ""
    syllables: list[Syllable]
    pauses: list[Pause]
    phrases: list[Phrase]
    habits: list[Habit]
    mismatched_ranges: list[IdxRange] = Field(default_factory=list)
    unclear_ranges: list[UnclearRange] = Field(default_factory=list)
    disfluencies: list[Disfluency] = Field(default_factory=list)
    spoken_differences: list[SpokenDifference] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    summary: Summary

    def to_json_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)

    def to_json(self, **kwargs: Any) -> str:
        return self.model_dump_json(by_alias=True, **kwargs)
