"""Multipart HTTP tests against the actual application and route integration."""

import asyncio
from io import BytesIO
from unittest.mock import Mock

import pytest

from fastapi import FastAPI, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from app.main import app
from app.domains.practice import script_upload_router as upload, title
from app.domains.practice.tests.test_api import api, count
from tests.test_auth_api import client


@pytest.fixture
def upload_api(api):
    # The shared fixture uses app.main.app with real authentication credentials.
    return api


def test_static_post_after_dynamic_get(api):
    _, _, headers, _ = api
    candidate = FastAPI()
    candidate.dependency_overrides.update(app.dependency_overrides)
    # Exercise the adverse registration order from the pending read API:
    # GET dynamic route first, POST static route afterward.
    @candidate.get("/practices/{practice_id}")
    def detail(practice_id: int):
        return {"id": practice_id}

    candidate.include_router(upload.router, prefix="/practices")
    with TestClient(candidate) as c:
        response = c.post("/practices/script-upload", headers=headers,
                          files={"file": ("script.txt", b"text")})
        assert response.status_code == 200, response.text
        assert response.json() == {"script": "text"}
        assert c.get("/practices/123", headers=headers).json() == {"id": 123}


def test_actual_app_openapi_registration():
    operation = app.openapi()["paths"]["/practices/script-upload"]["post"]
    assert "multipart/form-data" in operation["requestBody"]["content"]
    assert "200" in operation["responses"]


@pytest.mark.parametrize("bom", [b"", b"\xef\xbb\xbf"])
@pytest.mark.parametrize("script", ["가", "가" * 1500, " \r\n 발표 대본\t\n "])
def test_upload_returns_only_text_without_generation_or_persistence(upload_api, monkeypatch, bom, script):
    c, engine, headers, _ = upload_api
    generate = Mock(side_effect=AssertionError("upload must not generate a title"))
    monkeypatch.setattr(title, "generate_title", generate)
    monkeypatch.setattr("app.domains.practice.service.generate_title", generate)
    response = c.post("/practices/script-upload", headers=headers,
                      files={"file": ("script.txt", bom + script.encode(), "text/plain")})
    assert response.status_code == 200, response.text
    assert response.json() == {"script": script}
    generate.assert_not_called()
    assert count(engine) == 0


@pytest.mark.parametrize("token", [None, "invalid"])
def test_upload_requires_authentication(upload_api, token):
    c, engine, _, _ = upload_api
    headers = {} if token is None else {"Authorization": "Bearer " + token}
    response = c.post("/practices/script-upload", headers=headers,
                      files={"file": ("script.txt", b"text")})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "ACCESS_CREDENTIAL_INVALID"
    assert count(engine) == 0


@pytest.mark.parametrize("name,content,status,code,field", [
    ("script.pdf", b"text", 415, "SCRIPT_FILE_TYPE_UNSUPPORTED", "file"),
    ("script.txt", b"x" * (1024 * 1024 + 1), 413, "SCRIPT_FILE_TOO_LARGE", "file"),
    ("script.txt", b"\xff", 422, "SCRIPT_FILE_INVALID_ENCODING", "file"),
    ("script.txt", b"", 422, "SCRIPT_FILE_TEXT_EMPTY", "file"),
    ("script.txt", b"\xef\xbb\xbf", 422, "SCRIPT_FILE_TEXT_EMPTY", "file"),
    ("script.txt", b" \r\n", 422, "SCRIPT_FILE_TEXT_EMPTY", "file"),
    ("script.txt", ("가" * 1501).encode(), 422, "VALIDATION_ERROR", "script"),
])
def test_upload_errors_do_not_write(upload_api, name, content, status, code, field):
    c, engine, headers, _ = upload_api
    response = c.post("/practices/script-upload", headers=headers,
                      files={"file": (name, content)})
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code
    assert response.json()["error"]["fields"] == [field]
    assert count(engine) == 0


def test_missing_file(upload_api):
    c, engine, headers, _ = upload_api
    response = c.post("/practices/script-upload", headers=headers, data={})
    assert response.status_code == 422
    assert response.json()["error"]["fields"] == ["file"]
    assert count(engine) == 0


@pytest.mark.parametrize("content", [b"text", b"\xff", b"x" * 1501,
                                     b"x" * (1024 * 1024 + 1)])
def test_file_closed_on_success_and_failure(content):
    file = UploadFile(filename="script.txt", file=BytesIO(content), size=len(content))
    try:
        asyncio.run(upload.upload_script(file=file, user=Mock()))
    except (upload.AuthError, RequestValidationError):
        pass
    assert file.file.closed


@pytest.mark.parametrize("known_size", [None, 1024 * 1024 + 1])
def test_oversized_file_read_is_bounded(known_size):
    class TrackedFile(BytesIO):
        def __init__(self):
            super().__init__(b"x" * (1024 * 1024 + 2))
            self.read_sizes = []

        def read(self, size=-1):
            self.read_sizes.append(size)
            assert 0 <= size <= 1024 * 1024 + 1
            return super().read(size)

    stream = TrackedFile()
    file = UploadFile(filename="script.txt", file=stream, size=known_size)
    with pytest.raises(upload.AuthError) as error:
        asyncio.run(upload.upload_script(file=file, user=Mock()))
    assert error.value.status == 413
    assert stream.read_sizes == ([] if known_size is not None else [1024 * 1024 + 1])
    assert stream.closed
