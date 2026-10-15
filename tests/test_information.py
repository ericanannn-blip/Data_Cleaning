import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from backend.config import Settings
from backend.extract_local import main as extract_local
from backend.information import InformationSchema, extract_information
from backend.main import create_app
from tests.fixtures import pdf_bytes, zip_bytes


def template():
    return {"name": "Contact", "entity": "person", "fields": [
        {"name": "person_name", "aliases": ["姓名", "Name"], "required": True},
        {"name": "identifier", "aliases": ["编号", "ID"]},
        {"name": "age", "aliases": ["年龄", "Age"], "type": "integer"},
        {"name": "date", "aliases": ["日期", "Date"], "type": "date"},
    ]}


def docx_bytes(text="姓名：张三"):
    xml = f'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>'
    return zip_bytes([("word/document.xml", xml.encode())])


class InformationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = Settings(self.root / "db.sqlite", self.root / "raw")
        self.client = self.enterContext(TestClient(create_app(self.settings)))

    def schema(self, definition=None):
        response = self.client.post("/information/schemas", json=definition or template())
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def upload(self, name, data):
        result = self.client.post("/files", files={"files": (name, data)}).json()
        self.assertEqual(result["succeeded"], 1, result)
        return result["files"][0]

    def extract(self, file, schema):
        response = self.client.post(f'/files/{file["id"]}/extract', params={"schema_id": schema["id"]})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_cross_format_information_family_and_source_evidence(self):
        schema = self.schema()
        for name, data, location in [
            ("text.dat", "姓名：张三\n编号：001\n年龄：20\n".encode(), {"line": 1}),
            ("record.dat", '{"姓名":"张三","ID":"001","Age":20}'.encode(), {"json_pointer": "", "field_pointer": "/姓名"}),
            ("table.csv", 'Name,ID,Age\n张三,001,20\n'.encode(), {"line_end": 2, "field_pointer": "/Name"}),
            ("word.dat", docx_bytes(), {"paragraph": 1, "part": "word/document.xml"}),
        ]:
            file = self.upload(name, data)
            run = self.extract(file, schema)
            self.assertEqual(run["schema_id"], schema["id"])
            field = run["records"][0]["fields"]["person_name"]
            self.assertEqual(field["value"], "张三")
            self.assertEqual(field["evidence"][0]["location"], location)
            self.assertEqual(run["source"]["sha256"], file["sha256"])
            self.assertTrue(run["complete"])
            if name != "word.dat":
                self.assertEqual(run["records"][0]["values"]["identifier"], "001")
                self.assertEqual(run["records"][0]["values"]["age"], 20)
        self.assertEqual(self.client.get("/information/runs").json()["total"], 4)

    def test_missing_conflicting_and_invalid_values_are_never_guessed(self):
        schema = self.schema()
        run = self.extract(self.upload("labels.dat", "姓名：张三\n姓名：李四\n年龄：001\n日期：2026-02-30\n".encode()), schema)
        record = run["records"][0]
        self.assertEqual(record["status"], "review")
        self.assertEqual(record["fields"]["person_name"]["status"], "conflict")
        self.assertIsNone(record["values"]["person_name"])
        self.assertEqual(len(record["fields"]["person_name"]["evidence"]), 2)
        self.assertEqual(record["fields"]["age"]["status"], "invalid_type")
        self.assertEqual(record["fields"]["date"]["status"], "invalid_type")
        missing = self.extract(self.upload("missing.dat", b"ordinary text"), schema)
        self.assertEqual(missing["records"][0]["fields"]["person_name"]["status"], "missing")
        self.assertEqual(missing["records"][0]["status"], "review")

    def test_exact_paths_and_required_json_schema(self):
        definition = {"name": "Nested", "entity": "record", "fields": [
            {"name": "code", "paths": ["/a~1b/~0code"], "required": True},
            {"name": "active", "type": "boolean", "aliases": ["enabled"]},
            {"name": "nested", "paths": ["/items/0/name"]},
        ]}
        schema = self.schema(definition)
        self.assertEqual(schema["json_schema"]["required"], ["code"])
        file = self.upload("nested.json", b'{"a/b":{"~code":"001"},"code":"wrong","enabled":true,"items":[{"name":"Array child"}]}')
        run = self.extract(file, schema)
        self.assertEqual(run["records"][0]["values"], {"code": "001", "active": True, "nested": "Array child"})
        self.assertEqual(run["records"][0]["fields"]["code"]["evidence"][0]["method"], "json_pointer")

    def test_schema_changes_and_runs_are_immutable_and_survive_restart(self):
        initial = self.schema()
        self.assertEqual(self.schema()["id"], initial["id"])
        file = self.upload("one.txt", b"Name: Ada")
        first = self.extract(file, initial)
        definition = template()
        definition["fields"][0]["aliases"].append("Contact")
        changed = self.schema(definition)
        self.assertNotEqual(initial["id"], changed["id"])
        second = self.extract(file, changed)
        with TestClient(create_app(self.settings)) as client:
            self.assertEqual(len(client.get("/information/schemas").json()["schemas"]), 2)
            result = client.get("/information/runs", params={"file_id": file["id"], "limit": 1}).json()
            self.assertEqual((result["total"], result["runs"][0]["id"]), (2, second["id"]))
            older = client.get("/information/runs", params={"schema_id": initial["id"]}).json()["runs"][0]
            self.assertEqual(older, first)
        (self.settings.upload_dir / f'{file["id"]}.dat').write_bytes(b"Name: changed")
        self.assertEqual(self.client.post(f'/files/{file["id"]}/extract', params={"schema_id": initial["id"]}).status_code, 422)
        self.assertEqual(self.client.get("/information/runs").json()["total"], 2)

    def test_schema_validation_rejects_ambiguous_and_unbounded_rules(self):
        cases = [
            {"name": "bad", "entity": "x", "fields": [{"name": "x"}, {"name": "X"}]},
            {"name": "bad", "entity": "x", "fields": [{"name": "x", "aliases": ["Same"]}, {"name": "y", "aliases": ["same"]}]},
            {"name": "bad", "entity": "x", "fields": [{"name": "x", "paths": ["/bad~2"]}]},
            {"name": "bad", "entity": "x", "fields": [{"name": "x", "regex": "(a+)+$"}]},
            {"name": "bad", "entity": "x", "fields": [{"name": "x", "type": "made-up"}]},
        ]
        for definition in cases:
            self.assertEqual(self.client.post("/information/schemas", json=definition).status_code, 422)

    def test_batch_keeps_successes_when_other_readers_fail(self):
        schema = self.schema()
        good = self.upload("good.dat", b"Name: Ada")
        bad = self.upload("bad.dat", b"PK\x03\x04invalid")
        empty = self.upload("empty.dat", b"")
        self.assertEqual(self.client.post(f'/files/{empty["id"]}/extract', params={"schema_id": schema["id"]}).status_code, 422)
        result = self.client.post("/information/extract", json={"schema_id": schema["id"], "file_ids": [good["id"], bad["id"], "missing", good["id"]]}).json()
        self.assertEqual((result["succeeded"], result["failed"]), (1, 2))
        self.assertTrue(result["results"][1]["error"])
        self.assertEqual(self.client.post("/information/extract", json={"schema_id": schema["id"], "file_ids": [good["id"]] * 21}).status_code, 422)
        self.assertEqual(self.client.post(f'/files/{good["id"]}/extract', params={"schema_id": "0" * 32}).status_code, 404)
        (self.settings.upload_dir / f'{bad["id"]}.dat').unlink()
        unavailable = self.client.post("/information/extract", json={"schema_id": schema["id"], "file_ids": [bad["id"], good["id"]]}).json()
        self.assertEqual((unavailable["succeeded"], unavailable["failed"]), (1, 1))

    def test_pdf_text_evidence_and_nonlabel_text_is_not_inferred(self):
        schema = self.schema({"name": "PDF", "entity": "page", "fields": [{"name": "page", "aliases": ["Synthetic page one"], "required": True}]})
        # Use equal-size replacement to keep the PDF fixture structurally intact.
        data = pdf_bytes().replace(b"Synthetic page two", b"page: second value")
        file = self.upload("pages.dat", data)
        run = self.extract(file, schema)
        field = run["records"][0]["fields"]["page"]
        self.assertEqual(field["value"], "second value")
        self.assertEqual(field["evidence"][0]["location"]["page"], 2)

    def test_docx_dtd_and_generic_archives_are_rejected(self):
        schema = self.schema()
        for data in [zip_bytes([("notes.txt", b"Name: Ada")]), zip_bytes([("word/document.xml", b'<!DOCTYPE a [<!ENTITY x "Name: Ada">]><a>&x;</a>')])]:
            file = self.upload("document.dat", data)
            self.assertEqual(self.client.post(f'/files/{file["id"]}/extract', params={"schema_id": schema["id"]}).status_code, 422)

    def test_partial_text_drops_cut_off_values_and_marks_review(self):
        from unittest.mock import patch
        path = self.root / "partial.txt"
        path.write_text("Name: Ada\nAge: 123456789", encoding="utf-8")
        schema = InformationSchema.model_validate(template())
        with patch("backend.information.MAX_TEXT_BYTES", 17):
            run = extract_information(path, path.name, schema)
        self.assertFalse(run["complete"])
        self.assertEqual(run["records"][0]["values"]["person_name"], "Ada")
        self.assertEqual(run["records"][0]["fields"]["age"]["status"], "missing")
        self.assertEqual(run["records"][0]["status"], "review")

    def test_record_budget_invalid_rows_and_repeated_matches(self):
        from unittest.mock import patch
        schema = self.schema()
        file = self.upload("list.json", b'[{"Name":"Ada"},{"Name":"Grace"},{"Name":"Lin"}]')
        with patch("backend.information.MAX_RECORDS", 2):
            run = self.extract(file, schema)
        self.assertEqual(len(run["records"]), 2)
        self.assertFalse(run["complete"])
        self.assertEqual(run["records"][1]["location"], {"json_pointer": "/1"})
        file = self.upload("events.ndjson", b'{"Name":"Ada"}\ninvalid\n{"Name":"Grace"}\n')
        run = self.extract(file, schema)
        self.assertEqual(run["records"][1]["location"], {"line": 3})
        self.assertIn("Invalid JSON at line 2", " ".join(run["notes"]))
        repeated = self.extract(self.upload("repeated.txt", b"Name: Ada\n" * 21), schema)
        self.assertEqual(repeated["records"][0]["fields"]["person_name"]["status"], "conflict")
        self.assertEqual(len(repeated["records"][0]["fields"]["person_name"]["evidence"]), 20)

    def test_local_directory_processing_stays_local_skips_links_and_preserves_inputs(self):
        directory = self.root / "selected"
        directory.mkdir()
        source = directory / "one.dat"
        source.write_bytes(b"Name: Ada")
        outside = self.root / "outside.txt"
        outside.write_bytes(b"Name: Hidden")
        (directory / "linked.dat").symlink_to(outside)
        (directory / "ignored.bin").write_bytes(b"skip")
        nested = directory / "nested"
        nested.mkdir()
        (nested / "two.txt").write_bytes(b"Name: Grace")
        schema_path = directory / "template.json"
        schema_path.write_text(json.dumps(template()), encoding="utf-8")
        output = directory / "results.jsonl"
        with contextlib.redirect_stdout(io.StringIO()):
            status = extract_local(["--root", str(directory), "--schema", str(schema_path), "--output", str(output),
                                    "--recursive", "--include", "*.dat", "--include", "*.txt"])
        self.assertEqual(status, 0)
        results = [json.loads(line) for line in output.read_text().splitlines()]
        self.assertEqual({run["source"]["relative_path"] for run in results}, {"one.dat", "nested/two.txt"})
        self.assertEqual(source.read_bytes(), b"Name: Ada")
        before = output.read_bytes()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            extract_local(["--root", str(directory), "--schema", str(schema_path), "--output", str(output)])
        self.assertEqual(output.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
