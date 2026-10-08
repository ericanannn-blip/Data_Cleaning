"""Content-first classification and reproducible, explicitly bounded schemas.

Samples describe observed data, not a contract for unobserved rows. Normalized
field names and inferred types are proposals; original values stay untouched.
"""

from collections import Counter
import codecs
import csv
from datetime import date, datetime
import hashlib
import io
import json
import math
from pathlib import Path
import random
import re
import unicodedata

from .inspection import reader
from .readers import (ReaderError, image_metadata, pdf_metadata, zip_entries)


CATEGORIES = {**reader.CATEGORY_BY_FORMAT, "csv": "table", "tsv": "table",
              "json": "table", "ndjson": "table", "tiff": "image", "empty": "unknown"}
TEXT_MIMES = {"text": "text/plain", "json": "application/json", "csv": "text/csv",
              "tsv": "text/tab-separated-values", "ndjson": "application/x-ndjson",
              "xml": "application/xml", "html": "text/html"}
NULL_MARKERS = {"", "null", "na", "n/a"}
MAX_SCAN_ROWS = 10_000
MAX_FIELDS = 200


def issue(code, message, count=1, severity="review"):
    return {"code": code, "message": message, "count": count, "severity": severity}


def json_value(text):
    def reject(value):
        raise ValueError("Non-finite JSON number")
    def finite_number(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("Non-finite JSON number")
        return number
    return json.loads(text, parse_constant=reject, parse_float=finite_number)


def decode_prefix(data, complete):
    decoded = reader.decode_text(data)
    if decoded is not None:
        return decoded
    if not complete:
        # A byte budget can cut through a multi-byte codepoint. Decode only
        # complete codepoints, without replacement characters or silent loss.
        for encoding in ("utf-8-sig", "utf-16", "utf-32", "gb18030"):
            try:
                text = codecs.getincrementaldecoder(encoding)().decode(data, final=False)
            except (UnicodeError, ValueError):
                continue
            if reader.is_readable(text) and "\x00" not in text:
                return text, encoding
    return None


def reservoir(rows, limit, seed):
    randomizer = random.Random(seed)
    selected = []
    count = 0
    for count, row in enumerate(rows, 1):
        if count > MAX_SCAN_ROWS:
            return selected, MAX_SCAN_ROWS, True
        if len(selected) < limit:
            selected.append(row)
        else:
            index = randomizer.randrange(count)
            if index < limit:
                selected[index] = row
    return selected, count, False


def scalar_type(value, infer_strings=False):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if not infer_strings:
        return "string"
    cleaned = str(value).strip()
    if cleaned.casefold() in NULL_MARKERS:
        return "null"
    # Leading-zero identifiers are strings, even when they look numeric.
    if re.fullmatch(r"[+-]?(0|[1-9][0-9]*)", cleaned):
        return "integer"
    if re.fullmatch(r"[+-]?(0|[1-9][0-9]*)\.[0-9]+", cleaned):
        return "number"
    if cleaned.casefold() in {"true", "false"}:
        return "boolean"
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", cleaned):
            date.fromisoformat(cleaned)
            return "date"
        if re.match(r"^\d{4}-\d{2}-\d{2}T", cleaned):
            datetime.fromisoformat(cleaned)
            return "datetime"
    except ValueError:
        pass
    return "string"


def pointer(name):
    return str(name).replace("~", "~0").replace("/", "~1")


def flatten(value, path="", depth=0):
    if isinstance(value, dict) and value and depth < 8:
        result = {}
        for key, item in value.items():
            if len(result) >= MAX_FIELDS:
                break
            child = flatten(item, path + "/" + pointer(key), depth + 1)
            for name, nested in child.items():
                if len(result) >= MAX_FIELDS:
                    break
                result[name] = nested
        return result
    return {path or "/value": value}


def summarize(rows, infer_strings=False):
    statistics = {}
    whitespace = 0
    for row in rows:
        for path, value in flatten(row).items():
            if path not in statistics and len(statistics) >= MAX_FIELDS:
                continue
            field = statistics.setdefault(path, {"path": path, "observed": 0,
                "null_count": 0, "types": Counter(), "examples": []})
            field["observed"] += 1
            kind = scalar_type(value, infer_strings)
            field["types"][kind] += 1
            if kind == "null":
                field["null_count"] += 1
            elif len(field["examples"]) < 3:
                example = json.dumps(value, ensure_ascii=False)[:120]
                if example not in field["examples"]:
                    field["examples"].append(example)
            if isinstance(value, str) and value != value.strip():
                whitespace += 1
    issues = []
    fields = []
    used_names = set()
    for path, field in sorted(statistics.items()):
        raw_name = path[1:].replace("~1", "/").replace("~0", "~")
        normalized = re.sub(r"[^\w]+", "_", unicodedata.normalize("NFKC", raw_name).strip()).strip("_").casefold() or "field"
        base = normalized
        suffix = 2
        while normalized in used_names:
            normalized = f"{base}_{suffix}"
            suffix += 1
        if base != normalized:
            issues.append(issue("name_collision", f"Field {path} shares a normalized name; proposed {normalized}."))
        used_names.add(normalized)
        field["normalized_name"] = normalized
        field["types"] = dict(sorted(field["types"].items()))
        field["missing_count"] = len(rows) - field["observed"]
        field["nullable"] = field["null_count"] + field["missing_count"] > 0
        field["null_ratio"] = round((field["null_count"] + field["missing_count"]) / len(rows), 4) if rows else 0
        non_null = set(field["types"]) - {"null"}
        if "number" in non_null:
            non_null.discard("integer")
        if len(non_null) > 1:
            issues.append(issue("type_conflict", f"Field {path} has conflicting observed types: {', '.join(sorted(non_null))}."))
        if field["nullable"]:
            issues.append(issue("missing_values", f"Field {path} has missing or null values in the sample.", field["null_count"] + field["missing_count"]))
        fields.append(field)
    if whitespace:
        issues.append(issue("whitespace", "Values have surrounding whitespace; trimming is a proposed cleaning rule.", whitespace))
    hashes = [hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest() for row in rows]
    duplicates = len(hashes) - len(set(hashes))
    if duplicates:
        issues.append(issue("duplicate_rows", "Repeated rows occur in the sample; check the record grain before deduplicating.", duplicates))
    if len(statistics) >= MAX_FIELDS:
        issues.append(issue("field_limit", "Schema is limited to 200 observed fields; inspect wide or deeply nested data separately."))
    return fields, issues


def _text_rows(text, complete, name, issues):
    stripped = text.lstrip()
    if stripped.startswith(("[", "{")):
        try:
            value = json_value(text)
            return "json", "record" if isinstance(value, list) else "document", value if isinstance(value, list) else [value]
        except (ValueError, RecursionError):
            if stripped.startswith("[") and not complete:
                # Read complete array elements within the byte budget. Never
                # treat a cut-off object/element as a valid record.
                decoder = json.JSONDecoder(parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
                                           parse_float=lambda value: json_value(value))
                rows, position = [], 1
                while len(rows) < MAX_SCAN_ROWS:
                    while position < len(stripped) and stripped[position].isspace():
                        position += 1
                    if position >= len(stripped) or stripped[position] == "]":
                        break
                    try:
                        value, end = decoder.raw_decode(stripped, position)
                    except (ValueError, RecursionError):
                        break
                    # A partial number/string at the budget boundary is not
                    # complete until its delimiter is visible.
                    delimiter = end
                    while delimiter < len(stripped) and stripped[delimiter].isspace():
                        delimiter += 1
                    if delimiter >= len(stripped) or stripped[delimiter] not in ",]":
                        break
                    rows.append(value)
                    if stripped[delimiter] == "]":
                        break
                    position = delimiter + 1
                if rows:
                    return "json", "record", rows
    # Drop an incomplete final line before line-oriented parsing.
    line_text = text if complete or text.endswith(("\n", "\r")) else text.rsplit("\n", 1)[0] if "\n" in text else ""
    lines = [line for line in line_text.splitlines() if line.strip()]
    extension = Path(name).suffix.lower()
    if lines and (extension in {".jsonl", ".ndjson"} or len(lines) > 1 and all(line.lstrip().startswith("{") for line in lines[:3])):
        records, failures = [], 0
        for line in lines[:MAX_SCAN_ROWS]:
            try:
                records.append(json_value(line))
            except (ValueError, RecursionError):
                failures += 1
        if failures:
            issues.append(issue("invalid_records", "Invalid JSON lines were excluded from schema inference and remain in the original.", failures))
        if records:
            return "ndjson", "record", records
    if stripped.startswith(("[", "{")):
        issues.append(issue("structure_unreadable", "The JSON sample is incomplete or malformed; no structured records could be established."))
        return "text", "line", []
    if stripped.startswith("<") and complete:
        try:
            kind, _, _, _ = reader.classify_text(text)
            if kind in {"xml", "html"}:
                return kind, "line", [{"line": line} for line in lines[:MAX_SCAN_ROWS]]
        except (ValueError, RecursionError):
            pass
    if lines:
        try:
            try:
                dialect = csv.Sniffer().sniff(line_text[:8192], delimiters=",\t;|")
            except csv.Error:
                if extension not in {".csv", ".tsv"}:
                    raise
                dialect = csv.excel_tab if extension == ".tsv" else csv.excel
            parsed = csv.reader(io.StringIO(line_text), dialect, strict=True)
            headers = next(parsed)
            if len(headers) >= 2:
                keys, used = [], set()
                for index, header in enumerate(headers):
                    key, suffix = header or f"column_{index + 1}", 2
                    original = key
                    while key in used:
                        key, suffix = f"{original} [{suffix}]", suffix + 1
                    if not header or key != header:
                        issues.append(issue("header_collision", "Empty or duplicate headers received distinct proposed column names."))
                    used.add(key)
                    keys.append(key)
                records, mismatches = [], 0
                try:
                    for values in parsed:
                        if not values:
                            continue
                        if len(records) >= MAX_SCAN_ROWS:
                            break
                        if len(values) != len(keys):
                            mismatches += 1
                            continue
                        records.append(dict(zip(keys, values)))
                except csv.Error:
                    issues.append(issue("invalid_records", "An incomplete or malformed delimited row was excluded."))
                if mismatches:
                    issues.append(issue("row_width", "Rows with a different column count were excluded from inference.", mismatches))
                if records or extension in {".csv", ".tsv"}:
                    return "tsv" if dialect.delimiter == "\t" else "csv", "record", records
        except (csv.Error, StopIteration):
            pass
    if extension in {".json", ".jsonl", ".ndjson", ".csv", ".tsv"}:
        issues.append(issue("structure_unreadable", "The sampled content does not establish the filename's expected structure."))
    return "text", "line", [{"line": line} for line in lines[:MAX_SCAN_ROWS]]


def build_profile(source, name, size, sha256, byte_limit=65536, row_limit=200):
    if isinstance(source, Path):
        with source.open("rb") as stream:
            sample = stream.read(byte_limit)
    else:
        source.seek(0)
        sample = source.read(byte_limit)
        source.seek(0)
    complete = len(sample) == size
    signature = reader.detect_format(sample)
    if sample.startswith((b"II*\x00", b"MM\x00*")):
        signature = ("tiff", "image/tiff", "tiff")
    kind = signature[0] if signature else "unknown"
    profile = {"format": kind, "mime_type": signature[1] if signature else "application/octet-stream",
               "category": CATEGORIES.get(kind, "unknown"), "reader": "hex",
               "grain": "unknown", "encoding": None, "metadata": {}, "fields": [],
               "issues": [], "rows": [], "preview_text": sample[:512].hex(" "),
               "sampling": {"analyzer_version": "2.0", "method": "seeded_reservoir_within_bounded_prefix", "seed": sha256[:16],
                            "byte_limit": byte_limit, "row_limit": row_limit, "bytes_read": len(sample),
                            "source_bytes": size, "source_sha256": sha256, "scanned_rows": 0,
                            "sampled_rows": 0, "complete": False}}
    rows = []
    try:
        if not sample:
            profile.update(format="empty", preview_text="")
            profile["issues"].append(issue("empty_file", "Empty file has no content schema."))
        elif profile["category"] == "image":
            profile.update(reader="image", grain="image", metadata=image_metadata(source))
            rows = [profile["metadata"]]
            complete = True  # Metadata describes this image, not its pixels.
        elif kind == "pdf":
            profile.update(category="document", reader="pdf", grain="document", metadata=pdf_metadata(source))
            rows = [profile["metadata"]]
            complete = True
        elif kind == "zip":
            entries = zip_entries(source)
            profile.update(reader="archive", grain="archive_entry", entries=entries,
                           metadata={"entries": len(entries), "expanded_bytes": sum(entry["size"] for entry in entries)})
            rows = [{key: value for key, value in entry.items() if key != "index"} for entry in entries]
            blocked = sum(bool(entry["blocked"]) for entry in entries)
            if blocked:
                profile["issues"].append(issue("blocked_members", "Some archive members exceed reading limits or have unsafe paths.", blocked))
            complete = True
        elif not signature:
            decoded = decode_prefix(sample, complete)
            if decoded:
                text, encoding = decoded
                kind, grain, rows = _text_rows(text, complete, name, profile["issues"])
                profile.update(format=kind, mime_type=TEXT_MIMES[kind], category=CATEGORIES[kind], reader="table" if kind in {"json", "csv", "tsv", "ndjson"} else "text",
                               grain=grain, encoding=encoding, preview_text=text[:12000])
            else:
                profile["issues"].append(issue("unknown_format", "Content format requires manual review; hexadecimal preview is available."))
        else:
            profile["issues"].append(issue("reader_unavailable", "Format identified by its signature; this version provides a hexadecimal preview."))
    except (ReaderError, OSError, ValueError, SyntaxError) as exc:
        profile["reader"] = "hex"
        profile["issues"].append(issue("reader_error", str(exc) if isinstance(exc, ReaderError) else "Content could not be read; the original is retained."))
    selected, count, capped = reservoir(rows, row_limit, int(sha256[:16] or "0", 16))
    profile["fields"], quality_issues = summarize(selected, profile["format"] in {"csv", "tsv"})
    profile["issues"].extend(quality_issues)
    profile["sampling"].update(scanned_rows=count, sampled_rows=len(selected), complete=complete and not capped and count <= row_limit)
    if profile["reader"] in {"image", "pdf", "archive"}:
        profile["sampling"].update(method="bounded_file_metadata", bytes_read=size, byte_limit=size)
    if not profile["sampling"]["complete"]:
        profile["issues"].append(issue("partial_sample", "Statistics describe the bounded sample; later rows may contain additional fields or types.", severity="info"))
    profile["rows"] = [flatten(row) for row in selected[:50]]
    profile["cleaning_rules"] = {"preserve_original": True, "field_names": "NFKC, trim, lowercase, underscores; collisions get distinct suffixes",
                                 "null_markers": sorted(NULL_MARKERS), "null_marker_scope": "CSV and TSV only",
                                 "type_inference": "CSV/TSV string types are proposals; JSON primitive types are preserved",
                                 "leading_zero_identifiers": "preserve as strings",
                                 "deduplication": "review sample duplicates against record grain before removal"}
    return profile


def fingerprint(profile):
    if not profile["fields"]:
        return None
    # Conservative grouping: matching format, grain and exact original paths.
    # Re-sampling a file keeps its existing family and records field drift.
    shape = [profile["format"], profile["grain"], sorted(field["path"] for field in profile["fields"])]
    return hashlib.sha256(json.dumps(shape, ensure_ascii=False).encode()).hexdigest()[:24]


def schema_definition(profiles):
    combined = {}
    for profile in profiles:
        for field in profile["fields"]:
            entry = combined.setdefault(field["path"], {"path": field["path"],
                "normalized_name": field["normalized_name"], "types": set(), "nullable": False})
            entry["types"].update(field["types"])
            entry["nullable"] |= field["nullable"]
    for path, entry in combined.items():
        entry["nullable"] |= any(path not in {field["path"] for field in p["fields"]} for p in profiles)
        entry["types"] = sorted(entry["types"])
    return {"format": profiles[0]["format"], "category": profiles[0]["category"],
            "grain": profiles[0]["grain"], "fields": list(sorted(combined.values(), key=lambda item: item["path"])),
            "status": "candidate", "scope": "observed samples", "source_files": len(profiles),
            "sampled_rows": sum(p["sampling"]["sampled_rows"] for p in profiles)}


def schema_drift(previous, current):
    before = {field["path"]: field for field in previous.get("fields", [])}
    after = {field["path"]: field for field in current.get("fields", [])}
    return {"added_fields": sorted(after.keys() - before.keys()), "removed_fields": sorted(before.keys() - after.keys()),
            "changed_fields": sorted(path for path in before.keys() & after.keys() if before[path] != after[path])}
