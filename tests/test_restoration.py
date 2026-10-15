import bz2
import gzip
import hashlib
import io
import lzma
from pathlib import Path
import tempfile
import unittest
import zlib

from fastapi.testclient import TestClient
from PIL import Image

from backend.config import Settings
from backend.main import create_app
from tests.fixtures import image_bytes, pdf_bytes, zip_bytes


class RestorationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = Settings(self.root / "app.db", self.root / "raw")
        self.client = self.enterContext(TestClient(create_app(self.settings)))

    def upload(self, name, data):
        result = self.client.post("/files", files={"files": (name, data)}).json()
        self.assertEqual(result["succeeded"], 1, result)
        return result["files"][0]

    def restore(self, record):
        response = self.client.post(f'/files/{record["id"]}/restore')
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_original_image_bytes_have_correct_inline_mime_and_download_name(self):
        for format in ("JPEG", "PNG", "GIF", "WEBP", "BMP", "TIFF"):
            data = image_bytes(format)
            record = self.upload("图片.dat", data)
            restored = self.restore(record)
            content = self.client.get("/" + restored["content_url"])
            self.assertEqual(content.content, data)
            self.assertEqual(content.headers["content-type"], restored["mime_type"])
            self.assertTrue(content.headers["content-disposition"].startswith("inline;"))
            self.assertEqual(restored["transforms"], [])
            self.assertTrue(restored["inline"])
            download = self.client.get("/" + restored["download_url"])
            self.assertTrue(download.headers["content-disposition"].startswith("attachment;"))
            self.assertIn("filename*=utf-8''", download.headers["content-disposition"])
            self.assertEqual(download.content, data)
            image = self.client.get(f'/files/{record["id"]}/restored/image')
            self.assertEqual(image.headers["content-type"], "image/png")
            with Image.open(io.BytesIO(image.content)) as preview:
                self.assertEqual(preview.size, (32, 24))

    def test_xor_recovery_preserves_upload_and_restores_exact_image(self):
        for format in ("JPEG", "PNG", "WEBP", "GIF"):
            data = image_bytes(format)
            encoded = bytes(byte ^ 0xA3 for byte in data)
            record = self.upload("encoded.dat", encoded)
            restored = self.restore(record)
            self.assertEqual(restored["transforms"], ["xor:0xa3"])
            self.assertEqual(self.client.get("/" + restored["content_url"]).content, data)
            partial = self.client.get("/" + restored["content_url"], headers={"Range": "bytes=0-4"})
            self.assertEqual(partial.status_code, 206)
            self.assertEqual(partial.content, data[:5])
            self.assertEqual(restored["sha256"], hashlib.sha256(data).hexdigest())
            self.assertEqual(restored["source_sha256"], hashlib.sha256(encoded).hexdigest())
            self.assertEqual((self.settings.upload_dir / f'{record["id"]}.dat').read_bytes(), encoded)

    def test_compressed_pdf_recovers_and_reads_both_pages(self):
        for kind, compress in [("gzip", gzip.compress), ("bzip2", bz2.compress), ("xz", lzma.compress), ("zlib", zlib.compress)]:
            data = pdf_bytes()
            record = self.upload("paper.dat", compress(data))
            restored = self.restore(record)
            self.assertEqual(restored["format"], "pdf")
            self.assertEqual(restored["transforms"], [kind])
            self.assertEqual(self.client.get("/" + restored["content_url"]).content, data)
            base = f'/files/{record["id"]}/restored/pages/'
            self.assertIn("Synthetic page two", self.client.get(base + "2").json()["text"])
            self.assertEqual(self.client.get(base + "2/image").headers["content-type"], "image/png")
            self.assertEqual(self.client.get(base + "3").status_code, 422)

    def test_compressed_xor_and_nested_streams(self):
        data = image_bytes()
        wrapped = gzip.compress(bytes(byte ^ 42 for byte in data))
        result = self.restore(self.upload("wrapped.dat", wrapped))
        self.assertEqual(result["transforms"], ["gzip", "xor:0x2a"])
        result = self.restore(self.upload("nested.dat", gzip.compress(bz2.compress(data))))
        self.assertEqual(result["transforms"], ["gzip", "bzip2"])

    def test_failed_unknown_damaged_truncated_and_extra_streams_leave_no_artifact(self):
        data = pdf_bytes()
        invalid = [b"", b"\x00\xff\x01", b"%PDF-1.4\nbroken", b"\xff\xd8\xffgarbage\xff\xd9",
                   gzip.compress(data)[:-4], bz2.compress(data)[:-3], lzma.compress(data)[:-3],
                   gzip.compress(data) + gzip.compress(b"second"), gzip.compress(gzip.compress(gzip.compress(gzip.compress(data))))]
        for source in invalid:
            record = self.upload("invalid.dat", source)
            response = self.client.post(f'/files/{record["id"]}/restore')
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(self.client.get(f'/files/{record["id"]}/restored').status_code, 404)
            self.assertEqual((self.settings.upload_dir / f'{record["id"]}.dat').read_bytes(), source)
        self.assertEqual(list(self.settings.restored_dir.iterdir()), [])

    def test_expansion_and_combined_storage_quota(self):
        settings = Settings(self.settings.database_path, self.settings.upload_dir, max_restored_bytes=100)
        record = self.upload("large.dat", gzip.compress(b"a" * 1000))
        with TestClient(create_app(settings)) as client:
            self.assertEqual(client.post(f'/files/{record["id"]}/restore').status_code, 422)
        data = image_bytes()
        record = self.upload("image.dat", data)
        used_raw = sum(path.stat().st_size for path in self.settings.upload_dir.glob("*.dat"))
        settings = Settings(self.settings.database_path, self.settings.upload_dir, max_storage_bytes=used_raw + len(data))
        with TestClient(create_app(settings)) as client:
            self.assertEqual(client.post(f'/files/{record["id"]}/restore').status_code, 200)
            failed = client.post("/files", files={"files": ("extra.dat", b"x")}).json()
            self.assertEqual(failed["failed"], 1)
            another = client.post(f'/files/{self.client.get("/files").json()["files"][-1]["id"]}/restore')
            self.assertEqual(another.status_code, 422)

    def test_cache_restart_missing_artifact_regeneration_and_source_integrity(self):
        record = self.upload("saved.dat", image_bytes())
        restored = self.restore(record)
        self.assertEqual(self.restore(record), restored)
        with TestClient(create_app(self.settings)) as client:
            self.assertEqual(client.get(f'/files/{record["id"]}/restored').json(), restored)
            self.assertEqual(client.get("/" + restored["content_url"]).content, image_bytes())
        artifact = self.settings.restored_dir / f'{record["id"]}.bin'
        artifact.write_bytes(b"corrupt")
        self.assertEqual(self.client.get("/" + restored["content_url"]).status_code, 422)
        regenerated = self.restore(record)
        self.assertEqual(regenerated["sha256"], restored["sha256"])
        artifact.unlink()
        self.restore(record)
        (self.settings.upload_dir / f'{record["id"]}.dat').write_bytes(b"different")
        self.assertEqual(self.client.post(f'/files/{record["id"]}/restore').status_code, 422)

    def test_non_preview_text_is_downloaded_and_archives_keep_bounded_readers(self):
        record = self.upload("untrusted.dat", b"<html><script>alert(1)</script></html>")
        restored = self.restore(record)
        self.assertFalse(restored["inline"])
        response = self.client.get("/" + restored["content_url"])
        self.assertTrue(response.headers["content-disposition"].startswith("attachment;"))
        self.assertIn("sandbox", response.headers["content-security-policy"])
        archive = self.upload("archive.dat", gzip.compress(zip_bytes([("image.png", image_bytes()), ("../../outside", b"blocked")])))
        self.restore(archive)
        base = f'/files/{archive["id"]}/restored'
        self.assertEqual(self.client.get(base + "/image?entry=0").status_code, 200)
        self.assertEqual(self.client.get(base + "/archive/1/profile").status_code, 422)

    def test_missing_ids_and_file_paths_are_not_exposed(self):
        for path in ("/files/missing/restore",):
            self.assertEqual(self.client.post(path).status_code, 404)
        record = self.upload("../../test.dat", image_bytes())
        self.assertEqual(self.client.get(f'/files/{record["id"]}/restored/content').status_code, 404)
        result = self.restore(record)
        self.assertNotIn(str(self.root), str(result))
        self.assertEqual(self.client.get(f'/restored/{record["id"]}.bin').status_code, 404)


if __name__ == "__main__":
    unittest.main()
