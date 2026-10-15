"""Additive, immutable information templates and extraction history."""

from datetime import datetime, timezone
import json
from uuid import uuid4

from .information import InformationSchema, extract_information


class InformationStore:
    def __init__(self, storage):
        self.storage = storage

    def save_schema(self, schema: InformationSchema):
        key = schema.key()
        with self.storage.write_lock, self.storage.connect() as connection:
            connection.execute("INSERT OR IGNORE INTO information_schemas VALUES (?, ?, ?)",
                               (key, json.dumps(schema.definition(), ensure_ascii=False), datetime.now(timezone.utc).isoformat()))
        return self.get_schema(key)

    def get_schema(self, key):
        with self.storage.connect() as connection:
            row = connection.execute("SELECT * FROM information_schemas WHERE id = ?", (key,)).fetchone()
        return self._schema(row) if row else None

    @staticmethod
    def _schema(row):
        schema = InformationSchema.model_validate_json(row["definition_json"])
        return {"id": row["id"], "created_at": row["created_at"],
                "definition": schema.definition(), "json_schema": schema.json_schema()}

    def schemas(self):
        with self.storage.connect() as connection:
            rows = connection.execute("SELECT * FROM information_schemas ORDER BY created_at DESC, id").fetchall()
        return [self._schema(row) for row in rows]

    def extract(self, file_id, key):
        saved = self.get_schema(key)
        record = self.storage.get_file(file_id)
        if saved is None or record is None:
            return None
        schema = InformationSchema.model_validate(saved["definition"])
        with self.storage.write_lock:
            result = extract_information(self.storage.raw_path(file_id), record["original_name"], schema, record["sha256"])
            result.update(id=str(uuid4()), file_id=file_id, created_at=datetime.now(timezone.utc).isoformat())
            with self.storage.connect() as connection:
                connection.execute("INSERT INTO information_runs VALUES (?, ?, ?, ?, ?)",
                                   (result["id"], file_id, key, json.dumps(result, ensure_ascii=False), result["created_at"]))
        return result

    def runs(self, file_id=None, schema_id=None, limit=20, offset=0):
        clauses, args = [], []
        for column, value in [("file_id", file_id), ("schema_id", schema_id)]:
            if value is not None:
                clauses.append(f"{column} = ?")
                args.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.storage.connect() as connection:
            total = connection.execute("SELECT COUNT(*) FROM information_runs" + where, args).fetchone()[0]
            rows = connection.execute("SELECT result_json FROM information_runs" + where +
                                      " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
        return {"runs": [json.loads(row[0]) for row in rows], "total": total, "limit": limit, "offset": offset}
