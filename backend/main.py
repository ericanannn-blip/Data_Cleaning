"""One FastAPI service serving the UI and bounded ingestion API."""

from contextlib import asynccontextmanager
from pathlib import Path
import hashlib
import io
import logging
import sqlite3

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from python_multipart.exceptions import MultipartParseError

from .config import Settings
from .storage import Storage
from .profiling import build_profile
from .readers import ReaderError, image_png, pdf_page, zip_member


STATIC = Path(__file__).resolve().parent / "static"


class BodyTooLarge(Exception):
    pass


class RequestLimitMiddleware:
    """Bound multipart bodies before parsing, including chunked requests."""

    def __init__(self, app, max_bytes: int):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        length = headers.get(b"content-length")
        if length is not None:
            try:
                declared = int(length)
                if declared < 0:
                    raise ValueError
            except ValueError:
                return await JSONResponse({"detail": "Invalid request size."}, 400)(scope, receive, send)
            if declared > self.max_bytes:
                return await JSONResponse({"detail": "Request exceeds the batch upload limit."}, 413)(scope, receive, send)
        consumed = 0

        async def bounded_receive():
            nonlocal consumed
            message = await receive()
            consumed += len(message.get("body", b""))
            if consumed > self.max_bytes:
                raise BodyTooLarge
            return message

        try:
            await self.app(scope, bounded_receive, send)
        except BodyTooLarge:
            await JSONResponse({"detail": "Request exceeds the batch upload limit."}, 413)(scope, receive, send)


class SecurityHeadersMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        async def secured_send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend([
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-frame-options", b"DENY"),
                    (b"cache-control", b"no-store"),
                ])
                if scope.get("path") == "/":
                    headers.append((b"content-security-policy", b"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"))
                message["headers"] = headers
            await send(message)
        await self.app(scope, receive, secured_send)


