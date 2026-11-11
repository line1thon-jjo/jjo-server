"""CORS: 허용한 프론트 주소에서만 브라우저 호출이 된다."""
from fastapi.testclient import TestClient

from app.main import app

PREFLIGHT = {"Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "authorization"}


def test_allowed_frontend_can_call():
    c = TestClient(app)
    r = c.options("/health", headers={"Origin": "https://jjo-beta.vercel.app", **PREFLIGHT})
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "https://jjo-beta.vercel.app"
    assert "authorization" in r.headers["access-control-allow-headers"].lower()
    assert c.get("/health", headers={"Origin": "https://jjo-beta.vercel.app"}).headers[
        "access-control-allow-origin"] == "https://jjo-beta.vercel.app"


def test_unknown_origin_is_not_allowed():
    r = TestClient(app).options("/health", headers={"Origin": "https://evil.example", **PREFLIGHT})
    assert "access-control-allow-origin" not in r.headers
