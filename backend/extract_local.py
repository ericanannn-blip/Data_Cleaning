"""Process an explicitly selected local directory without uploading anything.

python -m backend.extract_local --root DIR --schema TEMPLATE.json --output RESULTS.jsonl
"""

import argparse
import fnmatch
import json
from pathlib import Path
import sys

from pydantic import ValidationError

from .information import InformationSchema, extract_information
from .readers import ReaderError


def main(argv=None):
    parser = argparse.ArgumentParser(description="Extract information locally; no server or network connection is used.")
    parser.add_argument("--root", required=True, type=Path, help="Only read files within this directory.")
    parser.add_argument("--schema", required=True, type=Path, help="Information schema JSON template.")
    parser.add_argument("--output", required=True, type=Path, help="New local JSONL file; existing files are never overwritten.")
    parser.add_argument("--recursive", action="store_true", help="Include subdirectories; symbolic links are skipped.")
    parser.add_argument("--include", action="append", default=[], help="Filename glob; repeat for multiple patterns. Default: all files.")
    parser.add_argument("--max-file-bytes", type=int, default=10 * 1024 * 1024)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if not root.is_dir():
        parser.error("The selected root must be an accessible local directory.")
    if args.max_file_bytes < 1:
        parser.error("--max-file-bytes must be positive.")
    try:
        schema = InformationSchema.model_validate_json(args.schema.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, ValidationError):
        parser.error("The schema file must contain a valid information schema JSON object.")
    excluded = {args.schema.resolve(), args.output.resolve()}
    succeeded = failed = 0
    try:
        with args.output.open("x", encoding="utf-8") as output:
            files = root.rglob("*") if args.recursive else root.iterdir()
            for path in files:
                if path.is_symlink() or not path.is_file() or path.resolve() in excluded:
                    continue
                if not path.resolve().is_relative_to(root):
                    continue
                if args.include and not any(fnmatch.fnmatch(path.name, pattern) for pattern in args.include):
                    continue
                relative = path.relative_to(root).as_posix()
                try:
                    if path.stat().st_size > args.max_file_bytes:
                        raise ReaderError("File exceeds the selected local per-file size budget.")
                    result = extract_information(path, path.name, schema)
                    result["source"]["relative_path"] = relative
                    succeeded += 1
                except (ReaderError, OSError) as exc:
                    result = {"source": {"relative_path": relative}, "schema_id": schema.key(),
                              "error": str(exc) if isinstance(exc, ReaderError) else "Local file could not be read."}
                    failed += 1
                output.write(json.dumps(result, ensure_ascii=False) + "\n")
    except FileExistsError:
        parser.error("Output already exists. Select a new output filename.")
    except OSError:
        parser.error("Cannot read the selected directory or write the output file.")
    print(json.dumps({"succeeded": succeeded, "failed": failed, "schema_id": schema.key()}, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
