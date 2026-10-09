"""SQLite metadata and private UUID-named raw files."""

from datetime import datetime, timezone
from contextlib import contextmanager
import hashlib
import json
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
from .profiling import CATEGORIES, TEXT_MIMES, build_profile, fingerprint, schema_definition, schema_drift
from .readers import ReaderError


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
                CREATE INDEX IF NOT EXISTS files_sha256 ON files(sha256);
                CREATE TABLE IF NOT EXISTS file_profiles (
                    file_id TEXT NOT NULL REFERENCES files(id),
                    version INTEGER NOT NULL,
                    schema_key TEXT,
                    data_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (file_id, version)
                );
                CREATE INDEX IF NOT EXISTS profiles_schema ON file_profiles(schema_key);
                CREATE TABLE IF NOT EXISTS schema_versions (
                    schema_key TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    definition_json TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    drift_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    reviewed_at TEXT,
                    PRIMARY KEY (schema_key, version)
                );
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

    def _enrich(self, connection, record):
        record = dict(record)
        profile = connection.execute(
            "SELECT version, schema_key, data_json FROM file_profiles WHERE file_id = ? ORDER BY version DESC LIMIT 1",
            (record["id"],),
        ).fetchone()
        details = json.loads(profile["data_json"]) if profile else {}
        record.update(category=details.get("category", CATEGORIES.get(record["detected_type"], "unknown")),
                      reader=details.get("reader", "hex"), profile_version=profile["version"] if profile else 0,
                      schema_key=profile["schema_key"] if profile else None,
                      quality_issues=sum(item["severity"] == "review" for item in details.get("issues", [])))
        record["duplicate_files"] = connection.execute(
            "SELECT COUNT(*) - 1 FROM files WHERE sha256 = ?", (record["sha256"],)
        ).fetchone()[0]
        return record

    def list_files(self, limit: int, offset: int, category=None, query="", detected_type=None) -> dict:
        category_case = "CASE f.detected_type " + " ".join(
            f"WHEN '{kind}' THEN '{group}'" for kind, group in CATEGORIES.items()) + " ELSE 'unknown' END"
        join = """FROM files f LEFT JOIN file_profiles p ON p.file_id = f.id
                  AND p.version = (SELECT MAX(version) FROM file_profiles WHERE file_id = f.id)"""
        category_sql = f"COALESCE(json_extract(p.data_json, '$.category'), {category_case})"
        clauses, values = [], []
        if category:
            clauses.append(f"{category_sql} = ?")
            values.append(category)
        if query:
            clauses.append("instr(lower(f.original_name), lower(?)) > 0")
            values.append(query)
        if detected_type:
            clauses.append("f.detected_type = ?")
            values.append(detected_type)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as connection:
            records = connection.execute(
                f"SELECT f.* {join}{where} ORDER BY f.created_at DESC, f.id DESC LIMIT ? OFFSET ?",
                (*values, limit, offset),
            ).fetchall()
            total = connection.execute(f"SELECT COUNT(*) {join}{where}", values).fetchone()[0]
            categories = {row["category"]: row["count"] for row in connection.execute(
                f"SELECT {category_sql} AS category, COUNT(*) AS count {join} GROUP BY category")}
            formats = [row[0] for row in connection.execute("SELECT DISTINCT detected_type FROM files ORDER BY detected_type")]
            totals = connection.execute("""
                SELECT COUNT(*) AS total,
                       COALESCE(SUM(confidence = 'signature'), 0) AS signatures,
                       COALESCE(SUM(confidence != 'signature'), 0) AS review
                FROM files
            """).fetchone()
            files = [self._enrich(connection, row) for row in records]
        return {"files": files, "total": total, "summary": dict(totals),
                "categories": categories, "formats": formats, "limit": limit, "offset": offset}

    def get_file(self, file_id: str):
        with self.connect() as connection:
            record = connection.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
            return self._enrich(connection, record) if record else None

    def raw_path(self, file_id):
        # IDs always come from a persisted record, never directly from a URL.
        record = self.get_file(file_id)
        return self.settings.upload_dir / f'{record["id"]}.dat' if record else None

    def _save_profile(self, connection, file_id, profile):
        previous = connection.execute(
            "SELECT version, schema_key, data_json FROM file_profiles WHERE file_id = ? ORDER BY version DESC LIMIT 1", (file_id,)
        ).fetchone()
        version = previous["version"] + 1 if previous else 1
        previous_profile = json.loads(previous["data_json"]) if previous else {}
        same_grain = (previous_profile.get("format"), previous_profile.get("grain")) == (profile["format"], profile["grain"])
        old_key = previous["schema_key"] if previous else None
        key = (old_key if same_grain else None) or fingerprint(profile)
        now = datetime.now(timezone.utc).isoformat()
        connection.execute("INSERT INTO file_profiles VALUES (?, ?, ?, ?, ?)",
                           (file_id, version, key, json.dumps(profile, ensure_ascii=False), now))
        unsupported = profile["format"] in {"unknown", "empty"}
        confidence = "unknown" if unsupported else "heuristic" if profile["format"] in TEXT_MIMES else "signature"
        note = ("This file is empty; there is no content to classify." if profile["format"] == "empty"
                else "Format could not be identified. Manual review is needed.") if unsupported else None
        connection.execute("UPDATE files SET detected_type = ?, mime_type = ?, encoding = ?, confidence = ?, status = ?, error_message = ? WHERE id = ?",
                           (profile["format"], profile["mime_type"], profile["encoding"], confidence,
                            "unsupported" if unsupported else "ok", note, file_id))
        keys = [key] if key else []
        if old_key and old_key != key:
            keys.append(old_key)
        for key in keys:
            records = connection.execute("""
                SELECT p.* FROM file_profiles p WHERE p.schema_key = ? AND p.version =
                    (SELECT MAX(version) FROM file_profiles WHERE file_id = p.file_id)
                ORDER BY p.file_id
            """, (key,)).fetchall()
            profiles = [json.loads(row["data_json"]) for row in records]
            prior = connection.execute(
                "SELECT version, definition_json FROM schema_versions WHERE schema_key = ? ORDER BY version DESC LIMIT 1", (key,)
            ).fetchone()
            if profiles:
                definition = schema_definition(profiles)
            else:
                definition = {**json.loads(prior["definition_json"]), "fields": [],
                              "source_files": 0, "sampled_rows": 0, "status": "retired"}
            drift = schema_drift(json.loads(prior["definition_json"]) if prior else {}, definition)
            evidence = [{"file_id": row["file_id"], "profile_version": row["version"], "sampling": data["sampling"]}
                        for row, data in zip(records, profiles)]
            connection.execute("INSERT INTO schema_versions VALUES (?, ?, ?, ?, ?, ?, NULL)",
                (key, prior["version"] + 1 if prior else 1, json.dumps(definition, ensure_ascii=False),
                 json.dumps(evidence), json.dumps(drift), now))

    def get_profile(self, file_id, version=None):
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM file_profiles WHERE file_id = ? " +
                ("AND version = ?" if version else "ORDER BY version DESC LIMIT 1"),
                (file_id, version) if version else (file_id,)).fetchone()
        if row is None and version is None and self.get_file(file_id):
            return self.resample(file_id, self.settings.inspection_bytes, self.settings.sample_rows)
        if row is None:
            return None
        result = json.loads(row["data_json"])
        result.update(version=row["version"], schema_key=row["schema_key"], created_at=row["created_at"])
        return result

    def resample(self, file_id, byte_limit, row_limit):
        with self.write_lock:
            record = self.get_file(file_id)
            if record is None:
                return None
            path = self.raw_path(file_id)
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                while chunk := stream.read(64 * 1024):
                    digest.update(chunk)
            if digest.hexdigest() != record["sha256"]:
                raise ReaderError("Stored original differs from its upload hash. Restore storage before resampling.")
            profile = build_profile(path, record["original_name"], record["file_size"], record["sha256"], byte_limit, row_limit)
            with self.connect() as connection:
                self._save_profile(connection, file_id, profile)
        return self.get_profile(file_id)

    def profile_history(self, file_id):
        with self.connect() as connection:
            rows = connection.execute("SELECT version, created_at, data_json FROM file_profiles WHERE file_id = ? ORDER BY version DESC", (file_id,)).fetchall()
        return [{"version": row["version"], "created_at": row["created_at"],
                 "sampling": json.loads(row["data_json"])["sampling"]} for row in rows]

    def schemas(self):
        with self.connect() as connection:
            rows = connection.execute("""SELECT s.* FROM schema_versions s WHERE s.version =
                (SELECT MAX(version) FROM schema_versions WHERE schema_key = s.schema_key)
                ORDER BY s.created_at DESC, s.schema_key""").fetchall()
        return [{"id": row["schema_key"], "version": row["version"], "created_at": row["created_at"],
                 "reviewed_at": row["reviewed_at"], "definition": json.loads(row["definition_json"]),
                 "drift": json.loads(row["drift_json"])} for row in rows if json.loads(row["definition_json"])["status"] != "retired"]

    def schema_history(self, key):
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM schema_versions WHERE schema_key = ? ORDER BY version DESC", (key,)).fetchall()
        return [{"id": key, "version": row["version"], "created_at": row["created_at"], "reviewed_at": row["reviewed_at"],
                 "definition": json.loads(row["definition_json"]), "evidence": json.loads(row["evidence_json"]),
                 "drift": json.loads(row["drift_json"])} for row in rows]

    def confirm_schema(self, key, version):
        with self.write_lock, self.connect() as connection:
            latest = connection.execute("SELECT version, definition_json FROM schema_versions WHERE schema_key = ? ORDER BY version DESC LIMIT 1", (key,)).fetchone()
            if latest is None:
                return "missing"
            if latest["version"] != version:
                return "stale"
            if json.loads(latest["definition_json"])["status"] == "retired":
                return "retired"
            connection.execute("UPDATE schema_versions SET reviewed_at = COALESCE(reviewed_at, ?) WHERE schema_key = ? AND version = ?",
                               (datetime.now(timezone.utc).isoformat(), key, version))
        return "ok"

    def ingest(self, upload) -> dict:
        name = display_name(upload.filename)
        rejected = {"id": None, "original_name": name, "status": "error"}
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
                record["extension"] = Path(name).suffix.lower()
                profile = build_profile(temporary, name, size, record["sha256"], self.settings.inspection_bytes, self.settings.sample_rows)
                temporary.replace(target)
                try:
                    with self.connect() as connection:
                        connection.execute(
                            f"INSERT INTO files ({', '.join(FIELDS)}) VALUES ({', '.join('?' for _ in FIELDS)})",
                            tuple(record[field] for field in FIELDS),
                        )
                        self._save_profile(connection, file_id, profile)
                except Exception:
                    target.unlink(missing_ok=True)
                    raise
                return self.get_file(file_id)
            except Exception:
                logger.exception("Failed to store or inspect upload %s", file_id)
                return {**rejected, "error_message": "This file could not be stored or inspected. Try again later."}
            finally:
                temporary.unlink(missing_ok=True)
