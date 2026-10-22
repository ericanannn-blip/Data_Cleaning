"""Integration coverage for durable ingestion, file readers, and data contracts."""

import hashlib
import importlib.util
import io
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfWriter

from backend.config import Settings
from backend.extraction import extract
from backend.jobs import Worker
from backend.main import create_app
from backend.manage import backup, import_sources, verify
from backend.schemas import infer_schemas
from backend.storage import Storage


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = Settings(self.root / "app.db", self.root / "raw", worker_enabled=False)
        self.app = create_app(self.settings)
        self.client = self.enterContext(TestClient(self.app))
        self.options = self.app.state.worker.options

    def upload(self, data, name="sample.dat"):
        body = self.client.post("/files?filename=" + name, content=data,
                                headers={"content-type": "application/octet-stream"}).json()
        return body["files"][0]

    def read(self, data):
        path = self.root / "source.dat"
        path.write_bytes(data)
        return extract(path, self.options)

    def test_more_than_thousand_files_have_paginated_complete_results(self):
        response = self.client.post("/files", files=[("files", (f"{i}.dat", b"a,b\n1,2\n")) for i in range(1005)])
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["succeeded"], 1005)
        self.assertEqual(len(body["files"]), 200)
        ids, cursor = [r["id"] for r in body["files"]], body["next_cursor"]
        while cursor:
            page = self.client.get(f'/batches/{body["batch_id"]}?after={cursor}').json()
            ids.extend(r["id"] for r in page["files"])
            cursor = page["next_cursor"]
        self.assertEqual(len(set(ids)), 1005)
        self.assertEqual(self.client.get("/files").json()["total"], 1005)

    def test_raw_stream_and_repeated_uploads_share_an_unbounded_batch(self):
        batch = self.client.post("/batches").json()["batch_id"]
        content = (b"id,value\n" + b"1,23\n" * 10000)
        response = self.client.post(f"/files?batch_id={batch}&filename=first.csv", content=iter([content[:5], content[5:]]),
                                    headers={"content-type": "application/octet-stream"})
        self.assertEqual(response.json()["succeeded"], 1)
        second = self.client.post(f"/files?batch_id={batch}&filename=second.pdf", content=b"some text",
                                  headers={"content-type": "application/octet-stream"}).json()
        self.assertEqual(second["succeeded"], 2)
        self.assertEqual(self.client.get("/files/" + second["files"][0]["id"] + "/download").content, content)

    def test_fragmented_multipart_and_truncation_preserve_completed_files(self):
        from backend.uploads import StreamingUpload
        storage = self.app.state.storage
        batch = storage.create_batch()
        parser = StreamingUpload(storage, batch, "multipart/form-data; boundary=xxx")
        body = b'--xxx\r\nContent-Disposition: form-data; name="files"; filename="a.dat"\r\n\r\na,b\n1,2\r\n--xxx--\r\n'
        for index in range(0, len(body), 3):
            parser.write(body[index:index + 3])
        parser.finish()
        saved = storage.batch_results(batch)
        self.assertEqual(saved["succeeded"], 1)
        truncated = body[:-10]
        response = self.client.post("/files", content=truncated,
                                    headers={"content-type": "multipart/form-data; boundary=xxx"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(list(self.settings.upload_dir.glob("*.part")))

    def test_authentication_protects_originals_metadata_and_schemas(self):
        settings = replace(self.settings, api_token="x" * 40)
        with TestClient(create_app(settings)) as client:
            self.assertEqual(client.post("/files?filename=x.dat", content=b"hello",
                                        headers={"content-type": "application/octet-stream"}).status_code, 401)
            self.assertEqual(client.get("/schemas").status_code, 401)
            self.assertTrue(client.get("/config").json()["authentication_required"])
            headers = {"authorization": "Bearer " + settings.api_token, "content-type": "application/octet-stream"}
            record = client.post("/files?filename=x.dat", content=b"hello", headers=headers).json()["files"][0]
            self.assertEqual(client.get(f'/files/{record["id"]}/download').status_code, 401)
            self.assertEqual(client.get(f'/files/{record["id"]}/download', headers=headers).content, b"hello")
        with patch.dict(os.environ, {"APP_ENV": "production", "API_TOKEN": ""}):
            with self.assertRaises(ValueError):
                Settings.from_env()

    def test_content_schemas_preserve_native_data_and_leading_zero_identifiers(self):
        result = self.read(b" user id ,amount,date,amount\n001,12.5,2026-10-01,2\n002,,2026-10-02,3,unexpected\n")
        self.assertEqual(result["format"], "table")
        raw, tidy, semantic = result["schema"]["candidates"]
        self.assertEqual(raw["json_schema"]["properties"]["user id"]["type"], "string")
        self.assertEqual(tidy["json_schema"]["properties"]["user_id"]["type"], "string")
        self.assertIn("null", tidy["json_schema"]["properties"]["amount"]["type"])
        self.assertEqual(semantic["json_schema"]["x-field-roles"]["user id"]["role"], "identifier")
        self.assertTrue(result["warnings"])
        self.assertEqual(raw["json_schema"]["required"], [])
        self.assertFalse(result["sampling"]["full_file_validation"])

    def test_fingerprints_group_by_content_shape_not_sample_size_or_filename(self):
        first = infer_schemas([{"temperature": 20}], "tabular", {}, [])
        second = infer_schemas([{"temperature": 20}, {"temperature": 22}], "tabular", {}, [])
        self.assertEqual(first["schema_fingerprint"], second["schema_fingerprint"])

    def test_large_table_samples_tail_schema_drift_without_full_read(self):
        content = b"id,value\n" + b"1,2\n" * 100000 + b"2,not_numeric\n" * 100
        result = self.read(content)
        self.assertEqual(len(result["sampling"]["byte_offsets"]), 3)
        self.assertLessEqual(result["sampling"]["bytes_read"], self.settings.inspection_bytes)
        self.assertLessEqual(result["sampling"]["records_sampled"], self.settings.sample_rows)
        self.assertIn("string", result["schema"]["candidates"][1]["json_schema"]["properties"]["value"]["type"])

    def test_json_jsonl_xml_html_rtf_and_compressed_content(self):
        import gzip
        cases = [(b'[{"id":1,"v":null},{"id":2,"v":2}]', "json"),
                 (b'{"id":1}\n{"id":2}\n', "jsonl"),
                 (b'<rows><row><id>1</id></row></rows>', "xml"),
                 (b'<html><body><p>hello</p><script>do not execute</script></body></html>', "html"),
                 (b'{\\rtf1\\ansi hello}', "rtf"), (gzip.compress(b"a,b\n1,2\n"), "table")]
        for data, fmt in cases:
            with self.subTest(fmt=fmt):
                result = self.read(data)
                self.assertEqual(result["format"], fmt)
                self.assertEqual(result["status"], "sampled")
                self.assertEqual(len(result["schema"]["candidates"]), 3)

    def test_pdf_text_pages_and_password_required(self):
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
        writer = PdfWriter()
        for index in range(5):
            page = writer.add_blank_page(width=300, height=200)
            font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                     NameObject("/Subtype"): NameObject("/Type1"),
                                     NameObject("/BaseFont"): NameObject("/Helvetica")})
            page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
            content = DecodedStreamObject()
            content.set_data(f"BT /F1 16 Tf 30 100 Td (Measurement page {index + 1}) Tj ET".encode())
            page[NameObject("/Contents")] = content
        output = io.BytesIO()
        writer.write(output)
        result = self.read(output.getvalue())
        self.assertEqual(result["sampling"]["page_indices"], [0, 2, 4])
        self.assertEqual([r["page"] for r in result["sample"]], [1, 3, 5])
        self.assertIn("Measurement", result["sample"][0]["text"])
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        writer.encrypt("secret")
        output = io.BytesIO()
        writer.write(output)
        result = self.read(output.getvalue())
        self.assertEqual(result["status"], "encrypted")
        self.assertEqual(result["schema"]["candidates"], [])

    def test_image_ocr_and_xor_restoration(self):
        image = Image.new("RGB", (900, 200), "white")
        font = ImageFont.truetype("DejaVuSans.ttf", 48)
        ImageDraw.Draw(image).text((30, 65), "INVOICE TOTAL 12345", font=font, fill="black")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        for data in (buffer.getvalue(), bytes(b ^ 0x66 for b in buffer.getvalue())):
            result = self.read(data)
            self.assertEqual(result["format"], "png")
            self.assertEqual(result["status"], "sampled")
            self.assertIn("12345", result["sample"][0]["ocr_text"])
        buffer = io.BytesIO()
        image.save(buffer, format="PDF")
        result = self.read(buffer.getvalue())
        self.assertEqual(result["format"], "pdf")
        self.assertEqual(result["status"], "sampled")
        self.assertIn("12345", result["sample"][0]["text"])

    @unittest.skipUnless(all(importlib.util.find_spec(name) for name in ("numpy", "pyarrow", "h5py")), "Install requirements-science.txt")
    def test_scientific_formats_with_isolated_readers(self):
        import h5py
        import numpy as np
        import pyarrow as pa
        import pyarrow.parquet as pq
        path = self.root / "array.npy"
        np.save(path, np.arange(30).reshape(3, 10))
        cases = [(path.read_bytes(), "npy")]
        path = self.root / "parquet.dat"
        pq.write_table(pa.table({"id": [1, 2], "value": [2.5, 3.5]}), path)
        cases.append((path.read_bytes(), "parquet"))
        path = self.root / "hdf.dat"
        with h5py.File(path, "w") as source:
            source["measurement"] = np.arange(30)
        cases.append((path.read_bytes(), "hdf5"))
        for data, fmt in cases:
            with self.subTest(fmt=fmt):
                record = self.upload(data)
                self.app.state.worker.run_once()
                analysis = self.app.state.worker.analysis(record["id"])["analysis"]
                self.assertEqual(analysis["format"], fmt)
                self.assertEqual(analysis["status"], "sampled")
                self.assertEqual(len(analysis["schema"]["candidates"]), 3)

    def test_parser_policy_change_requeues_existing_files(self):
        record = self.upload(b"a,b\n1,2\n")
        worker = self.app.state.worker
        worker.run_once()
        changed = Worker(Storage(replace(self.settings, sample_rows=20)))
        self.assertIsNone(changed.analysis(record["id"])["analysis"])
        changed.refresh_policy()
        self.assertEqual(changed.analysis(record["id"])["job"]["state"], "pending")
        changed.run_once()
        self.assertEqual(changed.analysis(record["id"])["analysis"]["sampling"]["rows_budget"], 20)

    def test_legacy_sqlite_schema_migrates_without_losing_data(self):
        record = self.upload(b"historical raw bytes")
        with self.app.state.storage.connect() as connection:
            connection.execute("DROP TABLE batch_items")
            connection.execute("DROP TABLE batches")
            connection.execute("DROP TABLE jobs")
            connection.execute("DROP TABLE analyses")
            connection.execute("PRAGMA user_version=0")
        self.app.state.storage.initialize()
        self.assertEqual(self.app.state.storage.get_file(record["id"]), record)
        self.assertEqual(self.app.state.storage.raw_path(record["id"]).read_bytes(), b"historical raw bytes")
        self.assertEqual(self.app.state.worker.analysis(record["id"])["job"]["state"], "pending")

    def test_docx_xlsx_pptx_readers(self):
        import openpyxl
        workbook = openpyxl.Workbook()
        workbook.active.append(["id", "value"])
        workbook.active.append(["001", 12])
        output = io.BytesIO()
        workbook.save(output)
        workbook.close()
        result = self.read(output.getvalue())
        self.assertEqual(result["format"], "xlsx")
        self.assertEqual(result["sample"][0]["id"], "001")
        for name, fmt in [("word/document.xml", "docx"), ("ppt/slides/slide1.xml", "pptx")]:
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w") as archive:
                archive.writestr(name, '<root><p><t>Readable document text</t></p></root>')
                if fmt == "pptx":
                    archive.writestr("ppt/presentation.xml", "<root/>")
            result = self.read(output.getvalue())
            self.assertEqual(result["format"], fmt)
            self.assertIn("Readable document text", result["sample"][0]["text"])

    def test_corrupt_readers_and_opaque_data_do_not_claim_success(self):
        for data, expected in [(b"%PDF-1.4\n", "corrupt"), (b"\x89PNG\r\n\x1a\n", "corrupt"),
                               (b"\x00\xff\x00\x01", "unsupported"), (b"", "empty")]:
            record = self.upload(data)
            worker = self.app.state.worker
            worker.run_once()
            analysis = worker.analysis(record["id"])["analysis"]
            self.assertEqual(analysis["status"], expected)
            self.assertEqual(analysis["schema"]["candidates"], [])
            self.assertTrue(self.app.state.storage.raw_path(record["id"]).is_file())

    def test_content_cache_reuses_duplicate_and_versioned_policy(self):
        first = self.upload(b"a,b\n1,2\n")
        second = self.upload(b"a,b\n1,2\n", "other.pdf")
        worker = self.app.state.worker
        with patch.object(worker, "parse", wraps=worker.parse) as parse:
            worker.run_once()
            worker.run_once()
            self.assertEqual(parse.call_count, 1)
        self.assertEqual(worker.analysis(first["id"])["analysis"], worker.analysis(second["id"])["analysis"])
        self.assertEqual(self.client.get("/schemas").json()["schemas"][0]["uploaded_files"], 2)
        self.client.post(f'/files/{first["id"]}/reprocess')
        self.assertIsNone(worker.analysis(first["id"])["analysis"])

    def test_expired_job_is_recovered_and_reprocess_fences_stale_work(self):
        record = self.upload(b"a,b\n1,2\n")
        storage, worker = self.app.state.storage, self.app.state.worker
        job = worker.claim()
        with storage.connect() as connection:
            connection.execute("UPDATE jobs SET lease_until=0 WHERE file_id=?", (record["id"],))
        recovered = worker.claim()
        self.assertNotEqual(job["token"], recovered["token"])
        worker.reprocess(record["id"])
        with patch.object(worker, "parse", side_effect=lambda job: (worker.reprocess(record["id"]), {"status": "sampled"})[1]):
            worker.run_once()
        self.assertEqual(worker.analysis(record["id"])["job"]["state"], "pending")
        self.assertIsNone(worker.analysis(record["id"])["analysis"])
        worker.run_once()
        self.assertEqual(worker.analysis(record["id"])["job"]["state"], "completed")

    def test_backup_restored_originals_schemas_and_integrity(self):
        record = self.upload(b"id,value\n1,2\n")
        self.app.state.worker.run_once()
        snapshot = self.root / "backup"
        manifest = backup(self.app.state.storage, snapshot)
        self.assertEqual(manifest["copied_files"], 1)
        restored = Storage(Settings(snapshot / "app.db", snapshot / "raw", worker_enabled=False))
        self.assertTrue(verify(restored)["ok"])
        self.assertEqual(Worker(restored).analysis(record["id"])["analysis"]["format"], "table")
        restored.raw_path(record["id"]).write_bytes(b"corruption")
        self.assertFalse(verify(restored)["ok"])
        with self.assertRaises(FileExistsError):
            backup(self.app.state.storage, snapshot)

    def test_archive_import_blocks_paths_and_retains_archive_and_members(self):
        archive_path = self.root / "test.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("folder/valid.dat", b"a,b\n1,2\n")
            archive.writestr("../../outside.dat", b"escape")
            link = zipfile.ZipInfo("symlink.dat")
            link.external_attr = (0o120777 << 16)
            archive.writestr(link, "outside")
        result = import_sources(self.app.state.storage, [archive_path], expand=True)
        self.assertEqual((result["succeeded"], result["failed"]), (2, 2))
        self.assertFalse((self.root.parent / "outside.dat").exists())
        self.assertTrue(verify(self.app.state.storage)["ok"])

    def test_corrupt_archive_member_does_not_abort_remaining_members(self):
        archive_path = self.root / "corrupt-member.zip"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("bad.dat", b"damaged member contents")
            archive.writestr("good.dat", b"id,value\n1,2\n")
            member = archive.getinfo("bad.dat")
            content_offset = member.header_offset + 30 + len(member.filename.encode()) + len(member.extra)
        data = bytearray(archive_path.read_bytes())
        data[content_offset] ^= 1  # Leave the recorded CRC unchanged.
        archive_path.write_bytes(data)
        result = import_sources(self.app.state.storage, [archive_path], expand=True)
        self.assertEqual((result["succeeded"], result["failed"]), (2, 1))
        saved = [record for record in result["files"] if record["id"]]
        self.assertEqual({record["original_name"] for record in saved}, {archive_path.name, "good.dat"})
        self.assertFalse(list(self.settings.upload_dir.glob("*.part")))
        self.assertTrue(verify(self.app.state.storage)["ok"])

    def test_sqlite_sample_is_read_only(self):
        path = self.root / "source.db"
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE measurements(id TEXT, reading REAL)")
            connection.execute("INSERT INTO measurements VALUES ('001', 1.5)")
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        result = extract(path, self.options)
        self.assertEqual(result["format"], "sqlite")
        self.assertEqual(result["sample"][0]["id"], "001")
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)

    def test_two_storage_instances_share_atomic_quota(self):
        settings = replace(self.settings, max_storage_bytes=8)
        first, second = Storage(settings), Storage(settings)
        def ingest(index):
            storage = first if index % 2 else second
            return storage.ingest(SimpleNamespace(filename="x.dat", file=io.BytesIO(b"1234")))
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(ingest, range(3)))
        self.assertEqual(sum(record["id"] is not None for record in results), 2)
        self.assertEqual(len(list(settings.upload_dir.glob("*.dat"))), 2)

    def test_parser_timeout_and_output_limits_preserve_originals(self):
        original_popen = subprocess.Popen
        for program, code in [("while True: pass", "parser_timeout"),
                              ("import sys; sys.stdout.write('x' * 2097153); sys.stdout.flush()\nwhile True: pass", "output_limit")]:
            record = self.upload(program.encode())
            worker = self.app.state.worker
            worker.options["timeout"] = 1
            def popen(command, **kwargs):
                return original_popen([sys.executable, "-c", program], **kwargs)
            with patch("backend.jobs.subprocess.Popen", side_effect=popen):
                worker.run_once()
            result = worker.analysis(record["id"])["analysis"]
            self.assertEqual(result["status"], "resource_limit")
            self.assertEqual(result["error_code"], code)
            self.assertTrue(verify(self.app.state.storage)["ok"])

    def test_broken_and_entity_xml_is_saved_without_execution(self):
        samples = [b"<rows><unfinished>", b'<!DOCTYPE x [<!ENTITY a SYSTEM "file:///etc/passwd">]><x>&a;</x>']
        for data in samples:
            record = self.upload(data)
            self.assertIsNotNone(record["id"])
            self.app.state.worker.run_once()
            result = self.app.state.worker.analysis(record["id"])["analysis"]
            self.assertEqual(result["status"], "corrupt")
            self.assertNotIn("root:", str(result))

    def test_scientific_notation_matrix_keeps_the_first_row(self):
        result = self.read(b"1e-3 -2.5\n2e-3 4.1\n3e-3 9.2\n")
        self.assertEqual(result["format"], "table")
        self.assertEqual(result["sampling"]["records_sampled"], 3)
        self.assertFalse(result["metadata"]["header_inferred"])
        self.assertEqual(result["schema"]["candidates"][1]["json_schema"]["properties"]["column_1"]["type"], "number")

    def test_unicode_windows_keep_endianness_and_character_boundaries(self):
        text = "编号,温度\n" + "001,21.5\n" * 12000 + "002,待核实\n" * 100
        for encoding, bom in [("utf-8", b""), ("gb18030", b""), ("utf-16-be", b"\xfe\xff"),
                              ("utf-32-be", b"\x00\x00\xfe\xff")]:
            with self.subTest(encoding=encoding):
                result = self.read(bom + text.encode(encoding))
                self.assertEqual(result["format"], "table")
                self.assertEqual(result["sample"][0]["编号"], "001")
                profile = result["schema"]["candidates"][1]["json_schema"]["properties"]["温度"]
                self.assertIn("string", profile["type"])

    def test_commit_acknowledgement_failure_does_not_delete_committed_original(self):
        storage = self.app.state.storage
        connect = storage.connect
        @contextmanager
        def lose_acknowledgement():
            committed_file = False
            with connect() as connection:
                class Proxy:
                    def execute(self, sql, *arguments):
                        nonlocal committed_file
                        committed_file |= sql.startswith("INSERT INTO files")
                        return connection.execute(sql, *arguments)
                yield Proxy()
            if committed_file:
                raise sqlite3.OperationalError("commit acknowledgement lost")
        with patch.object(storage, "connect", lose_acknowledgement), self.assertLogs("backend.storage", level="ERROR"):
            record = self.upload(b"must remain preserved")
        self.assertIsNotNone(record["id"])
        self.assertEqual(storage.raw_path(record["id"]).read_bytes(), b"must remain preserved")
        self.assertTrue(verify(storage)["ok"])

    def test_optional_tika_adapter_streams_to_configured_local_service(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from threading import Thread
        received = []
        class Handler(BaseHTTPRequestHandler):
            def do_PUT(self):
                received.append(self.rfile.read(int(self.headers["Content-Length"])))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"Content from the configured fallback reader")
            def log_message(self, *arguments):
                pass
        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        path = self.root / "proprietary.dat"
        path.write_bytes(b"\x00\xff\x00\x01")
        try:
            options = {**self.options, "tika_url": f"http://127.0.0.1:{server.server_port}"}
            result = extract(path, options)
            self.assertEqual(result["status"], "sampled")
            self.assertEqual(result["metadata"]["fallback_reader"], "apache_tika")
            self.assertEqual(received, [path.read_bytes()])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_native_reader_installation_requeues_missing_dependency_results(self):
        storage = self.app.state.storage
        record = self.upload(b"\x00\x01\xff")
        with patch("backend.jobs.shutil.which", return_value=None):
            unavailable = Worker(storage)
        with patch.object(unavailable, "parse", return_value={"status": "dependency_missing"}):
            unavailable.run_once()
        self.assertEqual(unavailable.analysis(record["id"])["job"]["state"], "completed")
        executable = self.root / "ffprobe"
        executable.write_bytes(b"simulated native reader identity")
        with patch("backend.jobs.shutil.which", side_effect=lambda name: str(executable) if name == "ffprobe" else None):
            installed = Worker(storage)
        self.assertNotEqual(unavailable.policy_key, installed.policy_key)
        installed.refresh_policy()
        self.assertEqual(installed.analysis(record["id"])["job"]["state"], "pending")
        with patch.object(installed, "parse", return_value={"status": "sampled"}):
            installed.run_once()
        self.assertEqual(installed.analysis(record["id"])["analysis"]["status"], "sampled")


if __name__ == "__main__":
    unittest.main()
