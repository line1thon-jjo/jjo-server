"""Bound multipart input before it reaches persistent storage.

Starlette's normal max_part_size limits text fields, not uploaded file bodies.
Keep limits local to this endpoint rather than changing global request handling.
"""

import logging

from starlette.datastructures import FormData
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import Request

from app.core.storage import CHUNK_BYTES, MAX_AUDIO_BYTES

logger = logging.getLogger(__name__)

# Allow multipart framing/metadata separately from the exact audio body limit.
MAX_MULTIPART_BYTES = MAX_AUDIO_BYTES + 64 * 1024
MAX_PART_HEADER_BYTES = 16 * 1024
MAX_FIELD_BYTES = 1024
MAX_FIELDS = 8


class MultipartLimitError(MultiPartException):
    def __init__(self, message, code="MULTIPART_INPUT_TOO_LARGE"):
        super().__init__(message)
        self.code = code


class RecordingMultipartParser(MultiPartParser):
    def __init__(self, headers, stream):
        super().__init__(headers, stream, max_files=1, max_fields=MAX_FIELDS)
        self.finished = False

    def on_part_begin(self):
        super().on_part_begin()
        self.file_bytes = 0
        self.header_bytes = 0

    def on_part_data(self, data, start, end):
        if self._current_part.file is not None:
            self.file_bytes += end - start
            if self.file_bytes > MAX_AUDIO_BYTES:
                raise MultipartLimitError("Audio file exceeds 20 MiB", "AUDIO_FILE_TOO_LARGE")
        elif len(self._current_part.data) + end - start > MAX_FIELD_BYTES:
            raise MultipartLimitError("Multipart field is too large")
        super().on_part_data(data, start, end)

    def count_header(self, start, end):
        self.header_bytes += end - start
        if self.header_bytes > MAX_PART_HEADER_BYTES:
            raise MultipartLimitError("Multipart headers are too large")

    def on_header_field(self, data, start, end):
        self.count_header(start, end)
        super().on_header_field(data, start, end)

    def on_header_value(self, data, start, end):
        self.count_header(start, end)
        super().on_header_value(data, start, end)

    def on_end(self):
        self.finished = True
        super().on_end()


async def parse_recording_multipart(request: Request) -> FormData:
    # Import at request time: unrelated endpoints must still start when the
    # pending multipart dependency has not yet been installed.
    from python_multipart.exceptions import FormParserError

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            length = int(content_length)
        except ValueError:
            raise MultiPartException("Invalid Content-Length") from None
        if length < 0:
            raise MultiPartException("Invalid Content-Length")
        if length > MAX_MULTIPART_BYTES:
            raise MultipartLimitError("Multipart request is too large")
    raw_content_type = request.headers.get("content-type", "")
    if len(raw_content_type) > MAX_PART_HEADER_BYTES:
        raise MultipartLimitError("Multipart Content-Type is too large")
    content_type = raw_content_type.split(";", 1)[0].strip().lower()
    if content_type != "multipart/form-data":
        raise MultiPartException("Expected multipart/form-data")

    async def bounded_stream():
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > MAX_MULTIPART_BYTES:
                raise MultipartLimitError("Multipart request is too large")
            # Also bound parser callback queues when an ASGI server delivers a
            # large chunk. No buffering of the complete body is performed here.
            for offset in range(0, len(chunk), CHUNK_BYTES):
                yield chunk[offset:offset + CHUNK_BYTES]

    parser = RecordingMultipartParser(request.headers, bounded_stream())
    try:
        form = await parser.parse()
        # python-multipart.finalize() alone does not reject a truncated body.
        if not parser.finished:
            raise MultiPartException("Incomplete multipart data")
        return form
    except BaseException as exc:
        # Cover cancellation/disconnect/I/O errors as well as malformed input,
        # including Starlette versions whose parse() catches MultiPartException
        # only. Completed and in-progress spool files are both included here.
        for file in parser._files_to_close_on_error:
            try:
                file.close()
            except BaseException:
                logger.exception("Failed to close a multipart temporary file")
        if isinstance(exc, FormParserError):
            raise MultiPartException("Invalid multipart data") from exc
        raise
