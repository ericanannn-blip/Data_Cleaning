"""Three non-destructive, content-derived schema candidates from bounded samples."""

import hashlib
import json
import math
import re
import unicodedata
from collections import Counter
from datetime import datetime

VERSION = "1.0"
NULL_STRINGS = {"", "null", "none", "na", "n/a", "nan"}


def json_type(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number" if math.isfinite(value) else "null"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, (list, tuple)):
        return "array"
    return "string"


def analytical_type(value):
    native = json_type(value)
    if native != "string":
        return native, None
    text = value.strip()
    if text.lower() in NULL_STRINGS:
        return "null", None
    # Identifiers with leading zeroes must not be coerced into measures.
    if re.fullmatch(r"-?(0|[1-9]\d*)", text):
        return "integer", None
    if re.fullmatch(r"[+-]?(?:(?:0|[1-9]\d*)(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", text):
        return "number", None
    if text.lower() in {"true", "false"}:
        return "boolean", None
    if re.match(r"^\d{4}-\d{2}-\d{2}(?:$|[T ])", text):
        try:
            datetime.fromisoformat(text.replace("Z", "+00:00"))
            return "string", "date" if len(text) == 10 else "date-time"
        except ValueError:
            pass
    return "string", None


def normalized_name(name: str):
    name = unicodedata.normalize("NFKC", name).strip().lower()
    return re.sub(r"\W+", "_", name, flags=re.UNICODE).strip("_") or "column"


def type_union(types):
    types = set(types)
    if "number" in types:
        types.discard("integer")
    values = sorted(types or {"null"})
    return values[0] if len(values) == 1 else values


def infer_schemas(records: list[dict], family: str, sampling: dict, warnings: list[str]) -> dict:
    count = len(records)
    columns = sorted({str(key) for record in records for key in record})[:512]
    raw, tidy, roles, profiles = {}, {}, {}, {}
    used_names = set()
    for column in columns:
        values = [record.get(column) for record in records]
        observed = Counter(json_type(v) for v in values)
        interpreted = [analytical_type(v) for v in values]
        nulls = sum(kind == "null" for kind, _ in interpreted)
        formats = {fmt for _, fmt in interpreted if fmt}
        name = normalized_name(column)
        base, suffix = name, 2
        while name in used_names:
            name, suffix = f"{base}_{suffix}", suffix + 1
        used_names.add(name)
        raw[column] = {"type": type_union(observed), "x-observed-types": dict(observed)}
        types = [kind for kind, _ in interpreted]
        tidy[name] = {"type": type_union(types), "x-source-column": column,
                      "x-transform": "proposed; originals and sample values are unchanged"}
        if len(formats) == 1 and all(kind == "null" or fmt for kind, fmt in interpreted):
            tidy[name]["format"] = next(iter(formats))
        usable = [v for v, (kind, _) in zip(values, interpreted) if kind != "null"]
        unique = len({json.dumps(v, sort_keys=True, ensure_ascii=False, default=str) for v in usable})
        key_hint = bool(re.search(r"(^|_)(id|uuid|key|编号|序号)($|_)", name))
        if key_hint:
            role = "identifier"
        elif formats:
            role = "time"
        elif set(types) <= {"integer", "number", "null"}:
            role = "measure"
        elif family in {"document", "media_asset"} and "text" in name:
            role = "text"
        else:
            role = "dimension"
        roles[column] = {"role": role, "candidate_key_in_sample": bool(key_hint and nulls == 0 and unique == count)}
        profiles[column] = {"observed_types": dict(observed), "sample_null_count": nulls,
                            "sample_null_rate": round(nulls / count, 4) if count else None,
                            "sample_distinct_non_null": unique, "proposed_name": name}
    shared = {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
              "additionalProperties": True, "required": [],
              "x-inference": "sampled hypothesis; not an enforced data contract"}
    candidates = [
        {"name": "faithful", "purpose": "Preserve source columns and observed native types",
         "json_schema": {**shared, "properties": raw}},
        {"name": "analytics", "purpose": "Propose tidy columns, typed measures, missing-value and date handling",
         "json_schema": {**shared, "properties": tidy}},
        {"name": "semantic", "purpose": "Describe the unit of observation and analytical field roles",
         "json_schema": {**shared, "properties": raw, "x-content-family": family, "x-field-roles": roles}},
    ]
    fingerprint = hashlib.sha256(json.dumps({"family": family,
        "columns": {name: field["type"] for name, field in raw.items()}}, sort_keys=True).encode()).hexdigest()
    duplicate_rows = count - len({json.dumps(r, sort_keys=True, ensure_ascii=False, default=str) for r in records})
    return {"schema_version": VERSION, "schema_fingerprint": fingerprint,
            "candidates": candidates, "sampling": sampling, "profiles": profiles,
            "quality": {"sample_duplicate_rows": duplicate_rows, "warnings": warnings,
                        "scope": "sample only; no claims about unsampled rows, completeness, or full-file integrity"}}
