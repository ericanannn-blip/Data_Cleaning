"""Streaming multipart parsing without Starlette's 1,000-file form limit."""

from fastapi import HTTPException
from python_multipart import MultipartParser
from python_multipart.multipart import parse_options_header

from .storage import UploadWriter


class StreamingUpload:
    def __init__(self, storage, batch_id: str, content_type: str):
        _, options = parse_options_header(content_type)
        boundary = options.get(b"boundary", b"")
        if not boundary or len(boundary) > 200:
            raise HTTPException(400, "Missing or invalid multipart boundary.")
        self.storage, self.batch_id, self.writer = storage, batch_id, None
        self.header_name, self.header_value, self.headers = b"", b"", {}
        self.header_bytes, self.count, self.ended = 0, 0, False
        self.parser = MultipartParser(boundary, {
            "on_part_begin": self.begin,
            "on_header_field": self.header_field,
            "on_header_value": self.header_data,
            "on_header_end": self.header_end,
            "on_headers_finished": self.headers_finished,
            "on_part_data": self.part_data,
            "on_part_end": self.part_end,
            "on_end": self.end,
        })

    def begin(self):
        self.headers = {}
        self.header_bytes = 0

    def bounded_header(self, data):
        self.header_bytes += len(data)
        if self.header_bytes > 8192:
            raise HTTPException(400, "Multipart part headers are too large.")

    def header_field(self, data, start, end):
        piece = data[start:end]
        self.bounded_header(piece)
        self.header_name += piece

    def header_data(self, data, start, end):
        piece = data[start:end]
        self.bounded_header(piece)
        self.header_value += piece

    def header_end(self):
        self.headers[self.header_name.lower()] = self.header_value
        self.header_name, self.header_value = b"", b""

    def headers_finished(self):
        disposition, options = parse_options_header(self.headers.get(b"content-disposition", b""))
        if disposition != b"form-data" or options.get(b"name") != b"files" or b"filename" not in options:
            raise HTTPException(422, "Use one or more files fields with filenames.")
        self.writer = UploadWriter(self.storage, options[b"filename"].decode("utf-8", "replace"), self.batch_id)

    def part_data(self, data, start, end):
        if self.writer is None:
            raise HTTPException(400, "Invalid multipart file.")
        self.writer.write(data[start:end])

    def part_end(self):
        self.writer.finish()
        self.writer = None
        self.count += 1

    def end(self):
        self.ended = True

    def write(self, data):
        self.parser.write(data)

    def finish(self):
        self.parser.finalize()
        if not self.ended:
            raise HTTPException(400, "Truncated multipart body; completed files remain in the batch.")
        if not self.count:
            raise HTTPException(422, "Select at least one file.")

    def abort(self):
        if self.writer:
            self.writer.abort()
