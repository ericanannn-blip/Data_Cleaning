"""Durable SQLite leases and a bounded, disposable parser process."""

import hashlib
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from threading import Event, Thread
from uuid import uuid4

from .config import ROOT
from .extraction import READER_VERSION

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, storage):
        self.storage, self.stop = storage, Event()
        settings = storage.settings
        self.options = {"bytes": settings.inspection_bytes, "rows": settings.sample_rows,
                        "pages": settings.sample_pages, "ocr_languages": settings.ocr_languages,
                        "timeout": settings.parser_timeout, "memory_mb": settings.parser_memory_mb,
                        "tika_url": os.getenv("TIKA_URL", "")}
        dependencies = {}
        for name in ("pypdf", "pypdfium2", "Pillow", "openpyxl", "ijson", "defusedxml", "striprtf", "numpy", "h5py", "pyarrow"):
            try:
                dependencies[name] = version(name)
            except PackageNotFoundError:
                dependencies[name] = None
        # Installing or replacing a native reader must invalidate missing-reader results.
        # Stat the executables without loading their libraries into the API process.
        for name in ("tesseract", "ffprobe"):
            executable = shutil.which(name)
            try:
                path = Path(executable).resolve() if executable else None
                stamp = path.stat() if path else None
                dependencies[name] = {"path": str(path), "size": stamp.st_size, "mtime_ns": stamp.st_mtime_ns} if stamp else None
            except OSError:
                dependencies[name] = None
        self.policy_key = hashlib.sha256(json.dumps({"reader_version": READER_VERSION, "dependencies": dependencies, **self.options},
                                                   sort_keys=True).encode()).hexdigest()
        self.thread = Thread(target=self.run, name="content-reader", daemon=True)

    def start(self):
        self.refresh_policy()
        self.thread.start()

    def refresh_policy(self):
        with self.storage.connect() as connection:
            connection.execute("""UPDATE jobs SET state='pending', attempts=0, lease_until=0
                WHERE state='completed' AND NOT EXISTS (
                    SELECT 1 FROM analyses a JOIN files f ON f.sha256=a.sha256
                    WHERE f.id=jobs.file_id AND a.policy_key=?)""", (self.policy_key,))

    def close(self):
        self.stop.set()
        self.thread.join(timeout=10)

    def analysis(self, file_id):
        record = self.storage.get_file(file_id)
        with self.storage.connect() as connection:
            job = connection.execute("SELECT state, attempts, error_code FROM jobs WHERE file_id = ?", (file_id,)).fetchone()
            cached = connection.execute("SELECT result FROM analyses WHERE sha256 = ? AND policy_key = ?",
                                        (record["sha256"], self.policy_key)).fetchone()
        return {"file_id": file_id, "job": dict(job) if job else {"state": "pending"},
                "analysis": json.loads(cached[0]) if cached else None}

    def reprocess(self, file_id):
        record = self.storage.get_file(file_id)
        with self.storage.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM analyses WHERE sha256 = ? AND policy_key = ?", (record["sha256"], self.policy_key))
            # Changing the lease token fences a result produced by an old job.
            connection.execute("""INSERT INTO jobs(file_id) VALUES (?) ON CONFLICT(file_id) DO UPDATE SET
                state='pending', attempts=0, lease_until=0, lease_token=NULL, error_code=NULL""", (file_id,))

    def claim(self):
        now = time.time()
        with self.storage.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""UPDATE jobs SET state='failed', error_code='retry_exhausted'
                WHERE state='running' AND lease_until < ? AND attempts >= 3""", (now,))
            connection.execute("""UPDATE jobs SET state='pending', lease_until=0
                WHERE state='running' AND lease_until < ? AND attempts < 3""", (now,))
            row = connection.execute("""SELECT jobs.file_id, files.sha256 FROM jobs JOIN files ON files.id=jobs.file_id
                WHERE state='pending' AND lease_until=0 AND attempts < 3
                ORDER BY jobs.rowid LIMIT 1""").fetchone()
            if row is None:
                return None
            token = str(uuid4())
            connection.execute("""UPDATE jobs SET state='running', attempts=attempts+1,
                lease_until=?, lease_token=?, updated_at=? WHERE file_id=?""",
                (now + self.options["timeout"] + 30, token, now, row["file_id"]))
            return {**dict(row), "token": token}

    @staticmethod
    def terminate(process):
        if process.poll() is None:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        process.communicate()

    def parse(self, job):
        path = self.storage.raw_path(job["file_id"])
        if not path.is_file():
            return {"status": "storage_missing", "error_code": "original_missing", "schema": {"candidates": []}}
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen([sys.executable, "-m", "backend.extraction", str(path), json.dumps(self.options)],
                                       cwd=ROOT, env={**os.environ, "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"},
                                       stdout=output, stderr=subprocess.DEVNULL, start_new_session=os.name == "posix")
            deadline = time.monotonic() + self.options["timeout"]
            code, output_limit = "parser_timeout", 2 * 1024 * 1024
            while not self.stop.is_set() and time.monotonic() < deadline:
                if os.fstat(output.fileno()).st_size > output_limit:
                    code = "output_limit"
                    break
                try:
                    process.wait(timeout=.2)
                    if process.returncode != 0:
                        return {"status": "resource_limit", "error_code": "parser_exit", "schema": {"candidates": []}}
                    if os.fstat(output.fileno()).st_size > output_limit:
                        code = "output_limit"
                        break
                    output.seek(0)
                    try:
                        return json.load(output)
                    except ValueError:
                        return {"status": "reader_error", "error_code": "invalid_reader_response", "schema": {"candidates": []}}
                except subprocess.TimeoutExpired:
                    continue
            self.terminate(process)
            if self.stop.is_set():
                return None
            return {"status": "resource_limit", "error_code": code, "schema": {"candidates": []}}

    def run_once(self):
        job = self.claim()
        if not job:
            return False
        with self.storage.connect() as connection:
            cached = connection.execute("SELECT result FROM analyses WHERE sha256=? AND policy_key=?",
                                        (job["sha256"], self.policy_key)).fetchone()
        result = json.loads(cached[0]) if cached else self.parse(job)
        with self.storage.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # An explicit reprocess or another worker may have acquired the job.
            current = connection.execute("SELECT lease_token FROM jobs WHERE file_id=?", (job["file_id"],)).fetchone()
            if current[0] != job["token"]:
                return True
            if result is None:
                connection.execute("UPDATE jobs SET state='pending', attempts=MAX(0,attempts-1), lease_until=0 WHERE file_id=?", (job["file_id"],))
                return False
            connection.execute("INSERT OR REPLACE INTO analyses VALUES (?, ?, ?, ?)",
                               (job["sha256"], self.policy_key, json.dumps(result, ensure_ascii=False),
                                datetime.now(timezone.utc).isoformat()))
            connection.execute("UPDATE jobs SET state='completed', lease_until=0, error_code=NULL, updated_at=? WHERE file_id=?",
                               (time.time(), job["file_id"]))
        return True

    def run(self):
        while not self.stop.is_set():
            try:
                if not self.run_once():
                    self.stop.wait(.5)
            except Exception:
                logger.exception("Content worker failed; durable lease will be retried")
                self.stop.wait(1)
