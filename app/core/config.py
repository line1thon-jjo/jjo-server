from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    google_client_ids: list[str] = []
    auth_issuer: str = "jjo"
    signup_token_secret: str = ""
    access_token_secret: str = ""
    signup_token_ttl_seconds: int = 600
    access_token_ttl_seconds: int = 3600

    database_url: str = "postgresql+psycopg://jjo:jjo@localhost:5432/jjo"

    # Gemini. 성능이 부족하면 .env의 모델 이름만 바꾸면 된다.
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash-lite"  # 연습 제목 생성(practice)
    gemini_feedback_model: str = "gemini-3.5-flash-lite"  # 분석 리포트 AI 피드백(report). flash 는 무료 하루 20회라 lite 사용
    gemini_feedback_fallback_model: str = "gemini-3.5-flash-lite"  # 서버 혼잡일 때 재시도용. 시연 때 flash 로 올리는 것 검토


settings = Settings()
