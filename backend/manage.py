"""Operator tools: streaming import, bounded archive expansion, backup, verify."""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import zipfile
import zlib
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

from .config import Settings
from .jobs import Worker
from .storage import Storage, fsync_directory


def digest_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(65536):
            digest.update(chunk)
    return digest.hexdigest()


def verify(storage):
    findings = []
    checked = 0
    with storage.connect() as connection:
        integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        foreign_keys = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")]
        for row in connection.execute("SELECT id, file_size, sha256 FROM files"):
            path = storage.raw_path(row["id"])
            checked += 1
            if not path.is_file():
                findings.append({"file_id": row["id"], "problem": "missing_original"})
            elif path.stat().st_size != row["file_size"] or digest_file(path) != row["sha256"]:
                findings.append({"file_id": row["id"], "problem": "hash_mismatch"})
    return {"checked_files": checked, "sqlite_integrity": integrity, "foreign_key_violations": foreign_keys,
            "file_issues": findings, "ok": integrity == ["ok"] and not foreign_keys and not findings}


def backup(storage, destination):
    # Must be a new directory. The manifest appears only after full verification.
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "raw").mkdir()
    snapshot = destination / "app.db"
    with storage.connect() as source, sqlite3.connect(snapshot) as target:
        source.backup(target)
    copied = 0
    with sqlite3.connect(snapshot) as connection:
        for file_id, digest in connection.execute("SELECT id, sha256 FROM files"):
            target = destination / "raw" / f"{file_id}.dat"
            shutil.copyfile(storage.raw_path(file_id), target)
            with target.open("rb") as saved:
                os.fsync(saved.fileno())
            if digest_file(target) != digest:
                raise RuntimeError("Backup hash mismatch")
            copied += 1
    with snapshot.open("rb") as saved:
        os.fsync(saved.fileno())
    manifest = {"version": 1, "created_at": datetime.now(timezone.utc).isoformat(), "copied_files": copied,
                "database": "app.db", "raw_directory": "raw"}
    with (destination / "manifest.json").open("w", encoding="utf-8") as saved:
        saved.write(json.dumps(manifest, indent=2) + "\n")
        saved.flush()
        os.fsync(saved.fileno())
    fsync_directory(destination / "raw")
    fsync_directory(destination)
    return manifest


def import_sources(storage, paths, expand=False):
    batch_id = storage.create_batch()
    expanded_bytes = 0
    for path in paths:
        candidates = (p for p in path.rglob("*") if p.is_file() and not p.is_symlink()) if path.is_dir() else [path]
        for source in candidates:
            with source.open("rb") as stream:
                saved = storage.ingest(SimpleNamespace(filename=source.name, file=stream), batch_id)
            if not expand or not saved["id"] or not zipfile.is_zipfile(source):
                continue
            try:
                with zipfile.ZipFile(source) as archive:
                    for member in archive.infolist():
                        if member.is_dir():
                            continue
                        member_path = PurePosixPath(member.filename.replace("\\", "/"))
                        mode = member.external_attr >> 16
                        unsafe = (member_path.is_absolute() or ".." in member_path.parts or stat.S_ISLNK(mode)
                                  or re_drive(member.filename) or bool(member.flag_bits & 1))
                        expanded_bytes += member.file_size
                        if unsafe or expanded_bytes > storage.settings.archive_bytes:
                            storage.record_batch_item(batch_id, {"id": None, "original_name": member.filename,
                                "status": "error", "error_message": "Unsafe, encrypted, or over-budget archive member skipped."})
                            if expanded_bytes > storage.settings.archive_bytes:
                                break
                            continue
                        # Never extract a member path into the filesystem. The storage
                        # writer generates a private UUID and checks actual byte size.
                        try:
                            with archive.open(member) as stream:
                                storage.ingest(SimpleNamespace(filename=member.filename, file=stream), batch_id)
                        except (zipfile.BadZipFile, EOFError, RuntimeError, NotImplementedError, zlib.error, OSError):
                            storage.record_batch_item(batch_id, {"id": None, "original_name": member.filename,
                                "status": "error", "error_message": "Archive member could not be read; archive original retained."})
            except (zipfile.BadZipFile, EOFError, OSError):
                storage.record_batch_item(batch_id, {"id": None, "original_name": source.name,
                    "status": "error", "error_message": "Archive could not be expanded; original retained."})
    storage.batch_state(batch_id, "completed")
    return storage.batch_results(batch_id)


def re_drive(name):
    return len(name) >= 2 and name[1] == ":"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("import")
    ingest.add_argument("paths", nargs="+", type=Path)
    ingest.add_argument("--expand", action="store_true")
    commands.add_parser("drain")
    commands.add_parser("verify")
    dump = commands.add_parser("backup")
    dump.add_argument("destination", type=Path)
    restore = commands.add_parser("restore")
    restore.add_argument("snapshot", type=Path)
    restore.add_argument("destination", type=Path)
    args = parser.parse_args()
    if args.command == "restore":
        source = Storage(Settings(args.snapshot / "app.db", args.snapshot / "raw", worker_enabled=False))
        if not (args.snapshot / "manifest.json").is_file() or not verify(source)["ok"]:
            parser.error("Backup is incomplete or failed verification")
        shutil.copytree(args.snapshot, args.destination)
        print(json.dumps({"restored": str(args.destination)}))
        return 0
    storage = Storage(Settings.from_env())
    storage.initialize()
    if args.command == "import":
        result = import_sources(storage, args.paths, args.expand)
    elif args.command == "drain":
        worker, count = Worker(storage), 0
        worker.refresh_policy()
        while worker.run_once():
            count += 1
        result = {"processed_jobs": count}
    elif args.command == "verify":
        result = verify(storage)
    else:
        result = backup(storage, args.destination)
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result.get("ok") is False or result.get("failed", 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())
