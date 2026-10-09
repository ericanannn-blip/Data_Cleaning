import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from backend.config import Settings
from backend.main import create_app
from backend.profiling import build_profile
from backend.readers import MAX_MEMBER_BYTES
from tests.fixtures import image_bytes, pdf_bytes, zip_bytes


class ReadersAndSchemasTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.settings = Settings(self.root / "app.db", self.root / "raw")
        self.app = create_app(self.settings)
        self.client = self.enterContext(TestClient(self.app))

    def upload(self, name, data):
        response = self.client.post("/files", files={"files": (name, data, "application/octet-stream")})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["succeeded"], 1, response.text)
        record = response.json()["files"][0]
        self.assertEqual((self.settings.upload_dir / f'{record["id"]}.dat').read_bytes(), data)
        return record

    def profile(self, record, **params):
        return self.client.get(f'/files/{record["id"]}/profile', params=params).json()

    def test_content_first_formats_and_rendered_image(self):
        record = self.upload("misleading.pdf", image_bytes())
        self.assertEqual((record["detected_type"], record["category"]), ("png", "image"))
        profile = self.profile(record)
        self.assertEqual(profile["metadata"]["width"], 32)
        response = self.client.get(f'/files/{record["id"]}/image')
        self.assertEqual(response.headers["content-type"], "image/png")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        with Image.open(io.BytesIO(response.content)) as image:
            self.assertEqual(image.size, (32, 24))
        self.assertNotEqual(response.content, image_bytes())

    def test_tiff_and_jpeg_use_the_image_reader(self):
        for format, extension in [("TIFF", "tiff"), ("JPEG", "jpg")]:
            record = self.upload("picture." + extension, image_bytes(format))
            self.assertEqual(record["reader"], "image")
            self.assertEqual(self.client.get(f'/files/{record["id"]}/image').status_code, 200)

    def test_pdf_pages_render_and_text_tracks_page(self):
        record = self.upload("document.dat", pdf_bytes())
        self.assertEqual(self.profile(record)["metadata"]["pages"], 2)
        base = f'/files/{record["id"]}/pages/'
        self.assertIn("Synthetic page one", self.client.get(base + "1").json()["text"])
        self.assertIn("Synthetic page two", self.client.get(base + "2").json()["text"])
        image = self.client.get(base + "2/image")
        self.assertEqual(image.status_code, 200)
        self.assertLess(len(image.content), 100_000)
        for page in (0, 3, -1):
            self.assertEqual(self.client.get(base + str(page)).status_code, 422)

    def test_damaged_readers_retain_originals_and_report_quality(self):
        for name, data in [("broken.png", b"\x89PNG\r\n\x1a\ninvalid"), ("broken.pdf", b"%PDF-1.4\ninvalid"),
                           ("broken.zip", b"PK\x03\x04invalid")]:
            record = self.upload(name, data)
            profile = self.profile(record)
            self.assertEqual(profile["reader"], "hex")
            self.assertIn("reader_error", [note["code"] for note in profile["issues"]])
        self.assertEqual(self.client.get(f'/files/{record["id"]}/archive/0/profile').status_code, 422)

    def test_zip_members_read_in_memory_and_unsafe_paths_are_blocked(self):
        record = self.upload("bundle.zip", zip_bytes([
            ("data/records.csv", b"id,value\n001,5\n002,7\n"), ("image.png", image_bytes()),
            ("../../outside.txt", b"secret"), ("C:\\outside.txt", b"secret"), ("paper.pdf", pdf_bytes())]))
        profile = self.profile(record)
        self.assertEqual(profile["metadata"]["entries"], 5)
        base = f'/files/{record["id"]}'
        child = self.client.get(base + "/archive/0/profile").json()["profile"]
        self.assertEqual(child["format"], "csv")
        self.assertEqual(child["rows"][0]["/id"], "001")
        self.assertEqual(self.client.get(base + "/image?entry=1").status_code, 200)
        self.assertIn("Synthetic page two", self.client.get(base + "/pages/2?entry=4").json()["text"])
        for entry in (2, 3, -1, 100):
            self.assertEqual(self.client.get(base + f"/archive/{entry}/profile").status_code, 422)
        self.assertEqual(len(list(self.settings.upload_dir.iterdir())), 1)
        self.assertFalse((self.root / "outside.txt").exists())

    def test_zip_size_count_and_compression_limits(self):
        entries = [("large.txt", b"x" * (MAX_MEMBER_BYTES + 1))]
        record = self.upload("large.zip", zip_bytes(entries))
        self.assertTrue(self.profile(record)["entries"][0]["blocked"])
        self.assertEqual(self.client.get(f'/files/{record["id"]}/archive/0/profile').status_code, 422)
        record = self.upload("many.zip", zip_bytes([(str(index), b"") for index in range(1001)]))
        self.assertEqual(self.profile(record)["reader"], "hex")

    def test_csv_quality_proposals_preserve_values_and_colliding_names(self):
        data = b" ID ,Amount,amount\n001, 5 ,7\n002,,oops\n002,,oops\nwrong,row\n"
        record = self.upload("table.csv", data)
        profile = self.profile(record)
        fields = {field["path"]: field for field in profile["fields"]}
        self.assertEqual(fields["/ ID "]["types"], {"string": 3})
        self.assertEqual(fields["/Amount"]["null_count"], 2)
        self.assertNotEqual(fields["/Amount"]["normalized_name"], fields["/amount"]["normalized_name"])
        codes = {note["code"] for note in profile["issues"]}
        self.assertTrue({"name_collision", "row_width", "missing_values", "type_conflict", "whitespace", "duplicate_rows"} <= codes)
        self.assertEqual(profile["rows"][0]["/Amount"], " 5 ")

    def test_json_paths_nullability_and_primitive_types_are_preserved(self):
        record = self.upload("records.json", json.dumps([
            {"id": "001", "value": 1, "nested": {"a/b": True}, "a.b": "null"},
            {"id": "002", "value": "1", "nested": {"a/b": None}},
        ]).encode())
        fields = {field["path"]: field for field in self.profile(record)["fields"]}
        self.assertEqual(fields["/value"]["types"], {"integer": 1, "string": 1})
        self.assertEqual(fields["/a.b"]["types"], {"string": 1})
        self.assertEqual(fields["/a.b"]["missing_count"], 1)
        self.assertEqual(fields["/nested/a~1b"]["null_count"], 1)

    def test_ndjson_invalid_lines_are_reported_and_tsv_is_detected(self):
        record = self.upload("events.jsonl", b'{"id":1}\ninvalid\n{"id":2}\n')
        profile = self.profile(record)
        self.assertEqual(profile["format"], "ndjson")
        self.assertEqual(profile["sampling"]["sampled_rows"], 2)
        self.assertIn("invalid_records", {note["code"] for note in profile["issues"]})
        record = self.upload("table.dat", b"id\tvalue\n001\t5\n002\t7\n")
        self.assertEqual(record["detected_type"], "tsv")

    def test_sampling_is_reproducible_and_utf8_boundary_does_not_corrupt_values(self):
        data = ("id,value\n" + "".join(f"{i},中文记录\n" for i in range(1000))).encode()
        checksum = hashlib.sha256(data).hexdigest()
        first = build_profile(io.BytesIO(data), "table.csv", len(data), checksum, 4097, 10)
        second = build_profile(io.BytesIO(data), "table.csv", len(data), checksum, 4097, 10)
        self.assertEqual(first, second)
        self.assertEqual(first["sampling"]["sampled_rows"], 10)
        self.assertFalse(first["sampling"]["complete"])
        self.assertTrue(all(row["/value"] == "中文记录" for row in first["rows"]))

    def test_schema_versions_keep_evidence_and_late_field_drift(self):
        data = json.dumps([{"id": i, "value": i, **({"late": "added"} if i > 15 else {})} for i in range(40)]).encode()
        settings = Settings(self.settings.database_path, self.settings.upload_dir, inspection_bytes=128)
        with TestClient(create_app(settings)) as client:
            record = client.post("/files", files={"files": ("array.json", data)}).json()["files"][0]
            key = record["schema_key"]
            initial = client.get(f'/files/{record["id"]}/profile').json()
            self.assertFalse(initial["sampling"]["complete"])
            self.assertNotIn("/late", {field["path"] for field in initial["fields"]})
            reviewed = client.post(f"/schemas/{key}/confirm?version=1")
            self.assertEqual(reviewed.status_code, 200)
            updated = client.post(f'/files/{record["id"]}/resample?sample_bytes=65536&sample_rows=200').json()
            self.assertEqual(updated["version"], 2)
            self.assertEqual(updated["schema_key"], key)
            history = client.get(f"/schemas/{key}").json()["versions"]
            self.assertEqual(history[0]["drift"]["added_fields"], ["/late"])
            self.assertIsNone(history[0]["reviewed_at"])
            self.assertIsNotNone(history[1]["reviewed_at"])
            self.assertEqual(history[0]["definition"]["source_files"], 1)
            self.assertEqual(history[0]["evidence"][0]["sampling"]["source_sha256"], hashlib.sha256(data).hexdigest())
            self.assertEqual(client.post(f"/schemas/{key}/confirm?version=1").status_code, 409)
            self.assertEqual(client.get(f'/files/{record["id"]}/profile?version=1').json(), initial)

    def test_matching_shape_groups_samples_and_counts_latest_profile_once(self):
        first = self.upload("one.json", b'[{"id":1,"value":2}]')
        second = self.upload("two.json", b'[{"id":2,"value":"two"}]')
        self.assertEqual(first["schema_key"], second["schema_key"])
        self.client.post(f'/files/{first["id"]}/resample')
        latest = self.client.get(f'/schemas/{first["schema_key"]}').json()["versions"][0]
        self.assertEqual(latest["definition"]["source_files"], 2)
        self.assertEqual(latest["definition"]["sampled_rows"], 2)
        self.assertEqual(len(latest["evidence"]), 2)

    def test_classification_changes_do_not_mix_different_record_grains(self):
        data = b"id,value\n" + b"001,5\n" * 300
        settings = Settings(self.settings.database_path, self.settings.upload_dir, inspection_bytes=len(b"id,value\n"))
        with TestClient(create_app(settings)) as client:
            record = client.post("/files", files={"files": ("table.dat", data)}).json()["files"][0]
            old_key = record["schema_key"]
            self.assertIsNotNone(old_key)
            updated = client.post(f'/files/{record["id"]}/resample').json()
            self.assertEqual(updated["format"], "csv")
            self.assertNotEqual(updated["schema_key"], old_key)
            retired = client.get(f"/schemas/{old_key}").json()["versions"][0]
            self.assertEqual(retired["definition"]["status"], "retired")
            self.assertEqual(client.post(f'/schemas/{old_key}/confirm?version={retired["version"]}').status_code, 409)
            self.assertEqual(client.get("/files?category=table&detected_type=csv").json()["total"], 1)
            self.assertEqual(len(client.get("/schemas").json()["schemas"]), 1)

    def test_global_categories_search_and_filters_work_beyond_first_page(self):
        self.upload("old-target.csv", b"id,value\n001,5\n")
        for index in range(3):
            self.upload(f"newer-{index}.txt", b"notes")
        listed = self.client.get("/files?limit=1&category=table&q=target&detected_type=csv").json()
        self.assertEqual(listed["total"], 1)
        self.assertEqual(listed["files"][0]["original_name"], "old-target.csv")
        self.assertEqual(listed["categories"], {"table": 1, "text": 3})
        self.assertEqual(listed["summary"]["total"], 4)
        self.assertEqual(self.client.get("/files?q=' OR 1=1 --").json()["total"], 0)

    def test_identical_files_report_duplicate_bytes(self):
        one = self.upload("one.txt", b"same bytes")
        self.upload("two.txt", b"same bytes")
        self.assertEqual(self.client.get(f'/files/{one["id"]}').json()["duplicate_files"], 1)

    def test_profile_history_persists_after_restart_and_legacy_records_are_lazy(self):
        record = self.upload("saved.csv", b"id,value\n001,5\n")
        self.client.post(f'/files/{record["id"]}/resample')
        with TestClient(create_app(self.settings)) as client:
            self.assertEqual(client.get(f'/files/{record["id"]}/profile').json()["version"], 2)
            self.assertEqual(len(client.get(f'/files/{record["id"]}/history').json()["versions"]), 2)
        with sqlite3.connect(self.settings.database_path) as connection:
            connection.execute("DELETE FROM schema_versions")
            connection.execute("DELETE FROM file_profiles")
        self.assertEqual(self.client.get("/files").json()["files"][0]["profile_version"], 0)
        self.assertEqual(self.profile(record)["version"], 1)
        self.assertEqual(len(self.client.get("/schemas").json()["schemas"]), 1)

    def test_invalid_versions_budgets_and_missing_records(self):
        record = self.upload("notes.txt", b"notes")
        base = f'/files/{record["id"]}'
        self.assertEqual(self.client.get(base + "/profile?version=99").status_code, 404)
        for query in ("sample_bytes=1048577", "sample_rows=2001", "sample_rows=0"):
            self.assertEqual(self.client.post(base + "/resample?" + query).status_code, 422)
        for suffix in ("/profile", "/history", "/image", "/pages/1", "/archive/0/profile"):
            self.assertEqual(self.client.get("/files/missing" + suffix).status_code, 404)
        self.assertEqual(self.client.get("/schemas/missing").status_code, 404)
        self.assertEqual(self.client.post("/schemas/missing/confirm?version=1").status_code, 404)

    def test_unavailable_storage_preserves_current_profile_and_uses_safe_errors(self):
        record = self.upload("notes.txt", b"notes")
        with self.assertLogs("backend.main", level="ERROR"), patch("backend.storage.build_profile", side_effect=OSError("/private/storage/path")):
            response = self.client.post(f'/files/{record["id"]}/resample')
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("/private", response.text)
        self.assertEqual(self.profile(record)["version"], 1)

    def test_changed_original_cannot_forge_resample_evidence(self):
        record = self.upload("notes.txt", b"original notes")
        (self.settings.upload_dir / f'{record["id"]}.dat').write_bytes(b"altered notes")
        response = self.client.post(f'/files/{record["id"]}/resample')
        self.assertEqual(response.status_code, 422)
        self.assertIn("upload hash", response.json()["detail"])
        self.assertEqual(self.profile(record)["version"], 1)

    def test_legacy_unknown_files_gain_consistent_classification_after_profiling(self):
        record = self.upload("legacy.dat", image_bytes("TIFF"))
        with sqlite3.connect(self.settings.database_path) as connection:
            connection.execute("DELETE FROM schema_versions")
            connection.execute("DELETE FROM file_profiles")
            connection.execute("UPDATE files SET detected_type = 'unknown', confidence = 'unknown', status = 'unsupported'")
        self.assertEqual(self.profile(record)["reader"], "image")
        updated = self.client.get(f'/files/{record["id"]}').json()
        self.assertEqual((updated["detected_type"], updated["confidence"], updated["status"]), ("tiff", "signature", "ok"))


if __name__ == "__main__":
    unittest.main()
