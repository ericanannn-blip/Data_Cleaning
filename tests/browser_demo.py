"""Real production-process/browser acceptance check, using synthetic files.

Run: python tests/browser_demo.py
Optional screenshots: python tests/browser_demo.py --screenshots test-results
This starts an isolated server and verifies a real process restart, not just a
second TestClient instance. It never writes demo uploads into the repository.
"""

import argparse
import json
import os
from pathlib import Path
import socket
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

import httpx
from playwright.sync_api import expect, sync_playwright
from fixtures import image_bytes, pdf_bytes, zip_bytes


ROOT = Path(__file__).resolve().parent.parent


def start_server(port, data_dir, log):
    environment = {**os.environ, "PORT": str(port), "APP_DATA_DIR": str(data_dir),
                   "DATABASE_PATH": str(data_dir / "app.db"), "UPLOAD_DIR": str(data_dir / "raw")}
    environment["MAX_FILE_BYTES"] = "8192"
    process = subprocess.Popen([sys.executable, "-m", "backend"], cwd=ROOT, env=environment, stdout=log, stderr=log)
    try:
        with httpx.Client(trust_env=False, timeout=1) as client:
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError("Production server exited; inspect the server log.")
                try:
                    if client.get(f"http://127.0.0.1:{port}/health").status_code == 200:
                        return process
                except httpx.TransportError:
                    pass
                time.sleep(.1)
        raise RuntimeError("Server readiness timed out.")
    except BaseException:
        stop_server(process)
        raise


