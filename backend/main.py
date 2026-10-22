"""One FastAPI service serving the UI and bounded ingestion API."""

import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from python_multipart.exceptions import MultipartParseError
from starlette.concurrency import run_in_threadpool

from .config import Settings
from .jobs import Worker
from .storage import Storage, UploadWriter
from .uploads import StreamingUpload

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
            if self.max_bytes and declared > self.max_bytes:
                return await JSONResponse({"detail": "Request exceeds the batch upload limit."}, 413)(scope, receive, send)
        consumed = 0

        async def bounded_receive():
            nonlocal consumed
            message = await receive()
            consumed += len(message.get("body", b""))
            if self.max_bytes and consumed > self.max_bytes:
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


class AuthenticationMiddleware:
    def __init__(self, app, token):
        self.app, self.token = app, token

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if self.token and scope["type"] == "http" and any(path.startswith(p) for p in ("/files", "/batches", "/schemas")):
            supplied = dict(scope.get("headers", [])).get(b"authorization", b"")
            if not secrets.compare_digest(supplied, ("Bearer " + self.token).encode("utf-8")):
                return await JSONResponse({"detail": "Authentication required."}, 401)(scope, receive, send)
        return await self.app(scope, receive, send)


def create_app(settings: Settings | None = None):
    settings = settings or Settings.from_env()
    storage = Storage(settings)
    worker = Worker(storage)

    @asynccontextmanager
    async def lifespan(app):
        await run_in_threadpool(storage.initialize)
        if settings.worker_enabled:
            worker.start()
        try:
            yield
        finally:
            if settings.worker_enabled:
                await run_in_threadpool(worker.close)

    app = FastAPI(title="Persistent Content Ingestion", version="2.0.0", lifespan=lifespan)
    app.state.storage = storage
    app.state.worker = worker
    app.add_middleware(RequestLimitMiddleware, max_bytes=settings.max_request_bytes)
    app.add_middleware(AuthenticationMiddleware, token=settings.api_token)
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
                "max_files": None, "authentication_required": bool(settings.api_token)}

    @app.get("/schemas")
    def schema_catalog(limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0)):
        with storage.connect() as connection:
            rows = connection.execute("""SELECT json_extract(a.result, '$.schema.schema_fingerprint') fingerprint,
                COUNT(f.id) uploaded_files, a.result FROM analyses a JOIN files f ON f.sha256=a.sha256
                WHERE a.policy_key=? AND json_extract(a.result, '$.schema.schema_fingerprint') IS NOT NULL
                GROUP BY fingerprint ORDER BY fingerprint LIMIT ? OFFSET ?""", (worker.policy_key, limit, offset)).fetchall()
        import json
        return {"schemas": [{"fingerprint": row["fingerprint"], "uploaded_files": row["uploaded_files"],
                             "representative_schema": json.loads(row["result"])["schema"]} for row in rows],
                "limit": limit, "offset": offset}

    @app.post("/batches")
    def create_batch():
        return {"batch_id": storage.create_batch()}

    @app.get("/batches/{batch_id}")
    def batch_results(batch_id: str, limit: int = Query(200, ge=1, le=200), after: int = Query(0, ge=0)):
        if not storage.batch_exists(batch_id):
            raise HTTPException(404, "Batch not found.")
        return storage.batch_results(batch_id, limit, after)

    @app.get("/files")
    def list_files(limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0)):
        return storage.list_files(limit, offset)

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

    @app.get("/files/{file_id}/analysis")
    def file_analysis(file_id: str):
        require_file(file_id)
        return worker.analysis(file_id)

    @app.get("/files/{file_id}/schemas")
    def file_schemas(file_id: str):
        require_file(file_id)
        detail = worker.analysis(file_id)
        return {"file_id": file_id, "job": detail["job"],
                "schema": detail["analysis"].get("schema") if detail["analysis"] else None}

    @app.post("/files/{file_id}/reprocess", status_code=202)
    def reprocess(file_id: str):
        require_file(file_id)
        worker.reprocess(file_id)
        return {"file_id": file_id, "state": "pending"}

    @app.get("/files/{file_id}/download")
    def download(file_id: str):
        record = require_file(file_id)
        path = storage.raw_path(record["id"])
        if not path.is_file():
            raise HTTPException(410, "Original bytes are missing; restore from backup.")
        return FileResponse(path, media_type="application/octet-stream", filename=record["original_name"],
                            headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(record["original_name"])})

    @app.post("/files", openapi_extra={
        "requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {
            "type": "object", "required": ["files"], "properties": {
                "files": {"type": "array", "items": {"type": "string", "format": "binary"}}
            }
        }}, "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}}
    })
    async def upload_files(request: Request, filename: str = "unnamed.dat", batch_id: str | None = None):
        content_type = request.headers.get("content-type", "")
        media_type = content_type.split(";", 1)[0].lower()
        if media_type not in {"multipart/form-data", "application/octet-stream"}:
            raise HTTPException(415, "Use multipart/form-data files fields or application/octet-stream with filename.")
        if batch_id and not await run_in_threadpool(storage.batch_exists, batch_id):
            raise HTTPException(404, "Batch not found.")
        supplied_batch = batch_id is not None
        batch_id = batch_id or await run_in_threadpool(storage.create_batch)
        uploader = None
        try:
            uploader = (StreamingUpload(storage, batch_id, content_type) if media_type == "multipart/form-data"
                        else await run_in_threadpool(UploadWriter, storage, filename, batch_id))
            async for chunk in request.stream():
                for offset in range(0, len(chunk), 65536):
                    await run_in_threadpool(uploader.write, chunk[offset:offset + 65536])
            uploaded_file = await run_in_threadpool(uploader.finish)
            if not supplied_batch:
                await run_in_threadpool(storage.batch_state, batch_id, "completed")
            response = await run_in_threadpool(storage.batch_results, batch_id)
            if media_type == "application/octet-stream":
                response["uploaded_file"] = uploaded_file
            return response
        except BaseException:
            if not supplied_batch:
                await run_in_threadpool(storage.batch_state, batch_id, "interrupted")
            raise
        finally:
            if uploader:
                await run_in_threadpool(uploader.abort)

    def openapi():
        if app.openapi_schema is None:
            schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
            if settings.api_token:
                schema.setdefault("components", {}).setdefault("securitySchemes", {})["BearerAuth"] = {
                    "type": "http", "scheme": "bearer"}
                for path, operations in schema["paths"].items():
                    if any(path.startswith(prefix) for prefix in ("/files", "/batches", "/schemas")):
                        for operation in operations.values():
                            operation["security"] = [{"BearerAuth": []}]
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = openapi

    return app


app = create_app()
