"""Exercise multipart boundaries/lifetimes independently of persistent storage."""

import asyncio

import pytest
from starlette import formparsers
from starlette.requests import ClientDisconnect, Request

from app.core.storage import CHUNK_BYTES, MAX_AUDIO_BYTES
from app.domains.practice import router as routes
from app.domains.practice.tests.test_recordings import WAV, post, recording_api
from app.domains.practice.tests.test_api import api
from tests.test_auth_api import client
from app.domains.practice.uploads import (
    MAX_MULTIPART_BYTES, MultipartLimitError, MultiPartException, parse_recording_multipart,
)

PREFIX = (b"--test\r\nContent-Disposition: form-data; name=\"file\"; filename=\"audio.wav\""
          b"\r\nContent-Type: audio/wav\r\n\r\n")
SUFFIX = (b"\r\n--test\r\nContent-Disposition: form-data; name=\"duration_ms\"\r\n\r\n"
          b"1234\r\n--test--\r\n")


@pytest.fixture
def spools(monkeypatch):
    opened = []
    original = formparsers.SpooledTemporaryFile
    def track(*args, **kwargs):
        file = original(*args, **kwargs)
        opened.append(file)
        return file
    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", track)
    return opened


def request_from(chunks, *, length=None):
    chunks = iter(chunks)
    calls = []
    async def receive():
        calls.append(True)
        item = next(chunks, b"")
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, dict):
            return item
        return {"type": "http.request", "body": item, "more_body": bool(item)}
    headers = [(b"content-type", b"multipart/form-data; boundary=test")]
    if length is not None:
        headers.append((b"content-length", str(length).encode()))
    return Request({"type": "http", "method": "POST", "headers": headers}, receive), calls


def test_content_length_rejected_without_reading_or_opening_spools(spools):
    request, calls = request_from([PREFIX + WAV + SUFFIX], length=MAX_MULTIPART_BYTES + 1)
    with pytest.raises(MultipartLimitError):
        asyncio.run(parse_recording_multipart(request))
    assert calls == [] and spools == []


@pytest.mark.parametrize("length", [None, 1])
def test_file_limit_stops_chunked_or_false_length_body_early_and_closes_spool(spools, length):
    def chunks():
        yield PREFIX
        # Deliberately much longer than allowed: parser must stop consuming.
        for _ in range(MAX_AUDIO_BYTES // CHUNK_BYTES + 100):
            yield b"x" * CHUNK_BYTES
        yield SUFFIX
    request, calls = request_from(chunks(), length=length)
    with pytest.raises(MultipartLimitError) as error:
        asyncio.run(parse_recording_multipart(request))
    assert error.value.code in {"AUDIO_FILE_TOO_LARGE", "MULTIPART_INPUT_TOO_LARGE"}
    assert len(calls) <= MAX_AUDIO_BYTES // CHUNK_BYTES + 2
    assert spools and all(file.closed for file in spools)


@pytest.mark.parametrize("failure", [ClientDisconnect(), OSError("receive failed"), asyncio.CancelledError()])
def test_parse_interruption_closes_started_spool(spools, failure):
    request, _ = request_from([PREFIX + WAV, failure])
    with pytest.raises(type(failure)):
        asyncio.run(parse_recording_multipart(request))
    assert spools and all(file.closed for file in spools)


def test_incomplete_body_does_not_leave_unlisted_file_open(spools):
    request, _ = request_from([PREFIX + WAV])
    with pytest.raises(MultiPartException):
        asyncio.run(parse_recording_multipart(request))
    assert spools and all(file.closed for file in spools)


def test_complete_form_spool_closes_explicitly(spools):
    request, _ = request_from([PREFIX + WAV + SUFFIX])
    async def parse_and_close():
        form = await parse_recording_multipart(request)
        assert not spools[0].closed
        await form.close()
    asyncio.run(parse_and_close())
    assert all(file.closed for file in spools)


@pytest.mark.parametrize("body", [
    PREFIX + WAV + b"\r\n--test\r\nContent-Disposition: form-data; name=\"file\"; filename=\"second\"\r\n\r\nx\r\n--test--\r\n",
    PREFIX + WAV + b"\r\n--test\r\nContent-Disposition: form-data; name=\"duration_ms\"\r\n\r\n" + b"1" * 1025,
    PREFIX + WAV + b"\r\n--test\r\nContent-Disposition: " + b"x" * (16 * 1024 + 1),
])
def test_excess_parts_fields_headers_close_prior_spool(spools, body):
    request, _ = request_from([body])
    with pytest.raises(MultiPartException):
        asyncio.run(parse_recording_multipart(request))
    assert spools and all(file.closed for file in spools)


@pytest.mark.parametrize("outcome", ["success", "exact_limit", "invalid_duration", "unsupported", "too_large", "db_failure"])
def test_api_closes_spools_on_every_exit(recording_api, spools, monkeypatch, outcome):
    data = WAV
    duration = "1234"
    if outcome == "exact_limit":
        data = WAV + b"x" * (MAX_AUDIO_BYTES - len(WAV))
    elif outcome == "invalid_duration":
        duration = "0"
    elif outcome == "unsupported":
        data = b"text pretending to be audio"
    elif outcome == "too_large":
        data = WAV + b"x" * MAX_AUDIO_BYTES
        def must_not_persist(*args, **kwargs):
            pytest.fail("Oversize file reached persistent storage")
        monkeypatch.setattr(routes, "upload_recording", must_not_persist)
    elif outcome == "db_failure":
        original = routes.upload_recording
        def fail(db, **kwargs):
            from sqlalchemy import event
            from sqlalchemy.exc import SQLAlchemyError
            def before_commit(session):
                raise SQLAlchemyError("commit failed")
            event.listen(db, "before_commit", before_commit, once=True)
            return original(db, **kwargs)
        monkeypatch.setattr(routes, "upload_recording", fail)
    if outcome == "db_failure":
        from sqlalchemy.exc import SQLAlchemyError
        with pytest.raises(SQLAlchemyError):
            post(recording_api, data, duration=duration)
    else:
        response = post(recording_api, data, duration=duration)
        assert response.status_code == {
            "success": 201, "exact_limit": 201, "invalid_duration": 422, "unsupported": 415, "too_large": 413,
        }[outcome]
    assert spools and all(file.closed for file in spools)


def test_openapi_matches_manual_parser_responses_and_limits():
    from app.main import app
    operation = app.openapi()["paths"]["/practices/{practice_id}/recordings"]["post"]
    body = operation["requestBody"]
    assert body["required"] is True
    schema = body["content"]["multipart/form-data"]["schema"]
    assert set(schema["required"]) == {"file", "duration_ms"}
    assert schema["properties"]["file"]["format"] == "binary"
    assert schema["properties"]["file"]["maxLength"] == MAX_AUDIO_BYTES
    assert schema["properties"]["duration_ms"]["exclusiveMinimum"] == 0
    assert schema["properties"]["duration_ms"]["maximum"] == 2147483647
    for code in (201, 401, 404, 413, 415, 422, 500, 503):
        assert str(code) in operation["responses"]
    # FastAPI's automatic HTTPValidationError schema would misdescribe our envelope.
    assert operation["responses"]["422"]["content"]["application/json"]["schema"]["$ref"].endswith("/RecordingErrorResponse")
    assert "text/plain" in operation["responses"]["500"]["content"]
