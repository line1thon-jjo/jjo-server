"""report 도메인 응답 스키마 (BE2 담당). 키는 auth·practice API와 같은 snake_case.

값이 없는 필드는 내려가지 않아야 하므로(예: 평범한 글자의 pitch) 라우터에서
response_model_exclude_none=True 로 직렬화한다. 필드 설명은 docs/REPORT_API.md 참고.
"""
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.contracts.analysis_result import HabitType
from app.domains.report.models import ReportStatus


class StartAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recording_id: int


class Failure(BaseModel):
    code: str  # 분석 실패 사유 코드. 문구는 프론트가 정한다.


class ReportStatusResponse(BaseModel):
    """리포트 상태와 화면 헤더. failure 는 FAILED 일 때만, warnings 는 COMPLETED 일 때만 채워진다."""

    recording_id: int
    status: ReportStatus
    practice_title: str
    attempt_no: int
    duration_ms: int
    failure: Failure | None = None
    warnings: list[str] = []


class AudioUrlResponse(BaseModel):
    audio_url: str  # API 주소 기준 상대 경로. 프론트가 API 주소를 앞에 붙인다.
    expires_in_seconds: int


class Mark(BaseModel):
    """모양을 바꿀 글자. pitch 와 stretch 는 둘 중 하나만 있을 수도 있다."""

    idx: int
    pitch: int | None = None  # 1, 2, 6, 7 중 하나
    stretch: float | None = None  # 본인 평균 길이의 몇 배인지


class Pace(BaseModel):
    start_idx: int
    end_idx: int
    label: Literal["FAST", "SLOW"]


class NotablePause(BaseModel):
    after_idx: int
    duration_ms: int


class Segment(BaseModel):
    start_idx: int
    end_idx: int
    start_ms: int
    end_ms: int


class Replaced(BaseModel):
    start_idx: int
    end_idx: int
    spoken: str
    script_text: str | None = None


class Added(BaseModel):
    start_idx: int
    end_idx: int
    spoken: str


class Skipped(BaseModel):
    start_idx: int
    end_idx: int


class SpeedCounts(BaseModel):
    fast: int
    normal: int
    slow: int
    measured: int  # fast + normal + slow


class HabitCount(BaseModel):
    type: HabitType
    count: int


class OverviewStats(BaseModel):
    """전체 결과 탭 중 분석 결과에서 계산되는 부분. 총평(summary)은 AI 피드백에서 온다."""

    speed: SpeedCounts
    habits: list[HabitCount]


class UtteranceAnalysis(BaseModel):
    """발화 분석 탭."""

    spoken_text: str
    marks: list[Mark]
    paces: list[Pace]
    pauses: list[NotablePause]
    segments: list[Segment]
    replaced: list[Replaced]
    added: list[Added]
    skipped: list[Skipped]
