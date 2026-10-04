import contextlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from dat_reader import SCHEMA_VERSION, decode_bytes, run, scan_dat_files


class DatReaderTests(unittest.TestCase):
    def test_text_and_structured_content(self):
        result = decode_bytes('{"中文":"内容"}'.encode("gb18030"))
        self.assertEqual(result["format"], "json")
        self.assertEqual(result["content"], {"中文": "内容"})
        self.assertEqual(result["encoding"], "gb18030")
        self.assertEqual(decode_bytes(b"80")["content"], "80")
        self.assertEqual(decode_bytes(b"x^ plain text")["content"], "x^ plain text")

    def test_xor_image_is_restored_and_saved(self):
        image = b"\x89PNG\r\n\x1a\n" + bytes(range(24))
        encrypted = bytes(byte ^ 0x5A for byte in image)
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "input"
            source.mkdir()
            (source / "picture.dat").write_bytes(encrypted)
            output = Path(temp) / "output"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = run(source, output)
            console = json.loads(stdout.getvalue())
            record = json.loads((output / "manifest.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 0)
            self.assertEqual(console["scanned_files"], 1)
            self.assertEqual(record["category"], "image")
            self.assertEqual(record["format"], "png")
            self.assertEqual(record["transform"], ["xor:0x5a"])
            self.assertEqual((output / record["decoded_file"]).read_bytes(), image)

    def test_gzip_text(self):
        import gzip
        import zlib

        result = decode_bytes(gzip.compress("解压成功".encode("utf-8")))
        self.assertEqual(result["content"], "解压成功")
        self.assertEqual(result["transform"], ["gzip"])
        result = decode_bytes(zlib.compress(b"zlib works"))
        self.assertEqual(result["content"], "zlib works")
        self.assertEqual(result["transform"], ["zlib"])

    def test_docx_text(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("word/document.xml",
                             '<w:document xmlns:w="urn:w"><w:body><w:p><w:r>'
                             '<w:t>第一段</w:t></w:r></w:p><w:p><w:r>'
                             '<w:t>第二段</w:t></w:r></w:p></w:body></w:document>')
        result = decode_bytes(buffer.getvalue())
        self.assertEqual(result["format"], "docx")
        self.assertEqual(result["content"], "第一段\n第二段")

    def test_scan_ignores_non_dat_and_limits_expansion(self):
        import gzip

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "a.DAT").write_text("hello")
            (root / "b.txt").write_text("ignored")
            self.assertEqual(scan_dat_files(root), [root / "a.DAT"])
        with self.assertRaisesRegex(ValueError, "超过"):
            decode_bytes(gzip.compress(b"A" * 1000), max_bytes=100)

    def test_multiple_inputs_manifest_and_summary(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            (first / "same.dat").write_text("甲", encoding="utf-8")
            (second / "same.dat").write_text("乙", encoding="utf-8")
            (second / "unknown.dat").write_bytes(b"\x00\x01\x02\x03")
            output = root / "results"
            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = run([first, second, first / "same.dat"], output)
            self.assertEqual(exit_code, 0)
            records = [json.loads(line) for line in
                       (output / "manifest.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(records), 3)
            self.assertTrue(all(record["schema_version"] == SCHEMA_VERSION for record in records))
            text_records = [record for record in records if record["format"] == "text"]
            self.assertEqual(len(text_records), 2)
            self.assertNotEqual(text_records[0]["content_file"], text_records[1]["content_file"])
            self.assertEqual({(output / record["content_file"]).read_text(encoding="utf-8")
                              for record in text_records}, {"甲", "乙"})
            unknown = next(record for record in records if record["status"] == "unsupported")
            self.assertEqual(unknown["header_hex"], "00010203")
            summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["scanned_files"], 3)
            self.assertEqual(summary["category_counts"], {"text": 2, "unknown": 1})
            self.assertEqual(summary["status_counts"], {"ok": 2, "unsupported": 1})

    def test_large_known_binary_streams_past_memory_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "large.dat"
            data = b"%PDF-1.4\n" + b"A" * 4096
            source.write_bytes(data)
            output = root / "results"
            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = run(source, output, max_bytes=32)
            record = json.loads((output / "manifest.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 0)
            self.assertEqual(record["format"], "pdf")
            self.assertEqual(record["category"], "document")
            self.assertEqual((output / record["decoded_file"]).read_bytes(), data)


if __name__ == "__main__":
    unittest.main()
