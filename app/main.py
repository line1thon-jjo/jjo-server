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


app = FastAPI(title="jjo", lifespan=lifespan)

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
