import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.domains.auth.router import router as auth_router
from app.domains.practice.router import router as practice_router
from app.domains.report.router import router as report_router

logger = logging.getLogger(__name__)


def _warm_up_speech() -> None:
    """음성 분석 모델을 미리 올린다. 실패해도 서버는 뜨고, 첫 분석 때 다시 시도한다."""
    try:
        from app.speech.analyze import warm_up  # 무거운 의존성이라 필요할 때 불러온다

        warm_up()
        logger.info("Speech model is ready")
    except Exception:
        logger.exception("Speech model warm-up failed; it will be loaded on the first analysis")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    if settings.speech_warm_up:
        # 모델 로딩(첫 실행은 내려받기)을 기다리지 않고 서버를 띄운다. 그동안 들어온 분석은 로딩이 끝나길 기다린다.
        threading.Thread(target=_warm_up_speech, name="speech-warm-up", daemon=True).start()
    yield


API_DESCRIPTION = """
발표 대본을 녹음하면 말하기 습관(쪼)을 분석해 대본 위 글자 모양으로 보여주는 AI 발표 코칭 서비스 jjo 의 API.

- **로그인:** 오른쪽 위 **Authorize** 에 access_token 을 넣으면 자물쇠가 있는 API 를 직접 호출해 볼 수 있다.
- **오류 형식:** `{"error": {"code": "...", "message": "...", "fields": [...]}}`. 화면 문구는 `message` 가 아니라 `code` 로 정한다.
- **분석 리포트(report):** 필드 의미와 화면 흐름은 레포의 `docs/REPORT_API.md` 를 함께 본다.
"""

app = FastAPI(
    title="jjo API",
    version="1.0.0",
    description=API_DESCRIPTION,
    lifespan=lifespan,
    openapi_tags=[
        {"name": "auth", "description": "구글 로그인, 약관 동의, 온보딩"},
        {"name": "practice", "description": "연습(대본) 생성·조회, 녹음 업로드"},
        {"name": "report", "description": "분석 시작·상태, 결과 탭(전체 결과·발화 분석·AI 피드백), 녹음 재생"},
    ],
)

# 로그인은 쿠키가 아니라 Authorization 헤더라서 credentials 는 쓰지 않는다.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["Authorization", "Content-Type", "Range"],
    expose_headers=["Content-Range", "Accept-Ranges", "Content-Length"],
)

app.include_router(auth_router)
app.include_router(practice_router)
app.include_router(report_router)


@app.get("/health", tags=["health"])
def health() -> dict[str, str]:
    return {"status": "ok"}
