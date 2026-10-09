# DAT Lab: File Readers and Schema Workspace

A shared workspace for multi-format uploads, content-first classification,
built-in reading, data-quality review, and versioned sample-driven schemas.
Extensions are display metadata; signatures and parseable content provide
classification evidence. Original bytes are retained under private UUID names.
This extends the ingestion demo without changing the DAT CLI.

**Delivery:** this version is delivered through a feature branch and Pull
Request. A local preview or browser test is not a deployed version. Production
publication remains a separate step; the Railway configuration is preserved.

## Architecture

```text
Browser → FastAPI (frontend + API) → signatures + bounded readers/profiling
                                  ├── SQLite: metadata, profile/schema history
                                  └── private filesystem: original bytes
```

One service, one worker, one persistent volume. The lightweight frontend needs
no Node build. The existing [command-line reader](dat-file-reader/README.md) and
its seven tests are preserved; the web adapter reuses its detection functions.
The CLI still supports extraction. The web workspace reads ZIP members in
bounded memory buffers without extracting them. Images and PDF pages are
re-encoded as PNG; HTML/XML and other text are displayed as text. There is no
original-file download endpoint.

## Workspace sections and readers

- Categories cover tables/JSON, documents, images, archives, text, audio,
  video, databases and unrecognized content. Search and format/category filters
  run on all saved records, including unloaded pages.
- Details have **Read**, **Schema & quality**, **Properties** and **History** views.
- PNG, JPEG, GIF, WebP, BMP and TIFF use an image reader with zoom.
- PDF has page images, previous/next navigation and bounded text extraction.
  Scanned PDFs remain readable as images; OCR is not included.
- ZIP has entry listings and previews for readable text, tables, images and
  PDFs. Unsafe paths, symbolic links, duplicate member names, encrypted members
  and oversized members are blocked. Nested ZIPs are listed without traversal.
- CSV, TSV, JSON and NDJSON have sampled tables, preserving original names
  and values. Plain text and XML/HTML have escaped text previews.
- Other signature-recognized formats have metadata and hexadecimal previews.
  Unknown, empty and damaged files remain stored with review notes.

## Sampling, quality and schema evolution

Text profiling reads a bounded prefix (64 KiB by default) and uses a
SHA-256-seeded reservoir over parseable candidate records within that window.
This is **not** a uniform sample of an entire larger file. Resampling supports
up to 1 MiB and 2,000 records. Complete JSON array elements inside a partial
prefix can contribute evidence; cut-off elements are excluded. A partial JSON
object is not used to fabricate structured records. Malformed JSON lines and
delimited rows are reported and excluded from inference.

Each profile records the analyzer version, method, seed, source hash, byte/row
budgets, scanned candidate count, sampled count and completeness. A profile
scans at most 10,000 candidate records, infers at most 200 fields and retains
50 preview rows. Nested objects use escaped JSON Pointer paths to depth eight;
arrays remain array-valued fields. Metadata readers use an image, document or
archive-entry grain, rather than claiming pixel or page-content schemas.
Their input bytes are bounded by the upload limit.

Checks identify missing/null values, conflicting types, surrounding whitespace,
duplicate sample rows, colliding field names, invalid records and identical
file hashes. Proposed field names use Unicode NFKC, trimming, lowercase and
underscores; collisions receive distinct suffixes. CSV/TSV values may propose
numeric, boolean or ISO date types; leading-zero identifiers stay strings.
Their null markers are empty strings, `null`, `na` and `n/a` (case-insensitive).
JSON primitive types and quoted strings are preserved. These are proposals,
not destructive transformations. Deduplication requires checking the record
grain; original values remain intact.

Files initially share a candidate family only when format, grain and original
observed field paths match exactly. Classification does not infer business
meaning from filenames. Resampling retains the family when format and grain
match and records added, removed or changed fields. Format/grain changes move
evidence to a separate family. A schema revision aggregates only the latest
profile per source file, so repeated sampling does not double-count evidence.
Earlier revisions retain their original source references.

The library supports version comparison, source inspection, reviewing the
latest candidate, and downloading its definition/evidence as JSON. Review
applies to that sampled version. Later evidence creates a fresh candidate;
stale confirmation receives HTTP 409. Schemas describe observed samples and
do not establish constraints for unseen records.
Resampling verifies the stored original against its upload hash before saving
new evidence. Inspecting a historical schema opens its referenced profile
version, rather than replacing that source with the latest sample.

