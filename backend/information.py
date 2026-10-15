"""Explicit, local information extraction with inspectable source evidence.

Templates group information independently of container format. No model, OCR,
network call, arbitrary regex, or implicit business interpretation is involved.
"""

from datetime import date
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import re
import unicodedata

from defusedxml.ElementTree import fromstring
from defusedxml.common import DefusedXmlException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Literal
from xml.etree.ElementTree import ParseError

from .inspection import reader
from .profiling import decode_prefix, flatten, json_value
from .readers import ReaderError, pdf_document, zip_entries, zip_member


MAX_TEXT_BYTES = 1024 * 1024
MAX_RECORDS = 2000
MAX_PAGES = 20
MAX_PAGE_CHARS = 8000
MAX_MATCHES = 20


def normalize(value):
    return unicodedata.normalize("NFKC", value).strip().casefold()


class InformationField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80, pattern=r"^[\w-]+$")
    type: Literal["string", "integer", "number", "boolean", "date"] = "string"
    aliases: list[str] = Field(default_factory=list, max_length=20)
    paths: list[str] = Field(default_factory=list, max_length=20)
    required: bool = False

    @model_validator(mode="after")
    def validate_selectors(self):
        if any(not value.strip() or len(value) > 200 for value in self.aliases):
            raise ValueError("Aliases must contain 1–200 characters.")
        if any(not path.startswith("/") or len(path) > 500 or re.search(r"~(?![01])", path)
               for path in self.paths):
            raise ValueError("Paths must be escaped JSON Pointers, starting with /.")
        return self


class InformationSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    entity: str = Field(min_length=1, max_length=120)
    fields: list[InformationField] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def unique_fields(self):
        if not self.name.strip() or not self.entity.strip():
            raise ValueError("Name and entity cannot be blank.")
        names = [normalize(field.name) for field in self.fields]
        if len(set(names)) != len(names):
            raise ValueError("Field names must be unique after normalization.")
        owners = {}
        for field in self.fields:
            for alias in [field.name, *field.aliases]:
                key = normalize(alias)
                if key in owners and owners[key] != field.name:
                    raise ValueError("The same alias cannot select different information fields.")
                owners[key] = field.name
        return self

    def definition(self):
        return self.model_dump()

    def key(self):
        canonical = json.dumps(self.definition(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:32]

    def json_schema(self):
        return {"$schema": "https://json-schema.org/draft/2020-12/schema", "title": self.name,
                "type": "object", "additionalProperties": False,
                "properties": {field.name: ({"type": "string", "format": "date"} if field.type == "date"
                                           else {"type": field.type}) for field in self.fields},
                "required": [field.name for field in self.fields if field.required]}


def _docx_units(path):
    entries = zip_entries(path)
    matches = [entry for entry in entries if entry["name"] == "word/document.xml"]
    if not matches:
        raise ReaderError("Archive has no DOCX main document; select an archive member separately.")
    _, data = zip_member(path, matches[0]["index"])
    try:
        root = fromstring(data)
    except (ParseError, DefusedXmlException) as exc:
        raise ReaderError("DOCX document XML could not be read safely.") from exc
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    units, consumed, limited = [], 0, False
    paragraphs = root.iter(ns + "p")
    for number, paragraph in enumerate(paragraphs, 1):
        text = "".join(node.text or "" for node in paragraph.iter(ns + "t"))
        if consumed + len(text) > MAX_TEXT_BYTES or number > MAX_RECORDS:
            limited = True
            break
        consumed += len(text)
        units.append((text, {"paragraph": number, "part": "word/document.xml"}))
    return units, not limited


def _pdf_units(path):
    units, complete = [], True
    with pdf_document(path) as document:
        complete = len(document) <= MAX_PAGES
        for index in range(min(len(document), MAX_PAGES)):
            page = document[index]
            try:
                text_page = page.get_textpage()
                try:
                    count = text_page.count_chars()
                    text = text_page.get_text_range(0, min(count, MAX_PAGE_CHARS)) if count else ""
                    if count > MAX_PAGE_CHARS:
                        # Do not extract a value cut off by the character budget.
                        text = text.rsplit("\n", 1)[0] if "\n" in text else ""
                        complete = False
                    for line, value in enumerate(text.splitlines(), 1):
                        units.append((value, {"page": index + 1, "line": line}))
                finally:
                    text_page.close()
            finally:
                page.close()
    return units, complete


def _read_content(path, name, size):
    with path.open("rb") as stream:
        data = stream.read(MAX_TEXT_BYTES)
    if not data:
        raise ReaderError("Empty file has no information to extract.")
    complete = len(data) == size
    signature = reader.detect_format(data)
    if signature:
        kind = signature[0]
        if kind == "pdf":
            units, complete = _pdf_units(path)
            return kind, [], units, complete, []
        if kind == "zip":
            units, complete = _docx_units(path)
            return "docx", [], units, complete, ["Only the DOCX main body is inspected; headers, notes and attachments are excluded."]
        raise ReaderError("This format has no supported information reader. Images and scanned pages require OCR.")
    decoded = decode_prefix(data, complete)
    if not decoded:
        raise ReaderError("No readable text was found; binary documents require another reader or OCR.")
    text, _ = decoded
    notes, records, units = [], [], []
    # Partial text lines are excluded, including an incomplete final field value.
    bounded_text = text if complete or text.endswith(("\n", "\r")) else text.rsplit("\n", 1)[0] if "\n" in text else ""
    extension = Path(name).suffix.casefold()
    stripped = text.lstrip()
    if extension in {".jsonl", ".ndjson"} or (stripped.startswith("{") and len(bounded_text.splitlines()) > 1
                                               and all(line.lstrip().startswith("{") for line in bounded_text.splitlines() if line.strip())):
        kind = "ndjson"
        for number, line in enumerate(bounded_text.splitlines(), 1):
            if not line.strip():
                continue
            if len(records) >= MAX_RECORDS:
                complete = False
                break
            try:
                value = json_value(line)
            except (ValueError, RecursionError):
                notes.append(f"Invalid JSON at line {number} was excluded.")
                continue
            records.append((flatten(value), {"line": number}, value))
    elif stripped.startswith(("{", "[")):
        kind = "json"
        if not complete:
            raise ReaderError("JSON exceeds the 1 MiB extraction budget; split it or use NDJSON. No partial JSON values were extracted.")
        try:
            value = json_value(text)
        except (ValueError, RecursionError) as exc:
            raise ReaderError("JSON content could not be parsed; no values were guessed.") from exc
        values = value if isinstance(value, list) else [value]
        complete = len(values) <= MAX_RECORDS
        for index, item in enumerate(values[:MAX_RECORDS]):
            records.append((flatten(item), {"json_pointer": f"/{index}" if isinstance(value, list) else ""}, item))
    else:
        kind = "text"
        lines = bounded_text.splitlines()
        try:
            dialect = csv.Sniffer().sniff(bounded_text[:8192], delimiters=",\t;|")
        except csv.Error:
            dialect = None
        if extension in {".csv", ".tsv"} or dialect:
            kind = "tsv" if extension == ".tsv" or dialect and dialect.delimiter == "\t" else "csv"
            table = csv.reader(io.StringIO(bounded_text, newline=""), dialect=dialect or "excel",
                               **({"delimiter": "\t"} if extension == ".tsv" else {}), strict=True)
            try:
                keys = next(table, [])
                if not keys or len(set(keys)) != len(keys) or len(keys) > 200:
                    raise ReaderError("Table headers must be unique and contain at most 200 fields.")
                for values in table:
                    if len(records) >= MAX_RECORDS:
                        complete = False
                        break
                    if not values:
                        continue
                    if len(values) != len(keys):
                        notes.append(f"Invalid table row ending at line {table.line_num} was excluded.")
                        continue
                    original = dict(zip(keys, values))
                    records.append((flatten(original), {"line_end": table.line_num}, original))
            except csv.Error:
                notes.append("Malformed or cut-off table row was excluded.")
                complete = False
        else:
            complete = complete and len(lines) <= 10000
            units = [(line, {"line": index}) for index, line in enumerate(lines[:10000], 1)]
    return kind, records, units, complete, notes


def _convert(raw, kind):
    if raw is None:
        raise ValueError("null value")
    text = str(raw).strip()
    if kind == "string":
        if isinstance(raw, (dict, list)) or isinstance(raw, bool):
            raise ValueError("not a scalar string")
        if not text:
            raise ValueError("empty string")
        return text
    if kind == "boolean":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and normalize(raw) in {"true", "false"}:
            return normalize(raw) == "true"
        raise ValueError("expected true or false")
    if kind == "date":
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            raise ValueError("expected ISO date")
        return date.fromisoformat(text).isoformat()
    if isinstance(raw, bool):
        raise ValueError("boolean is not numeric")
    if kind == "integer":
        if not re.fullmatch(r"[+-]?(0|[1-9][0-9]*)", text):
            raise ValueError("expected integer; leading-zero identifiers stay strings")
        return int(text)
    if not re.fullmatch(r"[+-]?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?", text):
        raise ValueError("expected number")
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("expected finite number")
    return value


def _field_result(field, candidates, overflow=False):
    evidence, converted, invalid = [], [], False
    for raw, location, method, excerpt in candidates:
        try:
            value = _convert(raw, field.type)
            error = None
            converted.append(value)
        except (ValueError, OverflowError):
            value, error, invalid = None, f"Expected {field.type}; review original value.", True
        evidence.append({"raw_value": raw, "value": value, "location": location,
                         "method": method, "excerpt": excerpt[:500], "error": error})
    distinct = {json.dumps(value, ensure_ascii=False, sort_keys=True) for value in converted}
    status = ("conflict" if overflow or len(distinct) > 1 else "invalid_type" if invalid
              else "found" if converted else "missing")
    return {"value": converted[0] if status == "found" else None, "status": status,
            "required": field.required, "evidence": evidence,
            "review": status in {"conflict", "invalid_type"} or field.required and status == "missing"}


def _resolve_pointer(value, pointer):
    for token in pointer[1:].split("/"):
        key = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and key in value:
            value = value[key]
        elif isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]*", key) and len(key) < 10 and int(key) < len(value):
            value = value[int(key)]
        else:
            return False, None
    return True, value