def create_app(settings: Settings | None = None):
    settings = settings or Settings.from_env()
    storage = Storage(settings)

    @asynccontextmanager
    async def lifespan(app):
        await run_in_threadpool(storage.initialize)
        yield

    app = FastAPI(title="DAT File Inspector", version="2.0.0", lifespan=lifespan)
    app.state.storage = storage
    app.add_middleware(RequestLimitMiddleware, max_bytes=settings.max_request_bytes)
    app.add_middleware(SecurityHeadersMiddleware)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.exception_handler(MultipartParseError)
    async def malformed_multipart(request, exc):
        return JSONResponse({"detail": "Invalid multipart upload. Select your files and try again."}, 400)

    @app.exception_handler(ReaderError)
    async def reader_error(request, exc):
        return JSONResponse({"detail": str(exc)}, 422)

    @app.exception_handler(OSError)
    @app.exception_handler(sqlite3.Error)
    async def unavailable_content(request, exc):
        logging.getLogger(__name__).exception("File storage unavailable")
        return JSONResponse({"detail": "File storage is temporarily unavailable. Please retry."}, 503)

    @app.get("/", include_in_schema=False)
    def frontend():
        return FileResponse(STATIC / "index.html")

    @app.get("/health")
    def health():
        with storage.connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return {"status": "ok"}

    @app.get("/config")
    def public_config():
        return {"max_file_bytes": settings.max_file_bytes, "max_request_bytes": settings.max_request_bytes,
                "max_files": settings.max_files}

    @app.get("/files")
    def list_files(limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0),
                   category: str | None = Query(None, max_length=30), q: str = Query("", max_length=255),
                   detected_type: str | None = Query(None, max_length=30)):
        return storage.list_files(limit, offset, category, q, detected_type)

    @app.get("/files/{file_id}")
    def file_detail(file_id: str):
        record = storage.get_file(file_id)
        if record is None:
            raise HTTPException(404, "File record not found.")
        return record

    def require_file(file_id):
        record = storage.get_file(file_id)
        if record is None:
            raise HTTPException(404, "File record not found.")
        return record

    def source(file_id, entry=None):
        require_file(file_id)
        path = storage.raw_path(file_id)
        if entry is not None:
            _, data = zip_member(path, entry)
            return io.BytesIO(data)
        return path

    @app.get("/files/{file_id}/profile")
    def file_profile(file_id: str, version: int | None = Query(None, ge=1)):
        require_file(file_id)
        profile = storage.get_profile(file_id, version)
        if profile is None:
            raise HTTPException(404, "Profile version not found.")
        return profile

    @app.get("/files/{file_id}/history")
    def file_history(file_id: str):
        require_file(file_id)
        return {"versions": storage.profile_history(file_id)}

    @app.post("/files/{file_id}/resample")
    def resample(file_id: str, sample_bytes: int = Query(262144, ge=1024, le=1048576),
                 sample_rows: int = Query(500, ge=1, le=2000)):
        require_file(file_id)
        return storage.resample(file_id, sample_bytes, sample_rows)

    @app.get("/files/{file_id}/image")
    def read_image(file_id: str, entry: int | None = Query(None, ge=0)):
        return Response(image_png(source(file_id, entry)), media_type="image/png")

    @app.get("/files/{file_id}/pages/{page_number}")
    def read_pdf_text(file_id: str, page_number: int, entry: int | None = Query(None, ge=0)):
        return pdf_page(source(file_id, entry), page_number)

    @app.get("/files/{file_id}/pages/{page_number}/image")
    def read_pdf_image(file_id: str, page_number: int, entry: int | None = Query(None, ge=0)):
        return Response(pdf_page(source(file_id, entry), page_number, as_image=True), media_type="image/png")

    @app.get("/files/{file_id}/archive/{entry}/profile")
    def read_archive_member(file_id: str, entry: int):
        require_file(file_id)
        name, data = zip_member(storage.raw_path(file_id), entry)
        return {"name": name, "profile": build_profile(io.BytesIO(data), name, len(data), hashlib.sha256(data).hexdigest())}

    @app.get("/schemas")
    def schemas():
        return {"schemas": storage.schemas()}

    @app.get("/schemas/{key}")
    def schema_history(key: str):
        history = storage.schema_history(key)
        if not history:
            raise HTTPException(404, "Schema not found.")
        return {"id": key, "versions": history}

    @app.post("/schemas/{key}/confirm")
    def confirm_schema(key: str, version: int = Query(..., ge=1)):
        result = storage.confirm_schema(key, version)
        if result == "missing":
            raise HTTPException(404, "Schema not found.")
        if result == "stale":
            raise HTTPException(409, "Schema has changed. Reload and review the latest version.")
        if result == "retired":
            raise HTTPException(409, "Schema family has no active evidence. Inspect the file's current schema.")
        return next(item for item in storage.schema_history(key) if item["version"] == version)

    @app.post("/files", openapi_extra={
        "requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {
            "type": "object", "required": ["files"], "properties": {
                "files": {"type": "array", "items": {"type": "string", "format": "binary"}}
            }
        }}}}
    })
    async def upload_files(request: Request):
        if request.headers.get("content-type", "").split(";", 1)[0].lower() != "multipart/form-data":
            raise HTTPException(415, "Use multipart/form-data with one or more files fields.")
        # The request middleware enforces total bytes before parser allocation;
        # parser limits also cap file count and reject extra text fields.
        async with request.form(max_files=settings.max_files, max_fields=0, max_part_size=8192) as form:
            uploads = form.getlist("files")
            if not uploads or any(not isinstance(item, UploadFile) for item in uploads) or any(key != "files" for key in form):
                raise HTTPException(422, "Select one or more files using the files field.")
            results = []
            for upload in uploads:
                results.append(await run_in_threadpool(storage.ingest, upload))
        return {"files": results, "succeeded": sum(item["id"] is not None for item in results),
                "failed": sum(item["id"] is None for item in results)}

    return app


app = create_app()
