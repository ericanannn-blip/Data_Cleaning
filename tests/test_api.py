import io
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from uuid import uuid4

from fastapi.testclient import TestClient

from backend.config import Settings
from backend.main import create_app


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.settings = Settings(root / "metadata" / "app.db", root / "private" / "raw")
        self.app = create_app(self.settings)
        self.client = self.enterContext(TestClient(self.app))

    def post(self, entries):
        return self.client.post("/files", files=[("files", (name, data, "application/octet-stream")) for name, data in entries])

    def test_frontend_assets_health_and_config(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("DAT file inspector", response.text)
        self.assertIn("script-src 'self'", response.headers["content-security-policy"])
        for asset in ("styles.css", "app.js", "favicon.svg"):
            self.assertEqual(self.client.get(f"/static/{asset}").status_code, 200)
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})
        self.assertEqual(self.client.get("/config").json()["max_files"], 20)

    def test_initialization_and_empty_list(self):
        self.assertTrue(self.settings.database_path.exists())
        self.assertTrue(self.settings.upload_dir.is_dir())
        self.assertEqual(self.client.get("/files").json()["files"], [])
        with sqlite3.connect(self.settings.database_path) as db:
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_multiple_upload_classification_list_and_detail(self):
        entries = [("text.dat", b"hello world"), ("data.DAT", b'{"answer":42}'),
                   ("image.dat", b"\x89PNG\r\n\x1a\n" + bytes(range(20))), ("paper.dat", b"%PDF-1.4\n")]
        response = self.post(entries)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["succeeded"], 4)
        self.assertEqual([item["detected_type"] for item in body["files"]], ["text", "json", "png", "pdf"])
        self.assertEqual([item["confidence"] for item in body["files"]], ["heuristic", "heuristic", "signature", "signature"])
        data = self.client.get("/files").json()
        self.assertEqual(data["summary"], {"total": 4, "signatures": 2, "review": 2})
        for result, (_, original) in zip(body["files"], entries):
            self.assertEqual(self.client.get(f'/files/{result["id"]}').json(), result)
            self.assertEqual((self.settings.upload_dir / f'{result["id"]}.dat').read_bytes(), original)
            self.assertNotIn(self.temp.name, json.dumps(result))

    def test_partial_invalid_files_keep_successes(self):
        body = self.post([("valid.dat", b"hello"), ("bad.txt", b"bad"), ("unknown.dat", b"\x00\x01\xff\x03")]).json()
        self.assertEqual((body["succeeded"], body["failed"]), (2, 1))
        self.assertEqual(body["files"][1]["status"], "error")
        self.assertIn("Only .dat", body["files"][1]["error_message"])
        self.assertEqual(body["files"][2]["status"], "unsupported")
        self.assertEqual(self.client.get("/files").json()["total"], 2)

    def test_empty_and_unknown_are_persisted_for_review(self):
        body = self.post([("empty.dat", b""), ("binary.dat", b"\x00\x01\x02")]).json()
        self.assertEqual(body["succeeded"], 2)
        self.assertEqual([item["detected_type"] for item in body["files"]], ["empty", "unknown"])
        self.assertTrue(all(item["error_message"] for item in body["files"]))
        self.assertEqual(body["files"][0]["printable_ratio"], 0)

    def test_duplicate_names_do_not_overwrite(self):
        body = self.post([("same.dat", b"first"), ("same.dat", b"second")]).json()
        first, second = body["files"]
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(len(list(self.settings.upload_dir.glob("*.dat"))), 2)
        self.assertEqual((self.settings.upload_dir / f'{first["id"]}.dat').read_bytes(), b"first")

    def test_filename_traversal_and_control_characters_are_metadata_only(self):
        response = self.post([("../../outside.dat", b"hello"), ("C:\\secret\\windows.dat", b"other"), ("<script>.dat", b"text")])
        self.assertEqual(response.json()["succeeded"], 3)
        names = [item["original_name"] for item in response.json()["files"]]
        self.assertEqual(names, ["outside.dat", "windows.dat", "<script>.dat"])
        self.assertFalse((Path(self.temp.name) / "outside.dat").exists())
        self.assertEqual(len(list(self.settings.upload_dir.glob("*.dat"))), 3)

    def test_no_public_raw_files_or_internal_paths(self):
        record = self.post([("private.dat", b"private")]).json()["files"][0]
        for path in (f'/data/raw/{record["id"]}.dat', f'/raw/{record["id"]}.dat', f'/static/../data/raw/{record["id"]}.dat'):
            self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(set(record), {"id", "original_name", "extension", "file_size", "mime_type", "encoding", "magic_bytes", "printable_ratio", "sample_bytes", "detected_type", "confidence", "status", "created_at", "error_message", "sha256"})

    def test_archives_classify_without_extraction(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("../../danger.txt", "A" * 1_000_000)
        record = self.post([("archive.dat", buffer.getvalue())]).json()["files"][0]
        self.assertEqual(record["detected_type"], "zip")
        self.assertEqual(len(list(self.settings.upload_dir.iterdir())), 1)
        self.assertFalse((Path(self.temp.name) / "danger.txt").exists())

    def test_bounded_inspection_does_not_parse_partial_json(self):
        record = self.post([("large.dat", b'{"text":"' + b"a" * 70_000 + b'"}')]).json()["files"][0]
        self.assertEqual(record["sample_bytes"], self.settings.inspection_bytes)
        self.assertEqual(record["detected_type"], "text")
        self.assertEqual(record["confidence"], "heuristic")

    def test_configurable_per_file_limit_keeps_other_files(self):
        settings = Settings(self.settings.database_path, self.settings.upload_dir, max_file_bytes=4)
        with TestClient(create_app(settings)) as client:
            response = client.post("/files", files=[("files", ("too-big.dat", b"12345")), ("files", ("ok.dat", b"1234"))])
        self.assertEqual(response.json()["succeeded"], 1)
        self.assertEqual(response.json()["failed"], 1)
        self.assertFalse(list(self.settings.upload_dir.glob("*.part")))

    def test_declared_and_chunked_request_limits(self):
        settings = Settings(self.settings.database_path, self.settings.upload_dir, max_request_bytes=256)
        with TestClient(create_app(settings)) as client:
            response = client.post("/files", files={"files": ("big.dat", b"a" * 512)})
            self.assertEqual(response.status_code, 413)
            chunks = iter([b"--x\r\nContent-Disposition: form-data; name=\"files\"; filename=\"x.dat\"\r\n\r\n", b"a" * 512, b"\r\n--x--\r\n"])
            response = client.post("/files", content=chunks, headers={"Content-Type": "multipart/form-data; boundary=x"})
            self.assertEqual(response.status_code, 413)
        self.assertEqual(self.client.get("/files").json()["total"], 0)

    def test_storage_quota(self):
        settings = Settings(self.settings.database_path, self.settings.upload_dir, max_storage_bytes=5)
        with TestClient(create_app(settings)) as client:
            body = client.post("/files", files=[("files", ("a.dat", b"1234")), ("files", ("b.dat", b"12"))]).json()
        self.assertEqual((body["succeeded"], body["failed"]), (1, 1))
        self.assertIn("storage is full", body["files"][1]["error_message"])

    def test_malformed_wrong_field_missing_files_and_excess_count(self):
        for response, status in [
            (self.client.post("/files", content=b"bad", headers={"Content-Type": "application/json"}), 415),
            (self.client.post("/files", content=b"bad", headers={"Content-Type": "multipart/form-data"}), 400),
            (self.client.post("/files", content=b"garbage", headers={"Content-Type": "multipart/form-data; boundary=x"}), 400),
            (self.client.post("/files", files={"wrong": ("a.dat", b"a")}), 422),
            (self.client.post("/files", content=b"--x--\r\n", headers={"Content-Type": "multipart/form-data; boundary=x"}), 422),
            (self.post([(f"{i}.dat", b"a") for i in range(21)]), 400),
        ]:
            self.assertEqual(response.status_code, status, response.text)
            self.assertIn("detail", response.json())

    def test_unknown_detail_and_pagination(self):
        self.assertEqual(self.client.get("/files/not-a-record").status_code, 404)
        self.post([(f"{i}.dat", b"a") for i in range(3)])
        first = self.client.get("/files?limit=2").json()
        second = self.client.get("/files?limit=2&offset=2").json()
        self.assertEqual(len(first["files"]), 2)
        self.assertEqual(len(second["files"]), 1)
        self.assertEqual(first["total"], 3)
        self.assertEqual(self.client.get("/files?limit=0").status_code, 422)

    def test_metadata_and_raw_files_survive_new_application(self):
        record = self.post([("persist.dat", b"persistent bytes")]).json()["files"][0]
        with TestClient(create_app(self.settings)) as restarted:
            self.assertEqual(restarted.get("/files").json()["total"], 1)
            self.assertEqual(restarted.get(f'/files/{record["id"]}').json(), record)
        self.assertEqual((self.settings.upload_dir / f'{record["id"]}.dat').read_bytes(), b"persistent bytes")

    def test_database_failure_cleans_raw_upload_and_returns_safe_error(self):
        with self.assertLogs("backend.storage", level="ERROR"), patch("backend.storage.inspect_file", side_effect=OSError("/secret/internal/path")):
            body = self.post([("a.dat", b"a")]).json()
        self.assertEqual(body["failed"], 1)
        self.assertNotIn("/secret", body["files"][0]["error_message"])
        self.assertEqual(list(self.settings.upload_dir.iterdir()), [])

    def test_failed_metadata_commit_removes_renamed_raw_file(self):
        original = self.app.state.storage.connect

        @contextmanager
        def failing_connection():
            with original() as connection:
                class Proxy:
                    def execute(self, sql, *args):
                        if sql.startswith("INSERT"):
                            raise sqlite3.OperationalError("database is full")
                        return connection.execute(sql, *args)
                yield Proxy()

        with self.assertLogs("backend.storage", level="ERROR"), patch.object(self.app.state.storage, "connect", failing_connection):
            body = self.post([("commit-failure.dat", b"text")]).json()
        self.assertEqual(body["failed"], 1)
        self.assertEqual(list(self.settings.upload_dir.iterdir()), [])
        self.assertEqual(self.client.get("/files").json()["total"], 0)

    def test_startup_cleans_crash_orphans_but_preserves_committed_and_unrelated_files(self):
        record = self.post([("saved.dat", b"keep")]).json()["files"][0]
        orphan = self.settings.upload_dir / f"{uuid4()}.dat"
        partial = self.settings.upload_dir / f"{uuid4()}.part"
        operator_file = self.settings.upload_dir / "operator.dat"
        orphan.write_bytes(b"orphan")
        partial.write_bytes(b"incomplete")
        operator_file.write_bytes(b"unrelated")
        with TestClient(create_app(self.settings)) as client:
            self.assertEqual(client.get("/files").json()["total"], 1)
        self.assertFalse(orphan.exists())
        self.assertFalse(partial.exists())
        self.assertTrue(operator_file.exists())
        self.assertEqual((self.settings.upload_dir / f'{record["id"]}.dat').read_bytes(), b"keep")

    def test_environment_storage_paths_and_invalid_limit(self):
        root = Path(self.temp.name)
        with patch.dict(os.environ, {"APP_DATA_DIR": str(root / "env"), "DATABASE_PATH": str(root / "custom" / "db.sqlite"), "UPLOAD_DIR": str(root / "custom" / "raw"), "MAX_FILES": "3"}):
            settings = Settings.from_env()
            with TestClient(create_app(settings)) as client:
                self.assertEqual(client.get("/config").json()["max_files"], 3)
            self.assertTrue(settings.database_path.exists())
            self.assertTrue(settings.upload_dir.is_dir())
        with patch.dict(os.environ, {"MAX_FILES": "0"}):
            with self.assertRaises(ValueError):
                Settings.from_env()


if __name__ == "__main__":
    unittest.main()
