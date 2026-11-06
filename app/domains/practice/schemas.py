from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PracticeCreate(BaseModel):
    """연습 생성 입력. 사용자 ID는 요청 본문 대신 인증 정보에서 받는다."""

    model_config = ConfigDict(str_strip_whitespace=False, extra="forbid")

    script: str = Field(min_length=1, max_length=1500)

    @field_validator("script")
    @classmethod
    def validate_script(cls, script: str) -> str:
        if not script.strip():
            raise ValueError("대본에는 공백이 아닌 글자가 최소 1자 있어야 합니다.")
        # strip()은 검사에만 사용한다. 분석의 글자 위치 기준인 원문을 반환한다.
        return script


class PracticeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    script: str
    created_at: datetime
    updated_at: datetime


class RecordingResponse(BaseModel):
    recording_id: int
    practice_id: int
    attempt_no: int
    duration_ms: int
    created_at: datetime


class RecordingMetadata(BaseModel):
    # 기존 PostgreSQL INTEGER 컬럼의 범위. 실제 음원 길이 검증이 아니다.
    duration_ms: int = Field(gt=0, le=2147483647)


class RecordingErrorDetail(BaseModel):
    code: str
    message: str
    fields: list[str]


class RecordingErrorResponse(BaseModel):
    error: RecordingErrorDetail


class RecordingAnalysisInput(BaseModel):
    """BE2 계약 초안. audio_path는 내부 경로, duration_ms는 클라이언트 값.

    길이와 헤더 검사는 실제 디코딩이나 분석 구간의 유효성을 보장하지 않는다.
    """

    model_config = ConfigDict(frozen=True)

    recording_id: int
    practice_id: int
    attempt_no: int
    script_snapshot: str
    audio_path: str
    duration_ms: int
