import pytest
from fastapi.exceptions import RequestValidationError

from app.domains.auth.router import AuthError
from app.domains.practice.script_upload import MAX_SCRIPT_FILE_BYTES, extract_script


@pytest.mark.parametrize("script", ["가", "가" * 1500, " \r\n 대본\t\n "])
@pytest.mark.parametrize("bom", [b"", b"\xef\xbb\xbf"])
def test_extract_preserves_original_text(script, bom):
    assert extract_script("script.TXT", bom + script.encode()) == script


@pytest.mark.parametrize("name,content,status,code", [
    (None, b"text", 415, "SCRIPT_FILE_TYPE_UNSUPPORTED"),
    ("script.pdf", b"text", 415, "SCRIPT_FILE_TYPE_UNSUPPORTED"),
    ("script.txt.pdf", b"text", 415, "SCRIPT_FILE_TYPE_UNSUPPORTED"),
    ("script.txt", b"a" * (MAX_SCRIPT_FILE_BYTES + 1), 413, "SCRIPT_FILE_TOO_LARGE"),
    ("script.txt", b"\xff\xfe", 422, "SCRIPT_FILE_INVALID_ENCODING"),
    ("script.txt", b"\xe3\x81", 422, "SCRIPT_FILE_INVALID_ENCODING"),
    ("script.txt", b"", 422, "SCRIPT_FILE_TEXT_EMPTY"),
    ("script.txt", b"\xef\xbb\xbf", 422, "SCRIPT_FILE_TEXT_EMPTY"),
    ("script.txt", b" \r\n\t", 422, "SCRIPT_FILE_TEXT_EMPTY"),
])
def test_file_errors(name, content, status, code):
    with pytest.raises(AuthError) as error:
        extract_script(name, content)
    assert (error.value.status, error.value.code, error.value.fields) == (status, code, ["file"])


@pytest.mark.parametrize("content", [("가" * 1501).encode(), b"a" * MAX_SCRIPT_FILE_BYTES])
def test_existing_script_limit(content):
    with pytest.raises(RequestValidationError) as error:
        extract_script("script.txt", content)
    assert error.value.errors()[0]["loc"] == ("body", "script")
    assert error.value.errors()[0]["type"] == "string_too_long"


def test_post_static_route_after_get_dynamic_route():
    """Verify the read PR's method/path combination without multipart installed."""
    import asyncio
    from starlette.responses import JSONResponse
    from starlette.routing import Route, Router

    async def detail(request):
        return JSONResponse({"route": "detail"})

    async def upload(request):
        return JSONResponse({"route": "upload"})

    router = Router(routes=[
        Route("/practices/{practice_id}", detail, methods=["GET"]),
        Route("/practices/script-upload", upload, methods=["POST"]),
    ])
    messages = []

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        messages.append(message)

    asyncio.run(router({"type": "http", "path": "/practices/script-upload",
                        "method": "POST", "root_path": "", "headers": []}, receive, send))
    assert messages[0]["status"] == 200
    assert messages[1]["body"] == b'{"route":"upload"}'
