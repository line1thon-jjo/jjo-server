import pytest

from app.core.config import settings
from app.domains.report.feedback import service as feedback_service


@pytest.fixture(autouse=True)
def disable_live_gemini(monkeypatch):
    """pytest 에서는 .env 에 키가 있어도 실제 Gemini 를 호출하지 않는다(키가 없으면 규칙 피드백)."""
    monkeypatch.setattr(settings, "gemini_api_key", "")

    def refuse(*_args, **_kwargs):
        raise AssertionError("pytest must not call live Gemini")

    monkeypatch.setattr(feedback_service, "call_gemini", refuse)
