"""Identify and decode .dat files by their contents, without third-party packages."""

from __future__ import annotations

import argparse
import bz2
import gzip
import hashlib
import io
import json
import lzma
import os
import re
import shutil
import sys
import xml.etree.ElementTree as ET
import zipfile
import zlib
from collections import Counter
from pathlib import Path
from typing import Any, Iterator, Sequence


DEFAULT_MAX_BYTES = 64 * 1024 * 1024
SCHEMA_VERSION = "2.0"
CATEGORY_BY_FORMAT = {
    "png": "image", "jpeg": "image", "gif": "image", "webp": "image", "bmp": "image",
    "pdf": "document", "docx": "document", "xlsx": "document", "pptx": "document",
    "text": "text", "json": "text", "xml": "text", "html": "text",
    "mp3": "audio", "flac": "audio", "ogg": "audio", "wav": "audio",
    "mp4": "video", "zip": "archive", "7z": "archive", "rar": "archive",
    "gzip": "archive", "bzip2": "archive", "xz": "archive", "zlib": "archive",
    "sqlite": "database",
}
STREAMABLE_FORMATS = {"png", "jpeg", "gif", "webp", "bmp", "pdf", "mp3", "flac",
                      "ogg", "wav", "mp4", "sqlite", "7z", "rar"}


def detect_format(data: bytes) -> tuple[str, str, str] | None:
    """Return (name, MIME type, extension) from a signature, not a filename."""
    signatures = (
        (b"\x89PNG\r\n\x1a\n", "png", "image/png", "png"),
        (b"\xff\xd8\xff", "jpeg", "image/jpeg", "jpg"),
        (b"GIF87a", "gif", "image/gif", "gif"),
        (b"GIF89a", "gif", "image/gif", "gif"),
        (b"%PDF-", "pdf", "application/pdf", "pdf"),
        (b"PK\x03\x04", "zip", "application/zip", "zip"),
        (b"PK\x05\x06", "zip", "application/zip", "zip"),
        (b"\x1f\x8b\x08", "gzip", "application/gzip", "gz"),
        (b"BZh", "bzip2", "application/x-bzip2", "bz2"),
        (b"\xfd7zXZ\x00", "xz", "application/x-xz", "xz"),
        (b"SQLite format 3\x00", "sqlite", "application/vnd.sqlite3", "sqlite"),
        (b"7z\xbc\xaf\x27\x1c", "7z", "application/x-7z-compressed", "7z"),
        (b"Rar!\x1a\x07", "rar", "application/vnd.rar", "rar"),
        (b"fLaC", "flac", "audio/flac", "flac"),
        (b"OggS", "ogg", "application/ogg", "ogg"),
        (b"ID3", "mp3", "audio/mpeg", "mp3"),
        (b"BM", "bmp", "image/bmp", "bmp"),
    )
    for signature, name, mime, extension in signatures:
        if data.startswith(signature):
            return name, mime, extension
    if len(data) >= 12 and data[:4] == b"RIFF":
        if data[8:12] == b"WEBP":
            return "webp", "image/webp", "webp"
        if data[8:12] == b"WAVE":
            return "wav", "audio/wav", "wav"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "mp4", "video/mp4", "mp4"
    if len(data) >= 2 and data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "mp3", "audio/mpeg", "mp3"
    if data.startswith((b"\x78\x01", b"\x78\x5e", b"\x78\x9c", b"\x78\xda")):
        return "zlib", "application/zlib", "zlib"
    return None


