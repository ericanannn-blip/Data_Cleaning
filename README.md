# DAT File Ingestion and Classification

A shared web demo that stores multiple `.dat` uploads, detects likely underlying
file formats, and lets visitors inspect persistent metadata. A `.dat` extension
does not specify a format. Detection uses the existing DAT reader's signature
and text rules; it does not classify the meaning of content.

**Deployment status:** implemented and verified locally, including Docker volume
persistence. There is no verified public URL yet. This workspace has no Railway
credentials, and its proxy denies the Railway API. Public deployment and public
health checks remain pending an authenticated Railway environment with network
access. Do not treat the local preview as a public deployment.

## Architecture

```text
Browser → FastAPI (frontend + API) → existing DAT detection rules
                                  ├── SQLite: metadata
                                  └── private filesystem: original bytes
```

One service, one worker, one persistent volume. The lightweight frontend needs
no Node build. The existing [command-line reader](dat-file-reader/README.md) and
its seven tests are preserved; the web adapter reuses its detection functions.
The CLI still supports extraction. The public web demo inspects metadata only
and never extracts archives or serves raw uploaded files.

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
filters, network errors, page refresh, mobile layout, SQLite rows, raw files,
and persistence after stopping and starting the server. Temporary uploads are
deleted afterward; screenshots are ignored by Git.

Verified in this workspace: **27 unit tests pass** (7 original reader + 20 API),
browser acceptance passes with no JavaScript errors, Docker builds, and SQLite
records and original file hashes survive container replacement on a named volume.

## API

| Endpoint | Behavior |
| --- | --- |
| `GET /` | Web interface |
| `POST /files` | Multipart upload; repeat the `files` field for each file |
| `GET /files?limit=100&offset=0` | Newest records, total, and global summary; max page size 200 |
| `GET /files/{file_id}` | One saved metadata record; 404 if absent |
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
`error_message`, and `sha256`. The printable ratio counts ASCII printable
bytes plus tab/newline/carriage return in the inspected sample; it is not a
Unicode readability score. Internal filesystem paths are never returned.

Signature matches are labeled `confidence: "signature"`; text/JSON/XML/HTML and
encoding estimates are `"heuristic"`. Empty or unidentified files use
`status: "unsupported"`, remain persisted, and are shown for review. Headers
identify a likely format, not file integrity or safety. ZIP-based office files
are reported as ZIP in this metadata-only demo; compressed content and XOR
images are not decoded by the web adapter. The original CLI supports those
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

This is an unauthenticated, shared demo. Use synthetic files: all visitors can
see metadata, including original filenames and header bytes. Raw contents have
no public download route. The storage cap rejects further uploads when full;
there is no public delete endpoint. Operators manage retention and backups.
Back up the database consistently with SQLite's backup API and preserve the raw
directory; do not copy just a live WAL database file.

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
