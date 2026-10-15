"""Bounded DAT recovery and persistent, validated original-format artifacts."""

import bz2
from datetime import datetime, timezone
import hashlib
import io
import json
import lzma
import os
from pathlib import Path
import zlib

from .inspection import reader
from .profiling import build_profile
from .readers import ReaderError


COMPRESSED = {"gzip", "bzip2", "xz", "zlib"}
INLINE_FORMATS = {"pdf", "jpeg", "png", "gif", "webp", "bmp", "tiff"}
EXTENSIONS = {"jpeg": "jpg", "text": "txt", "ndjson": "jsonl"}


def checksum(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(65536):
            digest.update(chunk)
    return digest.hexdigest()


def unpack(data, kind, budget):
    try:
        if kind in {"gzip", "zlib"}:
            decoder = zlib.decompressobj(31 if kind == "gzip" else 15)
            decoded = decoder.decompress(data, budget + 1)
        elif kind == "bzip2":
            decoder = bz2.BZ2Decompressor()
            decoded = decoder.decompress(data, max_length=budget + 1)
        else:
            decoder = lzma.LZMADecompressor(memlimit=64 * 1024 * 1024)
            decoded = decoder.decompress(data, max_length=budget + 1)
        if len(decoded) > budget:
            raise ReaderError("Recovered content exceeds the configured size limit.")
        if not decoder.eof or decoder.unused_data:
            raise ReaderError("Compressed content is incomplete or contains additional streams; no partial recovery was saved.")
        return decoded
    except (zlib.error, OSError, EOFError, ValueError, lzma.LZMAError) as exc:
        if isinstance(exc, ReaderError):
            raise
        raise ReaderError("Compressed content could not be recovered.") from exc


def recover(data, budget):
    transforms, layers = [], 0
    while True:
        signature = reader.detect_format(data)
        if signature and signature[0] in COMPRESSED:
            if layers >= 3:
                raise ReaderError("Recovery supports at most three compression layers.")
            data = unpack(data, signature[0], budget)
            transforms.append(signature[0])
            layers += 1
            continue
        if signature is None:
            xor = reader.find_image_xor(data)
            if xor:
                data, key, _ = xor
                transforms.append(f"xor:0x{key:02x}")
        break
    if not data:
        raise ReaderError("Empty content has no file to recover.")
    if len(data) > budget:
        raise ReaderError("Recovered content exceeds the configured size limit.")
    return data, transforms


class RestorationStore:
    def __init__(self, storage):
        self.storage = storage

    def get(self, file_id):
        with self.storage.connect() as connection:
            row = connection.execute("SELECT data_json FROM restored_files WHERE file_id = ?", (file_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def path(self, file_id):
        saved = self.get(file_id)
        if not saved:
            return None
        path = self.storage.settings.restored_dir / f"{file_id}.bin"
        if not path.is_file() or checksum(path) != saved["sha256"]:
            raise ReaderError("Recovered file is unavailable or differs from its saved hash. Restore it again.")
        return path

    def restore(self, file_id):
        record = self.storage.get_file(file_id)
        if record is None:
            return None
        settings = self.storage.settings
        with self.storage.write_lock:
            source = self.storage.raw_path(file_id)
            if checksum(source) != record["sha256"]:
                raise ReaderError("Stored original differs from its upload hash. Restore storage before recovery.")
            cached = self.get(file_id)
            if cached:
                try:
                    self.path(file_id)
                    return cached
                except ReaderError:
                    pass  # Regenerate a missing or damaged derived artifact.
            with source.open("rb") as stream:
                raw = stream.read(settings.max_file_bytes + 1)
            if len(raw) > settings.max_file_bytes:
                raise ReaderError("Source exceeds the configured recovery input limit.")
            data, transforms = recover(raw, settings.max_restored_bytes)
            digest = hashlib.sha256(data).hexdigest()
            profile = build_profile(io.BytesIO(data), record["original_name"], len(data), digest,
                                    settings.inspection_bytes, settings.sample_rows)
            if profile["reader"] == "hex":
                raise ReaderError("The recovered format could not be validated for reading. The original upload is retained.")
            kind = profile["format"]
            extension = EXTENSIONS.get(kind, kind)
            filename = f'{Path(record["original_name"]).stem[:180]}.{extension}'
            base = f"files/{file_id}/restored"
            result = {"file_id": file_id, "filename": filename, "format": kind,
                      "mime_type": profile["mime_type"], "file_size": len(data), "sha256": digest,
                      "source_sha256": record["sha256"], "transforms": transforms, "profile": profile,
                      "created_at": datetime.now(timezone.utc).isoformat(), "inline": kind in INLINE_FORMATS,
                      "content_url": base + "/content", "download_url": base + "/content?download=true"}
            temporary = settings.restored_dir / f"{file_id}.part"
            target = settings.restored_dir / f"{file_id}.bin"
            try:
                with self.storage.connect() as connection:
                    previous_size = connection.execute("SELECT file_size FROM restored_files WHERE file_id = ?", (file_id,)).fetchone()
                    used = self.storage.used_bytes(connection) - (previous_size[0] if previous_size else 0)
                    if used + len(data) > settings.max_storage_bytes:
                        raise ReaderError("Storage is full. Free storage before saving the recovered file.")
                    with temporary.open("xb") as output:
                        output.write(data)
                        output.flush()
                        os.fsync(output.fileno())
                    temporary.replace(target)
                    connection.execute("INSERT OR REPLACE INTO restored_files VALUES (?, ?, ?)",
                                       (file_id, json.dumps(result, ensure_ascii=False), len(data)))
            except Exception:
                # If a DB write failed, never leave newly generated bytes unaccounted for.
                target.unlink(missing_ok=True)
                raise
            finally:
                temporary.unlink(missing_ok=True)
            return result