Existing databases receive additive history tables. Legacy files are profiled
on first detail inspection or explicit resampling. Metadata, profile versions,
schema revisions and review state persist in SQLite across restarts.

## Local setup

Use Python 3.12+. From the repository root:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m backend
```

Open `http://localhost:8000`. On Windows, activate with
`.venv\Scripts\Activate.ps1`; set variables with `$env:PORT="8000"`.

The production entrypoint is `python -m backend`. It binds to `0.0.0.0`, reads
`PORT` (default `8000` locally), and runs a single Uvicorn worker. Direct equivalent:

```sh
uvicorn backend.main:app --host 0.0.0.0 --port "${PORT:-8000}" --workers 1
```

## Tests

```sh
python -m unittest discover -s dat-file-reader -p 'test_*.py' -v
python -m unittest discover -s tests -p 'test_*.py' -v
```

For the real browser/process acceptance check:

```sh
python -m playwright install chromium
python tests/browser_demo.py --screenshots test-results
```

The script can also use a preinstalled `chromium` automatically, or an explicit
`CHROMIUM_PATH`. It starts an isolated production server, uploads synthetic
files using the UI, checks partial errors, drag-and-drop, detail views, search,
filters, PDF pages/text, ZIP member previews, data-quality review, sampling
history, schema confirmation/export, network errors, page refresh, mobile
layout, SQLite rows, raw files, and persistence after restarting. Temporary uploads are
deleted afterward; screenshots are ignored by Git.

Browser acceptance verifies that records, reviewed schemas, profile history
and original file bytes survive an actual server restart, with no JavaScript
errors. Run the two unit suites above for the current test count. Container
publication is a separate check.

## API

| Endpoint | Behavior |
| --- | --- |
| `GET /` | Web interface |
| `POST /files` | Multipart upload; repeat the `files` field for each file |
| `GET /files?limit=100&offset=0` | Newest records, total, and global summary; max page size 200 |
| `GET /files/{file_id}` | One saved metadata record; 404 if absent |
| `GET /files/{file_id}/profile?version=N` | Current or historical content profile; lazy first profile for legacy files |
| `GET /files/{file_id}/history` | Profile versions and sampling evidence |
| `POST /files/{file_id}/resample?sample_bytes=262144&sample_rows=500` | Append profile/schema revisions from original bytes |
| `GET /files/{file_id}/image` | Re-encoded image preview |
| `GET /files/{file_id}/pages/{page}` | Bounded PDF text; one-based page numbers |
| `GET /files/{file_id}/pages/{page}/image` | Rasterized PDF page |
| `GET /files/{file_id}/archive/{entry}/profile` | In-memory member preview; zero-based directory index |
| `GET /schemas` | Current active schema families |
| `GET /schemas/{key}` | Revisions, drift and sampling evidence |
| `POST /schemas/{key}/confirm?version=N` | Mark the latest sample as reviewed; 409 for a stale version |
| `GET /health` | Lightweight SQLite connectivity check; `{"status":"ok"}` |
| `GET /config` | Public upload limits; no internal paths |
| `GET /docs` | Interactive API reference |

`POST /files` returns `{"files":[...],"succeeded":N,"failed":N}`. Each accepted
record has an ID; a rejected file has `id: null`, `status: "error"`, and a useful
`error_message`. Valid files in a mixed batch are still saved. Malformed or
oversized entire requests return HTTP 4xx before ingestion.

Records contain `original_name`, `extension`, `file_size`, `mime_type`,
`encoding`, `magic_bytes` (first 32 bytes in hex), `printable_ratio`,
`sample_bytes`, `detected_type`, `confidence`, `status`, `created_at`,
`error_message`, `sha256`, `category`, `reader`, `profile_version`, `schema_key`,
`quality_issues`, and `duplicate_files`. The printable ratio counts ASCII printable
bytes plus tab/newline/carriage return in the inspected sample; it is not a
Unicode readability score. Internal filesystem paths are never returned.

List queries accept `category`, `q` and `detected_type`. `total` counts matching
records, while `summary` and `categories` are global. Image and PDF endpoints
also accept `?entry=N` for ZIP members. No endpoint accepts arbitrary local
paths or extraction targets.

Signature matches are labeled `confidence: "signature"`; text/JSON/CSV/TSV/
NDJSON/XML/HTML and
encoding estimates are `"heuristic"`. Empty or unidentified files use
`status: "unsupported"`, remain persisted, and are shown for review. Headers
identify a likely format, not file integrity or safety. ZIP-based office files
are read as ZIP containers; compressed streams and XOR images are not decoded
by the web adapter. The original CLI supports those
additional transformations.