def stop_server(process):
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--screenshots", type=Path)
    args = parser.parse_args()
    if args.screenshots:
        args.screenshots.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        data_dir = Path(temporary)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        with (data_dir / "server.log").open("w+") as log:
            process = start_server(port, data_dir, log)
            try:
                with sync_playwright() as playwright:
                    # Use a preinstalled Chromium when available; otherwise use
                    # the browser installed by `python -m playwright install`.
                    executable = os.getenv("CHROMIUM_PATH") or shutil.which("chromium")
                    browser = playwright.chromium.launch(executable_path=executable)
                    page = browser.new_page(viewport={"width": 1440, "height": 1100})
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(url)
                    expect(page.locator("#empty-title")).to_have_text("Your inspection log starts here")
                    expect(page.locator("#connection-label")).to_have_text("Service online")
                    expect(page.locator("#upload-button")).to_be_disabled()
                    samples = [
                        {"name": "notes.dat", "mimeType": "application/octet-stream", "buffer": b"synthetic plain text"},
                        {"name": "measurements.dat", "mimeType": "application/octet-stream", "buffer": b'{"temperature":22}'},
                        {"name": "signature.dat", "mimeType": "application/octet-stream", "buffer": image_bytes()},
                        {"name": "empty.dat", "mimeType": "application/octet-stream", "buffer": b""},
                        {"name": "too-big.bin", "mimeType": "application/octet-stream", "buffer": b"x" * 8193},
                    ]
                    page.locator("#file-input").set_input_files(samples)
                    expect(page.locator("#selected-files li")).to_have_count(5)
                    expect(page.locator(".queue-error")).to_contain_text("file limit")
                    page.locator("#upload-button").click()
                    expect(page.locator("#page-message")).to_contain_text("4 files saved.")
                    expect(page.locator("#results-body tr")).to_have_count(4)
                    expect(page.locator("#error-list")).to_contain_text("too-big.bin")
                    expect(page.locator("#results-body")).to_contain_text("PNG")
                    expect(page.locator("#results-body")).to_contain_text("JSON ≈")
                    expect(page.locator("#results-body")).to_contain_text("Needs review")
                    expect(page.locator("#upload-button")).to_be_disabled()
                    page.locator("#clear-selection").click()
                    page.locator("#dismiss-errors").click()
                    page.get_by_role("button", name="Details for signature.dat").click()
                    expect(page.locator("#detail-dialog")).to_be_visible()
                    page.wait_for_function("() => document.querySelector('#detail-body .preview-image')?.naturalWidth > 0")
                    page.get_by_role("tab", name="Properties", exact=True).click()
                    expect(page.locator("#detail-body")).to_contain_text("image/png")
                    expect(page.locator("#detail-body")).to_contain_text("89504e470d0a1a0a")
                    page.get_by_role("button", name="Close file details").click()
                    page.locator("#table-wrapper").evaluate("node => node.scrollLeft = 0")
                    page.locator("#search").fill("measurements")
                    expect(page.locator("#results-body tr")).to_have_count(1)
                    page.locator("#search").fill("")
                    page.locator("#type-filter").select_option("png")
                    expect(page.locator("#results-body tr")).to_have_count(1)
                    page.locator("#type-filter").select_option("all")
                    page.evaluate("""() => {
                      const transfer = new DataTransfer();
                      transfer.items.add(new File(['dragged text'], 'dropped.dat', {type: 'application/octet-stream'}));
                      document.getElementById('drop-zone').dispatchEvent(new DragEvent('drop', {dataTransfer: transfer, bubbles: true}));
                    }""")
                    expect(page.locator("#selected-files")).to_contain_text("dropped.dat")
                    page.locator("#upload-button").click()
                    expect(page.locator("#page-message")).to_contain_text("1 file saved.")
                    expect(page.locator("#results-body tr")).to_have_count(5)
                    more = [
                        {"name": "paper.pdf", "mimeType": "application/pdf", "buffer": pdf_bytes()},
                        {"name": "bundle.zip", "mimeType": "application/zip", "buffer": zip_bytes([
                            ("data/table.csv", b"id,value\n001,5\n002,7\n"),
                            ("image.png", image_bytes()), ("../../outside.txt", b"blocked")])},
                        {"name": "quality.csv", "mimeType": "text/csv", "buffer": b" ID ,amount\n001, 5 \n002,\n002,\n"},
                    ]
                    page.locator("#file-input").set_input_files(more)
                    page.locator("#upload-button").click()
                    expect(page.locator("#page-message")).to_contain_text("3 files saved.")
                    expect(page.locator("#results-body tr")).to_have_count(8)
                    page.get_by_role("button", name="Images 1", exact=True).click()
                    expect(page.locator("#results-body tr")).to_have_count(1)
                    page.get_by_role("button", name="All files 8", exact=True).click()
                    expect(page.locator("#results-body tr")).to_have_count(8)
                    page.get_by_role("button", name="Details for paper.pdf", exact=True).click()
                    page.wait_for_function("() => document.querySelector('#detail-body .pdf-page')?.naturalWidth > 0")
                    expect(page.locator(".pdf-text")).to_contain_text("Synthetic page one")
                    page.get_by_role("button", name="Next page", exact=True).click()
                    expect(page.locator(".pdf-text")).to_contain_text("Synthetic page two")
                    if args.screenshots:
                        page.screenshot(path=str(args.screenshots / "pdf-reader.png"))
                    page.get_by_role("button", name="Close file details").click()
                    page.get_by_role("button", name="Details for bundle.zip", exact=True).click()
                    expect(page.locator(".archive-list")).to_contain_text("Unsafe member path")
                    expect(page.locator(".archive-entry").nth(2).get_by_role("button", name="Read member")).to_be_disabled()
                    page.locator(".archive-entry").first.get_by_role("button", name="Read member").click()
                    expect(page.locator(".archive-preview")).to_contain_text("001")
                    if args.screenshots:
                        page.screenshot(path=str(args.screenshots / "zip-reader.png"))
                    page.get_by_role("button", name="Close file details").click()
                    page.get_by_role("button", name="Details for quality.csv", exact=True).click()
                    expect(page.locator("#detail-body .sample-table").first).to_contain_text("001")
                    page.get_by_role("tab", name="Schema & quality").click()
                    expect(page.locator(".quality-list")).to_contain_text("Repeated rows")
                    expect(page.locator(".quality-list")).to_contain_text("missing or null")
                    page.get_by_role("button", name="Resample & save version", exact=True).click()
                    expect(page.locator("#detail-title")).to_contain_text("v2")
                    page.get_by_role("tab", name="History", exact=True).click()
                    expect(page.locator(".history-row")).to_have_count(2)
                    page.get_by_role("button", name="View sample", exact=True).click()
                    expect(page.locator("#detail-title")).to_contain_text("v1")
                    page.get_by_role("tab", name="Schema & quality").click()
                    page.get_by_role("button", name="Review schema family", exact=True).click()
                    expect(page.locator("#detail-title")).to_contain_text("CSV schema · v2")
                    page.get_by_role("button", name="Confirm reviewed schema", exact=True).click()
                    expect(page.locator(".schema-detail [role='status']")).to_contain_text("Review applies to this sample version")
                    with page.expect_download() as download:
                        page.get_by_role("button", name="Download schema JSON", exact=True).click()
                    exported = json.loads(Path(download.value.path()).read_text())
                    assert exported["version"] == 2 and exported["reviewed_at"]
                    assert exported["definition"]["source_files"] == 1
                    page.get_by_label("Schema version", exact=True).select_option("1")
                    page.get_by_role("button", name="Inspect source", exact=True).click()
                    expect(page.locator("#detail-title")).to_have_text("quality.csv · v1")
                    page.get_by_role("button", name="Close file details").click()
                    page.reload()
                    expect(page.locator("#results-body tr")).to_have_count(8)
                    expect(page.locator("#schema-library")).to_contain_text("REVIEWED SAMPLE")
                    if args.screenshots:
                        page.screenshot(path=str(args.screenshots / "desktop.png"), full_page=True)
                    page.route("**/files?*", lambda route: route.abort())
                    page.locator("#refresh-button").click()
                    expect(page.locator("#list-error")).to_contain_text("Cannot reach the service")
                    expect(page.locator("#results-body tr")).to_have_count(8)
                    page.unroute("**/files?*")
                    page.locator("#refresh-button").click()
                    expect(page.locator("#list-error")).to_be_hidden()
                    page.set_viewport_size({"width": 390, "height": 844})
                    expect(page.locator("#upload-button")).to_be_visible()
                    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "Mobile page overflows horizontally"
                    page.get_by_role("button", name="Details for notes.dat").click()
                    page.get_by_role("tab", name="Properties", exact=True).click()
                    expect(page.locator("#detail-body")).to_contain_text("heuristic")
                    page.get_by_role("button", name="Close file details").click()
                    if args.screenshots:
                        page.screenshot(path=str(args.screenshots / "mobile.png"), full_page=True)
                    with sqlite3.connect(data_dir / "app.db") as db:
                        count = db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
                    assert count == 8
                    raw_before = {path.name: path.read_bytes() for path in (data_dir / "raw").glob("*.dat")}
                    assert len(raw_before) == 8
                    stop_server(process)
                    process = start_server(port, data_dir, log)
                    page.reload()
                    expect(page.locator("#results-body tr")).to_have_count(8)
                    expect(page.locator("#schema-library")).to_contain_text("REVIEWED SAMPLE")
                    assert raw_before == {path.name: path.read_bytes() for path in (data_dir / "raw").glob("*.dat")}
                    assert not errors, errors
                    browser.close()
                print(json.dumps({"frontend": "passed", "multi_file_upload": "passed", "drag_drop": "passed",
                                  "partial_errors": "passed", "classification": "passed", "details": "passed",
                                  "network_errors": "passed", "mobile": "passed", "sqlite_rows": count,
                                  "raw_files": len(raw_before), "process_restart_persistence": "passed",
                                  "pdf_reader": "passed", "zip_member_preview": "passed", "category_filters": "passed",
                                  "quality_checks": "passed", "resample_history": "passed", "schema_review_export": "passed",
                                  "browser_errors": errors}))
            except BaseException:
                log.flush()
                log.seek(0)
                print(log.read(), file=sys.stderr)
                raise
            finally:
                stop_server(process)


if __name__ == "__main__":
    main()
