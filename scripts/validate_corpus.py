"""Run the complete ingestion/reader/schema/integrity pipeline on a local corpus.

Use: python scripts/validate_corpus.py /path/test.zip --output test-results/corpus
Originals are retained under APP_DATA_DIR; the script never deletes source data.
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.config import Settings
from backend.jobs import Worker
from backend.manage import import_sources, verify
from backend.storage import Storage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, default=Path("test-results/corpus"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    storage = Storage(Settings.from_env())
    storage.initialize()
    batch = import_sources(storage, args.sources, expand=True)
    worker = Worker(storage)
    worker.refresh_policy()
    while worker.run_once():
        pass
    formats, statuses, families = Counter(), Counter(), Counter()
    shape_count = 0
    with (args.output / "manifest.jsonl").open("w", encoding="utf-8") as output, storage.connect() as connection:
        for record in connection.execute("""SELECT f.* FROM batch_items b JOIN files f ON f.id=b.file_id
                                             WHERE b.batch_id=? ORDER BY b.sequence""", (batch["batch_id"],)):
            detail = worker.analysis(record["id"])
            analysis = detail["analysis"] or {"status": detail["job"]["state"]}
            formats[analysis.get("format", "unknown")] += 1
            statuses[analysis["status"]] += 1
            families[analysis.get("family", "opaque")] += 1
            schema = analysis.get("schema", {})
            fingerprint = schema.get("schema_fingerprint")
            if fingerprint:
                # At most one schema file per content shape; no large sample reread.
                schema_file = args.output / f"schema-{fingerprint}.json"
                if not schema_file.exists():
                    schema_file.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n")
            output.write(json.dumps({"file": dict(record), **detail}, ensure_ascii=False) + "\n")
        shape_count = connection.execute("""SELECT COUNT(DISTINCT json_extract(a.result, '$.schema.schema_fingerprint'))
            FROM batch_items b JOIN files f ON f.id=b.file_id JOIN analyses a ON a.sha256=f.sha256
            WHERE b.batch_id=? AND a.policy_key=?""", (batch["batch_id"], worker.policy_key)).fetchone()[0]
    report = {"sources": [str(p.resolve()) for p in args.sources], "batch_id": batch["batch_id"],
              "uploaded_files": batch["succeeded"], "rejected_members": batch["failed"],
              "format_counts": dict(formats), "reading_status_counts": dict(statuses), "content_families": dict(families),
              "schema_shapes": shape_count, "integrity": verify(storage),
              "interpretation": "Sampled reader results; opaque/corrupt/encrypted content is preserved for review"}
    (args.output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False))
    unfinished = sum(statuses[state] for state in ("pending", "running", "failed", "reader_error"))
    return 0 if report["integrity"]["ok"] and batch["failed"] == 0 and not unfinished else 1


if __name__ == "__main__":
    raise SystemExit(main())
