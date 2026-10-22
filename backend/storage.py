"""SQLite metadata and private UUID-named raw files."""

import hashlib
import json
import logging
import os
import sqlite3
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from uuid import UUID, uuid4

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
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self):
        self.settings.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings.upload_dir.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            if connection.execute("PRAGMA user_version").fetchone()[0] > 2:
                raise RuntimeError("Database schema is newer than this application")
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
                CREATE INDEX IF NOT EXISTS files_sha256 ON files(sha256);
                CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'open'
                );
                CREATE TABLE IF NOT EXISTS batch_items (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id TEXT NOT NULL REFERENCES batches(id),
                    file_id TEXT REFERENCES files(id), rejection TEXT
                );
                CREATE INDEX IF NOT EXISTS batch_sequence ON batch_items(batch_id, sequence);
                CREATE TABLE IF NOT EXISTS jobs (
                    file_id TEXT PRIMARY KEY REFERENCES files(id),
                    state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    lease_until REAL NOT NULL DEFAULT 0, lease_token TEXT,
                    error_code TEXT, updated_at REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state, lease_until);
                CREATE TABLE IF NOT EXISTS analyses (
                    sha256 TEXT NOT NULL, policy_key TEXT NOT NULL,
                    result TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY (sha256, policy_key)
                );
                PRAGMA user_version = 2;
            """)
            connection.execute("INSERT OR IGNORE INTO jobs(file_id) SELECT id FROM files")
        # Do not remove raw orphans: a database restore may need those bytes.
        # Recent temporary files may belong to another live uploader.
        for temporary in self.settings.upload_dir.glob("*.part"):
            try:
                if str(UUID(temporary.stem)) == temporary.stem and temporary.stat().st_mtime < time.time() - 86400:
                    temporary.unlink(missing_ok=True)
            except (ValueError, FileNotFoundError):
                continue

    def create_batch(self) -> str:
        batch_id = str(uuid4())
        with self.connect() as connection:
            connection.execute("INSERT INTO batches(id, created_at) VALUES (?, ?)",
                               (batch_id, datetime.now(timezone.utc).isoformat()))
        return batch_id

    def batch_exists(self, batch_id: str) -> bool:
        with self.connect() as connection:
            return connection.execute("SELECT 1 FROM batches WHERE id = ?", (batch_id,)).fetchone() is not None

    def batch_state(self, batch_id: str, state: str):
        with self.connect() as connection:
            connection.execute("UPDATE batches SET state = ? WHERE id = ?", (state, batch_id))

    def batch_results(self, batch_id: str, limit: int = 200, after: int = 0) -> dict:
        with self.connect() as connection:
            rows = connection.execute("""SELECT sequence, file_id, rejection FROM batch_items
                WHERE batch_id = ? AND sequence > ? ORDER BY sequence LIMIT ?""",
                (batch_id, after, limit + 1)).fetchall()
            totals = connection.execute("""SELECT COUNT(file_id) succeeded,
                COUNT(*) - COUNT(file_id) failed FROM batch_items WHERE batch_id = ?""", (batch_id,)).fetchone()
            batch = connection.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
            files = []
            for row in rows[:limit]:
                record = (dict(connection.execute("SELECT * FROM files WHERE id = ?", (row["file_id"],)).fetchone())
                          if row["file_id"] else json.loads(row["rejection"]))
                files.append(record)
        return {"batch_id": batch_id, "state": batch["state"], "files": files,
                **dict(totals), "results_truncated": len(rows) > limit,
                "next_cursor": rows[limit - 1]["sequence"] if len(rows) > limit else None}

    def record_batch_item(self, batch_id: str | None, record: dict):
        if batch_id:
            with self.connect() as connection:
                connection.execute("INSERT INTO batch_items(batch_id, file_id, rejection) VALUES (?, ?, ?)",
                                   (batch_id, record["id"], None if record["id"] else json.dumps(record)))

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

    def ingest(self, upload, batch_id: str | None = None) -> dict:
        writer = UploadWriter(self, upload.filename, batch_id)
        try:
            while chunk := upload.file.read(64 * 1024):
                writer.write(chunk)
            return writer.finish()
        finally:
            writer.abort()

    def raw_path(self, file_id: str) -> Path:
        # The ID comes from a looked-up record, never directly from a filename.
        return self.settings.upload_dir / f"{file_id}.dat"


def fsync_directory(path: Path):
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class UploadWriter:
    """One current file on disk; no upload count or batch-sized in-memory list."""

    def __init__(self, storage: Storage, filename: str, batch_id: str | None = None):
        self.storage, self.batch_id = storage, batch_id
        self.name, self.file_id = display_name(filename), str(uuid4())
        self.temporary = storage.settings.upload_dir / f"{self.file_id}.part"
        self.target = storage.raw_path(self.file_id)
        self.output = self.temporary.open("xb")
        self.size, self.digest, self.error = 0, hashlib.sha256(), None

    def write(self, chunk: bytes):
        if self.error:
            return
        self.size += len(chunk)
        limit = self.storage.settings.max_file_bytes
        if limit and self.size > limit:
            self.error = "File exceeds the per-file upload limit."
            self.abort()
            return
        try:
            self.output.write(chunk)
            self.digest.update(chunk)
        except OSError:
            logger.exception("Failed to write upload %s", self.file_id)
            self.error = "Storage write failed. Check available disk space."
            self.abort()

    def finish(self) -> dict:
        rejected = {"id": None, "original_name": self.name, "status": "error"}
        try:
            if self.error:
                record = {**rejected, "error_message": self.error}
                self.storage.record_batch_item(self.batch_id, record)
                return record
            self.output.flush()
            os.fsync(self.output.fileno())
            self.output.close()
            metadata = inspect_file(self.temporary, self.storage.settings.inspection_bytes)
            record = {"id": self.file_id, "original_name": self.name,
                      "extension": Path(self.name).suffix.lower(), **metadata,
                      "created_at": datetime.now(timezone.utc).isoformat(), "sha256": self.digest.hexdigest()}
            with self.storage.write_lock, self.storage.connect() as connection:
                # Cross-process quota check and metadata/queue commit are atomic.
                connection.execute("BEGIN IMMEDIATE")
                quota = self.storage.settings.max_storage_bytes
                if quota:
                    used = connection.execute("SELECT COALESCE(SUM(file_size), 0) FROM files").fetchone()[0]
                    if used + self.size > quota:
                        self.error = "Configured storage is full. Contact the operator."
                        record = {**rejected, "error_message": self.error}
                        if self.batch_id:
                            connection.execute("INSERT INTO batch_items(batch_id, rejection) VALUES (?, ?)",
                                               (self.batch_id, json.dumps(record)))
                        return record
                self.temporary.replace(self.target)
                fsync_directory(self.target.parent)
                connection.execute(
                    f"INSERT INTO files ({', '.join(FIELDS)}) VALUES ({', '.join('?' for _ in FIELDS)})",
                    tuple(record[field] for field in FIELDS),
                )
                connection.execute("INSERT INTO jobs(file_id) VALUES (?)", (self.file_id,))
                if self.batch_id:
                    connection.execute("INSERT INTO batch_items(batch_id, file_id) VALUES (?, ?)",
                                       (self.batch_id, self.file_id))
            return record
        except Exception:
            logger.exception("Failed to commit upload %s", self.file_id)
            if self.target.exists():
                try:
                    accepted = self.storage.get_file(self.file_id)
                except Exception:
                    # Preserve bytes when commit outcome cannot be established.
                    # A later verify/restore must be able to recover the original.
                    logger.error("Commit outcome unknown; preserving original %s", self.file_id)
                    raise
                if accepted:
                    return accepted
                self.target.unlink(missing_ok=True)
            record = {**rejected, "error_message": "This file could not be stored or inspected. Try again later."}
            self.storage.record_batch_item(self.batch_id, record)
            return record
        finally:
            self.abort()

    def abort(self):
        if not self.output.closed:
            self.output.close()
        self.temporary.unlink(missing_ok=True)