## Storage and limits

SQLite stores metadata. The filesystem stores original uploaded bytes under
generated UUID names. Duplicate original filenames get separate records and
copies. Required directories and the schema initialize automatically. SQLite
uses WAL and a busy timeout. Startup cleans interrupted writes and generated
raw orphans while preserving committed files.

| Environment variable | Default |
| --- | --- |
| `APP_DATA_DIR` | `<repository>/data` |
| `DATABASE_PATH` | `<APP_DATA_DIR>/app.db` |
| `UPLOAD_DIR` | `<APP_DATA_DIR>/raw` |
| `PORT` | `8000` locally; supplied by the deployment platform |
| `MAX_FILE_BYTES` | `10485760` (10 MiB) |
| `MAX_REQUEST_BYTES` | `52428800` (50 MiB including multipart overhead) |
| `MAX_FILES` | `20` per request |
| `MAX_STORAGE_BYTES` | `524288000` (500 MiB of accepted raw files) |
| `INSPECTION_BYTES` | `65536` (64 KiB prefix) |

See [.env.example](.env.example). Export variables before startup; the app does
not automatically load `.env`. Store uploads outside the static asset directory.
Databases, uploads, `.env` files, and browser output are excluded from Git.

This is an unauthenticated, shared workspace. Use synthetic files: visitors can
see metadata, content previews, sampled values and schema evidence. Original
bytes have no public download route. The storage cap rejects uploads when full;
there is no public delete endpoint. Operators manage retention and backups.
Back up the database consistently with SQLite's backup API and preserve the raw
directory; do not copy just a live WAL database file.

Reader limits: 25 megapixels per image; PDF output is scaled to a longest side
of at most 1,400 pixels and text to 8,000 characters per page; ZIP directories
are limited to 1,000 entries and 50 MiB declared expanded size. Each member is
limited to 2 MiB, with a compression-ratio check. Reader failure does not
discard the stored original. PDFium operations are serialized because its
native API is not thread-safe.

## Docker

```sh
docker build -t dat-inspector .
docker volume create dat-inspector-data
docker run --name dat-inspector -p 8000:8000 -e PORT=8000 \
  -v dat-inspector-data:/data dat-inspector
```

The image defaults `APP_DATA_DIR=/data`. The named volume preserves both the
database and original files when the container is replaced. A volume must be
mounted: `/data` alone inside a container is not persistent storage. Builds
behind a TLS-inspecting proxy can optionally pass a trusted public CA bundle as
the BuildKit secret `proxy_ca`; it is not stored in the image.

## Railway deployment

Railway is the selected provider. [Dockerfile](Dockerfile) and
[railway.json](railway.json) are ready for a single service. Volume provisioning
is a provider operation; `railway.json` does not create one automatically.

1. In an authenticated Railway project, create a single empty service and attach
   a Railway Volume mounted at `/data` **before allowing demo uploads**.
2. Configure `APP_DATA_DIR=/data`, `DATABASE_PATH=/data/app.db`, and
   `UPLOAD_DIR=/data/raw`. The platform supplies `PORT`. Keep one replica and one
   worker; disable scale-to-zero for this demo.
3. From this working tree, install/authenticate the Railway CLI in an environment
   with provider network access, then run `railway link` to select that project
   and service, followed by `railway up`. This deploys the current files without
   depending on whether the Git branch has been pushed. If using Git integration
   instead, the completed changes must first be available on its selected branch.
4. Generate a public domain under the service's networking settings. Railway
   uses `/health` for readiness; startup is `python -m backend`.
5. Open the public URL; upload synthetic text, JSON, PNG-header, and empty `.dat`
   samples. Check results, details, and page refresh. Confirm public `/health`
   returns HTTP 200. Restart/redeploy the service using the same volume and
   confirm both saved records and raw file hashes still exist.
6. Record the verified public URL here only after those checks pass.

Target persistent layout:

```text
/data/
├── app.db        # SQLite metadata (+ WAL/SHM while running)
└── raw/          # UUID-named original files, never publicly served
```

**Remaining deployment action:** make an authenticated Railway deployment
environment available with access to `backboard.railway.com` and the generated
public domain. This workspace's direct API probe was denied with HTTP 403 by
the network proxy, and no Railway token/connection is configured. No remote
service, provider volume, public domain, or public health/upload verification
has been claimed or completed.
