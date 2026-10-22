"""Bounded, metadata-only inspection using the existing reader's rules.

The CLI still supports decoding and extraction. The public web adapter only
looks at a bounded prefix and never extracts, serves, or executes user content.
"""

import importlib.util
import json
import re
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "existing_dat_reader", Path(__file__).resolve().parent.parent / "dat-file-reader" / "dat_reader.py"
)
reader = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reader)


def inspect_file(path: Path, sample_limit: int) -> dict:
    size = path.stat().st_size
    with path.open("rb") as stream:
        sample = stream.read(sample_limit)
    result = {
        "file_size": size,
        "magic_bytes": sample[:32].hex(),
        "printable_ratio": round(sum(b in (9, 10, 13) or 32 <= b <= 126 for b in sample) / len(sample), 4) if sample else 0.0,
        "sample_bytes": len(sample),
        "encoding": None,
        "detected_type": "unknown",
        "mime_type": "application/octet-stream",
        "confidence": "unknown",
        "status": "unsupported",
        "error_message": "Format could not be identified. Manual review is needed.",
    }
    if not sample:
        result.update(detected_type="empty", error_message="This file is empty; there is no content to classify.")
        return result
    signature = reader.detect_format(sample)
    if signature:
        name, mime, _ = signature
        result.update(detected_type=name, mime_type=mime, confidence="signature", status="ok", error_message=None)
        return result
    decoded = reader.decode_text(sample)
    if decoded:
        text, encoding = decoded
        name, mime = "text", "text/plain"
        # Only parse complete, bounded text. A partial JSON/XML document is not
        # evidence of plain text's actual structured format.
        if size <= sample_limit:
            try:
                if text.lstrip().startswith(("{", "[")):
                    json.loads(text)
                    name, mime = "json", "application/json"
                elif re.match(r"(?is)^\s*(<!doctype\s+html|<html\b)", text):
                    name, mime = "html", "text/html"
                elif text.lstrip().startswith("<"):
                    from defusedxml import ElementTree
                    from defusedxml.common import DefusedXmlException
                    try:
                        ElementTree.fromstring(text, forbid_dtd=True)
                        name, mime = "xml", "application/xml"
                    except (DefusedXmlException, ElementTree.ParseError):
                        pass
            except (ValueError, RecursionError):
                pass
        result.update(detected_type=name, mime_type=mime, encoding=encoding,
                      confidence="heuristic", status="ok", error_message=None)
    return result