def extract_information(path: Path, name: str, schema: InformationSchema, expected_sha256=None):
    # Hash in chunks; do not trust metadata or read the whole file into memory.
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(65536):
            digest.update(chunk)
    checksum = digest.hexdigest()
    if expected_sha256 and checksum != expected_sha256:
        raise ReaderError("Stored original differs from its upload hash. Restore storage before extraction.")
    kind, records, units, complete, notes = _read_content(path, name, path.stat().st_size)
    output = []
    if not records and kind in {"text", "pdf", "docx"}:
        records = [(None, {}, None)]
    for ordinal, (row, record_location, original) in enumerate(records, 1):
        fields = {}
        for field in schema.fields:
            candidates = []
            aliases = {normalize(alias) for alias in [field.name, *field.aliases]}
            if row is not None:
                selected = []
                if field.paths:
                    for pointer in dict.fromkeys(field.paths):
                        found, raw = _resolve_pointer(original, pointer)
                        if found:
                            selected.append((pointer, raw))
                else:
                    selected = row.items()
                for pointer, raw in selected:
                    leaf = pointer.rsplit("/", 1)[-1].replace("~1", "/").replace("~0", "~")
                    # Explicit paths take precedence over label aliases for structured records.
                    if field.paths or normalize(leaf) in aliases:
                        candidates.append((raw, {**record_location, "field_pointer": pointer},
                                           "json_pointer" if field.paths else "field_alias", json.dumps(raw, ensure_ascii=False)[:500]))
            else:
                for text, location in units:
                    match = re.match(r"^\s*(.{1,200}?)\s*[:：=]\s*(.*?)\s*$", text)
                    if match and normalize(match[1]) in aliases:
                        candidates.append((match[2], location, "label_value", text[:500]))
                        if len(candidates) > MAX_MATCHES:
                            break
            fields[field.name] = _field_result(field, candidates[:MAX_MATCHES], len(candidates) > MAX_MATCHES)
        output.append({"record": ordinal, "location": record_location, "fields": fields,
                       "values": {key: item["value"] for key, item in fields.items()},
                       "status": "review" if any(item["review"] for item in fields.values())
                       or not any(item["status"] == "found" for item in fields.values()) else "extracted"})
    if not complete:
        notes.append("Extraction covers a bounded prefix; uninspected content can contain additional or conflicting values.")
    if kind == "pdf" and not units:
        notes.append("No PDF text layer found. OCR is required for scanned pages.")
    # A sampled document is always reviewable even when all requested fields were found.
    if not complete:
        for record in output:
            record["status"] = "review"
    return {"schema_id": schema.key(), "entity": schema.entity, "schema": schema.definition(),
            "json_schema": schema.json_schema(), "source": {"name": name, "sha256": checksum, "format": kind},
            "extractor_version": "1.0", "complete": complete, "notes": notes[:100], "records": output,
            "limits": {"text_bytes": MAX_TEXT_BYTES, "records": MAX_RECORDS, "pdf_pages": MAX_PAGES,
                       "pdf_chars_per_page": MAX_PAGE_CHARS, "matches_per_field": MAX_MATCHES,
                       "alias_object_fields": 200, "alias_object_depth": 8}}
