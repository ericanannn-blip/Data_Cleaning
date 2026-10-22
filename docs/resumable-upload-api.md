# Resumable upload and file analysis

This extends the existing private Sites Worker and its D1/R2 storage. It does not upload the local `ai` directory automatically. Choose files or a folder in the browser to transmit them to the existing private site.

## Upload contract

1. `POST /uploads` with JSON `{filename,size,mapped_name?,batch_id?}` creates a durable D1 session and R2 multipart upload. `filename` is the original uploaded basename. `mapped_name` is an optional display/download name; both are retained independently.
2. `PUT /uploads/{upload_id}/parts/{part_number}` sends one part, numbered from 1. Each nonfinal part is exactly 8 MiB; the last part is the remaining bytes. Include `X-Chunk-SHA256` as lowercase SHA-256 hex. Different bytes cannot replace a recorded part. Matching repeated parts return success. `GET /uploads/{upload_id}` reports accepted part checksums for resume; the browser rechecks selected chunk bytes before skipping them.
3. `POST /uploads/{upload_id}/complete` requires all parts and verifies the stored file size. It streams the completed R2 original through SHA-256, retains a 64 KiB prefix for analysis, and atomically registers the file and its batch membership. Repeated completion returns the same record. An interrupted finalizer can be reclaimed after two minutes. `DELETE /uploads/{upload_id}` aborts an incomplete upload; completed originals cannot be deleted through this endpoint.

The hosted limit is 256 MiB per file, with no application file-count limit. Browser transfers use two files concurrently and sequential 8 MiB chunks within each file. Browser memory scales with concurrent chunks rather than the entire selected batch. The final full-file SHA-256 is streamed with bounded memory; CPU/runtime limits can still affect files near the cap. Tested sizes must be read from the verification record rather than inferred from this configured cap.

The compatibility raw-stream `POST /files` endpoint accepts at most 8 MiB; larger tools should use the resumable API. `/config` advertises these limits. Empty files use an empty R2 object and still receive a SHA-256 and analysis record.

## Schema, criteria and source contracts

`GET /files/{id}/schemas?download=1` returns the observed Draft 2020-12 JSON Schema, per-file criteria, results, byte-range evidence, coverage, source SHA-256 and pending review/classification status. Full small JSON can generate nested field/type schemas. CSV/TSV inference excludes incomplete trailing sampled rows, reports inconsistent widths and duplicate/blank headers, and retains string cell types. Text has a string schema. Binary formats have a **metadata envelope schema**, not a schema for unparsed PDF/image/archive contents.

Inspection is limited to 64 KiB. JSON larger than that is treated as unparsed UTF-8 text, with explicit prefix-only coverage; it does not receive a complete inferred JSON object schema. Image OCR, PDF text reading, archive extraction and topic/task classification are not implemented in the hosted Worker. Their absence is visible in extraction and semantic classification status. Signature matches identify probable containers, not integrity: ZIP may also contain Office documents, OLE is a generic compound container, and ISO media does not establish an MP4/M4A subtype.

Each schema criterion has a stable rule ID, target field, operator, expected condition, `pass`/`fail`/`unknown`, reason and byte range. Schemas are grouped by SHA-256 of canonical schema JSON, so differing types/nesting do not merge merely because column names match. `/schemas` lists at most 200 groups; `/files?schema={fingerprint}` filters matching files, and `/files?type={type}` filters by detected format.

`site/taxonomy.json` contains only the existing v1.1.0 code/label/include/exclude/boundary vocabulary and root-schema hash. No private source examples, paths or customization evidence are bundled. File evidence uses a separate `file-analysis-1.0.0` envelope and does not relax the original chat schema's user-only/attachment-body constraints. Observed structure does not establish topic labels; topics/tasks remain `insufficient_evidence`, review remains `pending`, and training is not authorized.

`GET /files/{id}/download` returns the exact original bytes, with the mapped name when supplied. `?name=detected` offers a recognized extension for `.dat`/extensionless names while keeping unknown or ambiguous containers unchanged. This is byte-preserving naming, not decryption or reconstruction. Original basename and mapped basename are visible separately; duplicate names retain distinct IDs.

## Verification

Run `npm run build`, `npm run verify`, and `node scripts/test-site.mjs`. The integration script uses actual local Miniflare D1 and R2 with synthetic fixtures only, writes results under ignored `test-results/`, and disposes its runtime. It tests file hashes, type/schema evidence, name mapping, large multipart transfers, retries, conflict/checksum rejection, incomplete completion, aborts and query validation.

No original personal files, contents, mapping records or credentials belong in this source checkout. Unfinished upload sessions remain available for explicit resume/abort; automatic expired-session garbage collection is not implemented.
