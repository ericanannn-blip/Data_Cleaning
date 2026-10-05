"""One FastAPI service serving the UI and bounded ingestion API."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from python_multipart.exceptions import MultipartParseError

from .config import Settings
from .storage import Storage


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

    app = FastAPI(title="DAT File Inspector", version="1.0.0", lifespan=lifespan)
    app.state.storage = storage
    app.add_middleware(RequestLimitMiddleware, max_bytes=settings.max_request_bytes)
    app.add_middleware(SecurityHeadersMiddleware)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.exception_handler(MultipartParseError)
    async def malformed_multipart(request, exc):
        return JSONResponse({"detail": "Invalid multipart upload. Select your files and try again."}, 400)

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
    def list_files(limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0)):
        return storage.list_files(limit, offset)

    @app.get("/files/{file_id}")
    def file_detail(file_id: str):
        record = storage.get_file(file_id)
        if record is None:
            raise HTTPException(404, "File record not found.")
        return record

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
