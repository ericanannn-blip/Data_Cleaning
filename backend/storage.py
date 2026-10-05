"""SQLite metadata and private UUID-named raw files."""

from datetime import datetime, timezone
from contextlib import contextmanager
import hashlib
import logging
import os
from pathlib import Path
import sqlite3
from threading import Lock
import unicodedata
from uuid import uuid4
from uuid import UUID

from .config import Settings
from .inspection import inspect_file


logger = logging.getLogger(__name__)
FIELDS = ("id", "original_name", "extension", "file_size", "mime_type", "encoding",
          "magic_bytes", "printable_ratio", "sample_bytes", "detected_type", "confidence",
          "status", "created_at", "error_message", "sha256")


def display_name(name: str | None) -> str:
    # This name is display metadata only, never a path component.
    name = (name or "unnamed").replace("\\", "/").rsplit("/", 1)[-1]
    return "".join(c for c in name if not unicodedata.category(c).startswith("C"))[:255] or "unnamed"


class Storage:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.write_lock = Lock()

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.settings.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self):
        self.settings.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings.upload_dir.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS files (
                    id TEXT PRIMARY KEY,
                    original_name TEXT NOT NULL,
                    extension TEXT NOT NULL,
                    file_size INTEGER NOT NULL,
                    mime_type TEXT NOT NULL,
                    encoding TEXT,
                    magic_bytes TEXT NOT NULL,
                    printable_ratio REAL NOT NULL,
                    sample_bytes INTEGER NOT NULL,
                    detected_type TEXT NOT NULL,
                    confidence TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    error_message TEXT,
                    sha256 TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS files_created_at ON files(created_at DESC, id DESC);
            """)
        # A killed process may leave an uncommitted write; committed raw files
        # are never touched. Initialization runs before the single worker starts.
        for temporary in self.settings.upload_dir.glob("*.part"):
            temporary.unlink(missing_ok=True)
        with self.connect() as connection:
            committed_ids = {row[0] for row in connection.execute("SELECT id FROM files")}
        # A crash between the atomic rename and SQLite commit can leave a raw
        # orphan. Remove only our generated names, never unrelated operator files.
        for raw in self.settings.upload_dir.glob("*.dat"):
            try:
                identifier = str(UUID(raw.stem))
            except ValueError:
                continue
            if identifier == raw.stem and identifier not in committed_ids:
                raw.unlink()

    def list_files(self, limit: int, offset: int) -> dict:
        with self.connect() as connection:
            records = connection.execute(
                "SELECT * FROM files ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()
            totals = connection.execute("""
                SELECT COUNT(*) AS total,
                       COALESCE(SUM(confidence = 'signature'), 0) AS signatures,
                       COALESCE(SUM(confidence != 'signature'), 0) AS review
                FROM files
            """).fetchone()
        return {"files": [dict(row) for row in records], "total": totals["total"],
                "summary": dict(totals), "limit": limit, "offset": offset}

    def get_file(self, file_id: str):
        with self.connect() as connection:
            record = connection.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
        return dict(record) if record else None

    def ingest(self, upload) -> dict:
        name = display_name(upload.filename)
        rejected = {"id": None, "original_name": name, "status": "error"}
        if Path(name).suffix.lower() != ".dat":
            return {**rejected, "error_message": "Only .dat files are accepted."}
        # Serialize quota checking and writes within the single application
        # instance, so concurrent batches cannot bypass the shared quota.
        with self.write_lock:
            file_id = str(uuid4())
            temporary = self.settings.upload_dir / f"{file_id}.part"
            target = self.settings.upload_dir / f"{file_id}.dat"
            size = 0
            digest = hashlib.sha256()
            try:
                with self.connect() as connection:
                    used = connection.execute("SELECT COALESCE(SUM(file_size), 0) FROM files").fetchone()[0]
                with temporary.open("xb") as output:
                    while chunk := upload.file.read(64 * 1024):
                        size += len(chunk)
                        if size > self.settings.max_file_bytes:
                            return {**rejected, "error_message": "File exceeds the per-file upload limit."}
                        if used + size > self.settings.max_storage_bytes:
                            return {**rejected, "error_message": "Demo storage is full. Contact the demo operator."}
                        digest.update(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                metadata = inspect_file(temporary, self.settings.inspection_bytes)
                record = {"id": file_id, "original_name": name, "extension": ".dat", **metadata,
                          "created_at": datetime.now(timezone.utc).isoformat(), "sha256": digest.hexdigest()}
                temporary.replace(target)
                try:
                    with self.connect() as connection:
                        connection.execute(
                            f"INSERT INTO files ({', '.join(FIELDS)}) VALUES ({', '.join('?' for _ in FIELDS)})",
                            tuple(record[field] for field in FIELDS),
                        )
                except Exception:
                    target.unlink(missing_ok=True)
                    raise
                return record
            except Exception:
                logger.exception("Failed to store or inspect upload %s", file_id)
                return {**rejected, "error_message": "This file could not be stored or inspected. Try again later."}
            finally:
                temporary.unlink(missing_ok=True)
