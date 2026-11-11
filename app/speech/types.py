"""speech 내부에서만 쓰는 작업용 자료형(Syl). 외부 계약은 contracts/analysis_result.py 에 있다."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .text import Unit


@dataclass
class Syl:
    unit: Unit
    start_ms: float = 0.0
    end_ms: float = 0.0
    aligned: bool = False
    inferred: bool = False
    group: Optional[int] = None
    confidence: Optional[float] = None
    pitch_hz: Optional[float] = None
    pitch_st: Optional[float] = None
    pitch_level: int = 4
    volume_db: Optional[float] = None
    low_volume: bool = False
    stretch: bool = False
    stretch_ratio: float = 1.0
    habit_types: list[str] = field(default_factory=list)
    hint: Optional[tuple[float, float]] = None
    mismatch: Optional[tuple[float, float]] = None
    snap_start: bool = False
    snap_end: bool = False
    sil_start: Optional[float] = None

    @property
    def dur_ms(self) -> float:
        return self.end_ms - self.start_ms

    @property
    def timed(self) -> bool:
        return self.aligned or self.inferred
