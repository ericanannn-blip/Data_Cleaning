"""Real production-process/browser acceptance check, using synthetic files.

Run: python tests/browser_demo.py
Optional screenshots: python tests/browser_demo.py --screenshots test-results
This starts an isolated server and verifies a real process restart, not just a
second TestClient instance. It never writes demo uploads into the repository.
"""

import argparse
import gzip
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


def start_server(port, data_dir, log, preview_prefix=False):
    environment = {**os.environ, "PORT": str(port), "APP_DATA_DIR": str(data_dir),
                   "DATABASE_PATH": str(data_dir / "app.db"), "UPLOAD_DIR": str(data_dir / "raw")}
    environment["MAX_FILE_BYTES"] = "8192"
    process = subprocess.Popen([sys.executable, "-m", "tests.preview_server" if preview_prefix else "backend"], cwd=ROOT, env=environment, stdout=log, stderr=log)
    try:
        with httpx.Client(trust_env=False, timeout=1) as client:
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError("Production server exited; inspect the server log.")
                try:
                    if client.get(f"http://127.0.0.1:{port}{'/preview' if preview_prefix else ''}/health").status_code == 200:
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
    parser.add_argument("--preview-prefix", action="store_true")
    args = parser.parse_args()
    if args.screenshots:
        args.screenshots.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        data_dir = Path(temporary)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        url = f"http://127.0.0.1:{port}{'/preview/' if args.preview_prefix else '/'}"
        with (data_dir / "server.log").open("w+") as log:
            process = start_server(port, data_dir, log, args.preview_prefix)
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
                    # Information schemas group requested fields independently
                    # of the existing format-based schema families.
                    definition = {"name": "Temperature information", "entity": "measurement", "fields": [
                        {"name": "temperature", "type": "integer", "required": True}]}
                    page.locator("#information-template").fill(json.dumps(definition))
                    page.locator("#save-information").click()
                    expect(page.locator("#information-message")).to_contain_text("Saved Temperature information")
                    page.locator("#search").fill("measurements")
                    expect(page.locator("#extract-loaded")).to_have_text("Extract 1 loaded files")
                    page.locator("#extract-loaded").click()
                    expect(page.locator("#information-progress")).to_contain_text("1 files extracted; 0 need attention")
                    expect(page.locator("#information-results .information-run")).to_have_count(1)
                    page.locator("#information-results summary").first.click()
                    expect(page.locator("#information-results td").nth(1)).to_have_text("22")
                    expect(page.locator("#information-results")).to_contain_text("field_pointer")
                    with page.expect_download() as download:
                        page.locator("#information-results").get_by_role("button", name="Download result & evidence").click()
                    extraction = json.loads(Path(download.value.path()).read_text())
                    assert extraction["records"][0]["values"]["temperature"] == 22
                    assert extraction["records"][0]["fields"]["temperature"]["evidence"][0]["raw_value"] == 22
                    page.locator("#search").fill("")
                    expect(page.locator("#results-body tr")).to_have_count(8)
                    page.locator("#extract-loaded").click()
                    expect(page.locator("#information-progress")).to_contain_text("5 files extracted; 3 need attention")
                    expect(page.locator("#information-results .information-run")).to_have_count(6)
                    page.get_by_role("button", name="Details for measurements.dat", exact=True).click()
                    page.get_by_role("tab", name="Information", exact=True).click()
                    select = page.get_by_label("Information schema for this file")
                    expect(select.locator("option")).to_have_count(2)
                    select.select_option(index=1)
                    page.locator("#detail-body").get_by_role("button", name="Extract information", exact=True).click()
                    expect(page.locator("#detail-body")).to_contain_text("Extraction saved")
                    expect(page.locator("#detail-body .information-run")).to_have_count(3)
                    page.get_by_role("button", name="Close file details").click()
                    page.reload()
                    expect(page.locator("#results-body tr")).to_have_count(8)
                    expect(page.locator("#schema-library")).to_contain_text("REVIEWED SAMPLE")
                    expect(page.locator("#information-results .information-run")).to_have_count(7)
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
                    information = page.locator("#information-results .information-run").first
                    information.locator("summary").first.click()
                    expect(information.locator("thead")).to_be_visible()
                    assert information.locator("td").last.evaluate("node => getComputedStyle(node).display") == "table-cell"
                    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "Expanded information table overflows"
                    if args.screenshots:
                        information.screenshot(path=str(args.screenshots / "mobile-information.png"))
                    page.get_by_role("button", name="Details for quality.csv", exact=True).click()
                    assert page.locator("#detail-body .sample-table td").last.evaluate("node => getComputedStyle(node).display") == "table-cell"
                    expect(page.locator("#detail-body .sample-table thead").first).to_be_visible()
                    page.get_by_role("button", name="Close file details").click()
                    page.get_by_role("button", name="Details for notes.dat").click()
                    page.get_by_role("tab", name="Properties", exact=True).click()
                    expect(page.locator("#detail-body")).to_contain_text("heuristic")
                    page.get_by_role("button", name="Close file details").click()
                    if args.screenshots:
                        page.screenshot(path=str(args.screenshots / "mobile.png"), full_page=True)
                    page.set_viewport_size({"width": 1440, "height": 1100})
                    original_jpeg = image_bytes("JPEG", size=(640, 480))
                    encoded_jpeg = bytes(value ^ 0xA3 for value in original_jpeg)
                    recovery_files = [
                        {"name": "restored-photo.dat", "mimeType": "application/octet-stream", "buffer": encoded_jpeg},
                        {"name": "compressed-paper.dat", "mimeType": "application/octet-stream", "buffer": gzip.compress(pdf_bytes())},
                    ]
                    page.locator("#file-input").set_input_files(recovery_files)
                    page.locator("#upload-button").click()
                    expect(page.locator("#page-message")).to_contain_text("2 files saved.")
                    expect(page.locator("#results-body tr")).to_have_count(10)
                    page.get_by_role("button", name="Details for restored-photo.dat", exact=True).click()
                    page.get_by_role("button", name="Restore & view original", exact=True).click()
                    expect(page.locator(".restoration-view")).to_have_attribute("data-restored", "true")
                    expect(page.locator(".restoration-view")).to_contain_text("xor:0xa3")
                    page.wait_for_function("() => document.querySelector('#detail-body .preview-image')?.naturalWidth === 640")
                    with page.expect_download() as download:
                        page.get_by_role("link", name="Download restored file", exact=True).click()
                    assert Path(download.value.path()).read_bytes() == original_jpeg
                    assert download.value.suggested_filename == "restored-photo.jpg"
                    with page.expect_popup() as popup:
                        page.get_by_role("link", name="Open original file", exact=True).click()
                    popup.value.wait_for_load_state()
                    assert popup.value.locator("img").evaluate("node => node.naturalWidth") == 640
                    popup.value.close()
                    if args.screenshots:
                        page.screenshot(path=str(args.screenshots / "restored-image.png"))
                    page.get_by_role("button", name="Close file details").click()
                    page.get_by_role("button", name="Details for compressed-paper.dat", exact=True).click()
                    page.get_by_role("button", name="Restore & view original", exact=True).click()
                    expect(page.locator(".restoration-view")).to_contain_text("compressed-paper.pdf")
                    page.wait_for_function("() => document.querySelector('#detail-body .pdf-page')?.naturalWidth > 0")
                    page.get_by_role("button", name="Next page", exact=True).click()
                    expect(page.locator(".pdf-text")).to_contain_text("Synthetic page two")
                    with page.expect_download() as download:
                        page.get_by_role("link", name="Download restored file", exact=True).click()
                    assert Path(download.value.path()).read_bytes() == pdf_bytes()
                    with page.expect_popup() as popup:
                        page.get_by_role("link", name="Open original file", exact=True).click()
                    popup.value.wait_for_load_state()
                    assert popup.value.url.endswith("/restored/content")
                    # Chromium's native PDF viewer uses a closed browser shadow
                    # tree; its plugin is not a page-DOM <embed> element.
                    native_response = page.request.get(popup.value.url)
                    assert native_response.headers["content-type"] == "application/pdf"
                    assert native_response.body() == pdf_bytes()
                    if args.screenshots:
                        popup.value.wait_for_timeout(500)
                        popup.value.screenshot(path=str(args.screenshots / "original-pdf.png"))
                    popup.value.close()
                    if args.screenshots:
                        page.screenshot(path=str(args.screenshots / "restored-pdf.png"))
                    page.get_by_role("button", name="Close file details").click()
                    with sqlite3.connect(data_dir / "app.db") as db:
                        count = db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
                        assert db.execute("SELECT COUNT(*) FROM restored_files").fetchone()[0] == 2
                    assert count == 10
                    raw_before = {path.name: path.read_bytes() for path in (data_dir / "raw").glob("*.dat")}
                    assert len(raw_before) == 10
                    stop_server(process)
                    process = start_server(port, data_dir, log, args.preview_prefix)
                    page.reload()
                    expect(page.locator("#results-body tr")).to_have_count(10)
                    expect(page.locator("#schema-library")).to_contain_text("REVIEWED SAMPLE")
                    expect(page.locator("#information-results .information-run")).to_have_count(7)
                    page.get_by_role("button", name="Details for restored-photo.dat", exact=True).click()
                    expect(page.locator(".restoration-view")).to_have_attribute("data-restored", "true")
                    page.wait_for_function("() => document.querySelector('#detail-body .preview-image')?.naturalWidth === 640")
                    assert raw_before == {path.name: path.read_bytes() for path in (data_dir / "raw").glob("*.dat")}
                    assert not errors, errors
                    browser.close()
                print(json.dumps({"frontend": "passed", "multi_file_upload": "passed", "drag_drop": "passed",
                                  "partial_errors": "passed", "classification": "passed", "details": "passed",
                                  "network_errors": "passed", "mobile": "passed", "sqlite_rows": count,
                                  "raw_files": len(raw_before), "process_restart_persistence": "passed",
                                  "pdf_reader": "passed", "zip_member_preview": "passed", "category_filters": "passed",
                                  "quality_checks": "passed", "resample_history": "passed", "schema_review_export": "passed",
                                  "information_batch_and_evidence": "passed", "information_export_and_history": "passed",
                                  "xor_image_and_compressed_pdf_restore": "passed", "original_download": "passed",
                                  "mobile_table_rendering": "passed", "web_preview_prefix": args.preview_prefix,
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