def find_image_xor(data: bytes) -> tuple[bytes, int, tuple[str, str, str]] | None:
    """Recognize the fixed one-byte XOR used by many image .dat files."""
    if len(data) < 8:
        return None
    signatures = (
        (b"\x89PNG\r\n\x1a\n", "png"),
        (b"GIF87a", "gif"),
        (b"GIF89a", "gif"),
        (b"RIFF", "webp"),
        (b"\xff\xd8\xff", "jpeg"),
    )
    for signature, name in signatures:
        key = data[0] ^ signature[0]
        if key == 0 or any((data[i] ^ key) != byte for i, byte in enumerate(signature)):
            continue
        if name == "webp" and bytes(byte ^ key for byte in data[8:12]) != b"WEBP":
            continue
        if name == "jpeg" and bytes(byte ^ key for byte in data[-2:]) != b"\xff\xd9":
            continue
        decoded = bytes(byte ^ key for byte in data)
        detected = detect_format(decoded)
        if detected and detected[0] == name:
            return decoded, key, detected
    return None


def is_readable(text: str) -> bool:
    if not text:
        return True
    bad = sum(not (char.isprintable() or char in "\n\r\t") for char in text)
    return bad / len(text) < 0.01


def decode_text(data: bytes) -> tuple[str, str] | None:
    if not data:
        return "", "utf-8"
    bom_encodings = (
        (b"\x00\x00\xfe\xff", "utf-32"),
        (b"\xff\xfe\x00\x00", "utf-32"),
        (b"\xef\xbb\xbf", "utf-8-sig"),
        (b"\xfe\xff", "utf-16"),
        (b"\xff\xfe", "utf-16"),
    )
    for bom, encoding in bom_encodings:
        if data.startswith(bom):
            try:
                text = data.decode(encoding)
            except UnicodeDecodeError:
                return None
            return (text, encoding) if is_readable(text) else None
    if b"\x00" in data:
        return None
    for encoding in ("utf-8", "gb18030"):
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        if is_readable(text):
            return text, encoding
    return None


def classify_text(text: str) -> tuple[str, str, str, Any]:
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            return "json", "application/json", "json", json.loads(text)
        except json.JSONDecodeError:
            pass
    if stripped.startswith("<"):
        try:
            ET.fromstring(text)
            return "xml", "application/xml", "xml", text
        except ET.ParseError:
            if re.match(r"(?is)^\s*(<!doctype\s+html|<html\b)", text):
                return "html", "text/html", "html", text
    return "text", "text/plain", "txt", text


def read_limited(stream: Any, max_bytes: int) -> bytes:
    data = stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"解压内容超过 {max_bytes} 字节限制")
    return data


def decompress(data: bytes, name: str, max_bytes: int) -> bytes:
    if name == "gzip":
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
            return read_limited(stream, max_bytes)
    if name == "bzip2":
        result = bz2.BZ2Decompressor().decompress(data, max_length=max_bytes + 1)
    elif name == "xz":
        result = lzma.LZMADecompressor().decompress(data, max_length=max_bytes + 1)
    elif name == "zlib":
        decoder = zlib.decompressobj()
        result = decoder.decompress(data, max_bytes + 1)
        if not decoder.eof:
            if len(result) > max_bytes:
                raise ValueError(f"解压内容超过 {max_bytes} 字节限制")
            raise ValueError("ZLIB 数据不完整")
    else:
        raise ValueError(f"不支持的压缩格式: {name}")
    if len(result) > max_bytes:
        raise ValueError(f"解压内容超过 {max_bytes} 字节限制")
    return result


def xml_text(data: bytes, breaks: set[str]) -> str:
    root = ET.fromstring(data)
    pieces: list[str] = []
    for element in root.iter():
        local_name = element.tag.rsplit("}", 1)[-1]
        if local_name in {"t", "v"} and element.text:
            pieces.append(element.text)
        if local_name in breaks:
            pieces.append("\n")
    return "".join(pieces).strip()


def decode_zip(data: bytes, max_bytes: int) -> tuple[str, str, str, Any]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        if len(infos) > 1000:
            raise ValueError("ZIP 条目超过 1000 个")
        names = set(archive.namelist())
        total_size = sum(info.file_size for info in infos)
        if total_size > max_bytes:
            raise ValueError(f"ZIP 解压总量超过 {max_bytes} 字节限制")

        def member(name: str) -> bytes:
            with archive.open(name) as stream:
                return read_limited(stream, max_bytes)

        if "word/document.xml" in names:
            return ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "docx", xml_text(member("word/document.xml"), {"p"}))
        if "xl/workbook.xml" in names:
            shared: list[str] = []
            if "xl/sharedStrings.xml" in names:
                root = ET.fromstring(member("xl/sharedStrings.xml"))
                for item in root:
                    shared.append("".join(node.text or "" for node in item.iter()
                                          if node.tag.rsplit("}", 1)[-1] == "t"))
            sheets: dict[str, list[list[str]]] = {}
            for name in sorted(n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)):
                rows: list[list[str]] = []
                root = ET.fromstring(member(name))
                for row in root.iter():
                    if row.tag.rsplit("}", 1)[-1] != "row":
                        continue
                    cells: list[str] = []
                    for cell in row:
                        if cell.tag.rsplit("}", 1)[-1] != "c":
                            continue
                        value = next((node.text or "" for node in cell.iter()
                                      if node.tag.rsplit("}", 1)[-1] in {"v", "t"}), "")
                        if cell.attrib.get("t") == "s":
                            try:
                                value = shared[int(value)]
                            except (ValueError, IndexError):
                                pass
                        cells.append(value)
                    rows.append(cells)
                sheets[name] = rows
            return ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "xlsx", sheets)
        if "ppt/presentation.xml" in names:
            slides = {}
            for name in sorted(n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)):
                slides[name] = xml_text(member(name), {"p"})
            return ("pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                    "pptx", slides)
        contents: dict[str, Any] = {}
        for info in infos:
            if info.is_dir():
                continue
            if info.file_size > max_bytes:
                raise ValueError(f"ZIP 条目超过 {max_bytes} 字节限制")
            decoded = decode_text(member(info.filename))
            contents[info.filename] = decoded[0] if decoded else {"binary_bytes": info.file_size}
        return "zip", "application/zip", "zip", contents


def decode_bytes(data: bytes, max_bytes: int = DEFAULT_MAX_BYTES) -> dict[str, Any]:
    """Return the detected type, decoded content, and bytes for saving when applicable."""
    transform: list[str] = []
    detected = detect_format(data)
    if detected is None:
        xor_result = find_image_xor(data)
        if xor_result:
            data, key, detected = xor_result
            transform.append(f"xor:0x{key:02x}")

    for _ in range(3):
        if detected is None or detected[0] not in {"gzip", "bzip2", "xz", "zlib"}:
            break
        name = detected[0]
        try:
            data = decompress(data, name, max_bytes)
        except zlib.error:
            if name == "zlib" and decode_text(data) is not None:
                detected = None
                break
            raise
        except ValueError as exc:
            if name == "zlib" and str(exc) == "ZLIB 数据不完整" and decode_text(data) is not None:
                detected = None
                break
            raise
        transform.append(name)
        detected = detect_format(data)

    if detected and detected[0] in {"gzip", "bzip2", "xz", "zlib"}:
        raise ValueError("压缩嵌套超过 3 层")

    if detected and detected[0] == "zip":
        name, mime, extension, content = decode_zip(data, max_bytes)
        return {"format": name, "mime": mime, "extension": extension, "content": content,
                "encoding": None, "transform": transform, "data": data}

    if detected is None:
        decoded = decode_text(data)
        if decoded:
            text, encoding = decoded
            name, mime, extension, content = classify_text(text)
            return {"format": name, "mime": mime, "extension": extension, "content": content,
                    "encoding": encoding, "transform": transform, "data": None}
        return {"format": "unknown", "mime": "application/octet-stream", "extension": "bin",
                "content": None, "encoding": None, "transform": transform, "data": data}

    name, mime, extension = detected
    return {"format": name, "mime": mime, "extension": extension, "content": None,
            "encoding": None, "transform": transform, "data": data}


def read_dat(path: Path, max_bytes: int = DEFAULT_MAX_BYTES) -> dict[str, Any]:
    """Read one file. Binary output is available in the private `data` result field."""
    with path.open("rb") as stream:
        data = read_limited(stream, max_bytes)
    result = decode_bytes(data, max_bytes)
    return {"path": str(path), "size": len(data), **result}


def scan_dat_files(source: Path) -> list[Path]:
    if source.is_file():
        return [source]
    if not source.is_dir():
        raise FileNotFoundError(f"路径不存在: {source}")
    return sorted(path for path in source.rglob("*") if path.is_file() and path.suffix.lower() == ".dat")


def iter_dat_files(source: Path, output_dir: Path) -> Iterator[tuple[Path, Path]]:
    """Yield (file, path relative to this input) without listing the entire tree in memory."""
    if source.is_file():
        yield source, Path(source.name)
        return
    if not source.is_dir():
        raise FileNotFoundError(f"路径不存在: {source}")
    for directory, dirs, files in os.walk(source):
        dirs[:] = sorted(name for name in dirs
                         if (Path(directory) / name).resolve() != output_dir
                         and not (Path(directory) / name).is_symlink())
        for name in sorted(files):
            if name.lower().endswith(".dat"):
                path = Path(directory) / name
                if path.is_file():
                    yield path, path.relative_to(source)


def _write_bytes(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    try:
        temporary.write_bytes(data)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def _copy_large(source: Path, target: Path) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    digest = hashlib.sha256()
    try:
        with source.open("rb") as input_stream, temporary.open("wb") as output_stream:
            while chunk := input_stream.read(1024 * 1024):
                digest.update(chunk)
                output_stream.write(chunk)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return digest.hexdigest()


def process_file(path: Path, relative: Path, source_index: int, output_dir: Path,
                 max_bytes: int) -> dict[str, Any]:
    """Process one file, keeping content out of the manifest for large batches."""
    size = path.stat().st_size
    with path.open("rb") as stream:
        header = stream.read(32)
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "source_path": str(path.resolve()),
        "source_index": source_index,
        "relative_path": relative.as_posix(),
        "size_bytes": size,
        "sha256": None,
        "status": "ok",
        "category": "unknown",
        "format": "unknown",
        "mime": "application/octet-stream",
        "encoding": None,
        "transform": [],
        "decoded_file": None,
        "content_file": None,
        "header_hex": None,
        "error_code": None,
        "error": None,
    }
    folder = f"source_{source_index:03d}"

    if size > max_bytes:
        detected = detect_format(header)
        if detected:
            name, mime, extension = detected
            record.update(format=name, mime=mime, category=CATEGORY_BY_FORMAT.get(name, "unknown"))
        if detected and detected[0] in STREAMABLE_FORMATS:
            target = output_dir / "files" / record["category"] / folder / relative.parent / f"{relative.name}.decoded.{extension}"
            record["sha256"] = _copy_large(path, target)
            record["decoded_file"] = target.relative_to(output_dir).as_posix()
        else:
            record.update(status="skipped", header_hex=header.hex(), error_code="size_limit",
                          error=f"文件超过 {max_bytes} 字节处理上限；可调整 --max-bytes")
        return record

    with path.open("rb") as stream:
        original = read_limited(stream, max_bytes)
    record["sha256"] = hashlib.sha256(original).hexdigest()
    result = decode_bytes(original, max_bytes)
    data = result["data"]
    name = result["format"]
    record.update(format=name, mime=result["mime"], category=CATEGORY_BY_FORMAT.get(name, "unknown"),
                  encoding=result["encoding"], transform=result["transform"])
    if name == "unknown":
        record.update(status="unsupported", header_hex=header.hex(), error_code="unknown_format",
                      error="无法可靠识别或解码此文件")
        return record

    if data is not None:
        target = output_dir / "files" / record["category"] / folder / relative.parent / f"{relative.name}.decoded.{result['extension']}"
        _write_bytes(target, data)
        record["decoded_file"] = target.relative_to(output_dir).as_posix()
    content = result["content"]
    if content is not None:
        structured = isinstance(content, (dict, list))
        extension = "json" if structured else "txt"
        target = output_dir / "content" / record["category"] / folder / relative.parent / f"{relative.name}.content.{extension}"
        text = json.dumps(content, ensure_ascii=False, indent=2) if structured else str(content)
        _write_bytes(target, text.encode("utf-8"))
        record["content_file"] = target.relative_to(output_dir).as_posix()
    return record


def run(sources: Path | Sequence[Path], output_dir: Path, max_bytes: int = DEFAULT_MAX_BYTES) -> int:
    """Scan multiple inputs and write one manifest row per file plus a summary."""
    if isinstance(sources, Path):
        sources = [sources]
    sources = list(sources)
    if not sources:
        raise ValueError("至少需要一个输入文件或目录")
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    counts: Counter[str] = Counter()
    categories: Counter[str] = Counter()
    formats: Counter[str] = Counter()
    examples: list[dict[str, Any]] = []
    seen: set[Path] = set()
    scanned = 0
    manifest_path = output_dir / "manifest.jsonl"

    with manifest_path.open("w", encoding="utf-8", newline="\n") as manifest:
        for index, source in enumerate(sources, 1):
            try:
                for path, relative in iter_dat_files(source, output_dir):
                    resolved = path.resolve()
                    if resolved in seen:
                        continue
                    seen.add(resolved)
                    scanned += 1
                    try:
                        record = process_file(path, relative, index, output_dir, max_bytes)
                    except Exception as exc:
                        record = {"schema_version": SCHEMA_VERSION, "source_path": str(resolved),
                                  "source_index": index, "relative_path": relative.as_posix(),
                                  "size_bytes": None, "sha256": None, "status": "error",
                                  "category": "unknown", "format": "unknown",
                                  "mime": "application/octet-stream", "encoding": None, "transform": [],
                                  "decoded_file": None, "content_file": None, "header_hex": None,
                                  "error_code": type(exc).__name__, "error": str(exc)}
                    manifest.write(json.dumps(record, ensure_ascii=False) + "\n")
                    manifest.flush()
                    counts[record["status"]] += 1
                    categories[record["category"]] += 1
                    formats[record["format"]] += 1
                    if record["status"] != "ok" and len(examples) < 20:
                        examples.append({key: record[key] for key in
                                         ("source_path", "status", "format", "header_hex", "error_code", "error")})
            except OSError as exc:
                counts["input_error"] += 1
                if len(examples) < 20:
                    examples.append({"source_path": str(source), "status": "input_error",
                                     "format": None, "header_hex": None,
                                     "error_code": type(exc).__name__, "error": str(exc)})

    summary = {"schema_version": SCHEMA_VERSION, "inputs": [str(source) for source in sources],
               "output_dir": str(output_dir), "scanned_files": scanned,
               "status_counts": dict(counts), "category_counts": dict(categories),
               "format_counts": dict(formats), "review_examples": examples,
               "max_bytes": max_bytes}
    summary_path = output_dir / "summary.json"
    _write_bytes(summary_path, (json.dumps(summary, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    print(json.dumps({"manifest": str(manifest_path), "summary": str(summary_path),
                      "scanned_files": scanned, "status_counts": dict(counts)}, ensure_ascii=False))
    return 1 if counts["error"] or counts["skipped"] or counts["input_error"] else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="批量扫描 .dat 文件并输出分类、解码文件和 JSON 报告。")
    parser.add_argument("sources", nargs="+", type=Path, help="一个或多个文件或目录；目录递归扫描 .dat")
    parser.add_argument("-o", "--output", type=Path, default=Path("decoded"),
                        help="结果目录（默认：./decoded）")
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES,
                        help="内存解码和解压内容的字节上限（默认：67108864）")
    args = parser.parse_args(argv)
    if args.max_bytes < 1:
        parser.error("--max-bytes 必须大于 0")
    try:
        return run(args.sources, args.output, args.max_bytes)
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
