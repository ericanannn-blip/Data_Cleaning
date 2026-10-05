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


ROOT = Path(__file__).resolve().parent.parent


def start_server(port, data_dir, log):
    environment = {**os.environ, "PORT": str(port), "APP_DATA_DIR": str(data_dir),
                   "DATABASE_PATH": str(data_dir / "app.db"), "UPLOAD_DIR": str(data_dir / "raw")}
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
                        {"name": "signature.dat", "mimeType": "application/octet-stream", "buffer": b"\x89PNG\r\n\x1a\n" + bytes(range(24))},
                        {"name": "empty.dat", "mimeType": "application/octet-stream", "buffer": b""},
                        {"name": "wrong.txt", "mimeType": "text/plain", "buffer": b"invalid extension"},
                    ]
                    page.locator("#file-input").set_input_files(samples)
                    expect(page.locator("#selected-files li")).to_have_count(5)
                    expect(page.locator(".queue-error")).to_have_text("Only .dat files are accepted.")
                    page.locator("#upload-button").click()
                    expect(page.locator("#page-message")).to_contain_text("4 files saved.")
                    expect(page.locator("#results-body tr")).to_have_count(4)
                    expect(page.locator("#error-list")).to_contain_text("wrong.txt")
                    expect(page.locator("#results-body")).to_contain_text("PNG")
                    expect(page.locator("#results-body")).to_contain_text("JSON ≈")
                    expect(page.locator("#results-body")).to_contain_text("Needs review")
                    expect(page.locator("#upload-button")).to_be_disabled()
                    page.locator("#clear-selection").click()
                    page.locator("#dismiss-errors").click()
                    page.get_by_role("button", name="Details for signature.dat").click()
                    expect(page.locator("#detail-dialog")).to_be_visible()
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
                    page.reload()
                    expect(page.locator("#results-body tr")).to_have_count(5)
                    if args.screenshots:
                        page.screenshot(path=str(args.screenshots / "desktop.png"), full_page=True)
                    page.route("**/files?*", lambda route: route.abort())
                    page.locator("#refresh-button").click()
                    expect(page.locator("#list-error")).to_contain_text("Cannot reach the service")
                    expect(page.locator("#results-body tr")).to_have_count(5)
                    page.unroute("**/files?*")
                    page.locator("#refresh-button").click()
                    expect(page.locator("#list-error")).to_be_hidden()
                    page.set_viewport_size({"width": 390, "height": 844})
                    expect(page.locator("#upload-button")).to_be_visible()
                    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "Mobile page overflows horizontally"
                    page.get_by_role("button", name="Details for notes.dat").click()
                    expect(page.locator("#detail-body")).to_contain_text("heuristic")
                    page.get_by_role("button", name="Close file details").click()
                    if args.screenshots:
                        page.screenshot(path=str(args.screenshots / "mobile.png"), full_page=True)
                    with sqlite3.connect(data_dir / "app.db") as db:
                        count = db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
                    assert count == 5
                    raw_before = {path.name: path.read_bytes() for path in (data_dir / "raw").glob("*.dat")}
                    assert len(raw_before) == 5
                    stop_server(process)
                    process = start_server(port, data_dir, log)
                    page.reload()
                    expect(page.locator("#results-body tr")).to_have_count(5)
                    assert raw_before == {path.name: path.read_bytes() for path in (data_dir / "raw").glob("*.dat")}
                    assert not errors, errors
                    browser.close()
                print(json.dumps({"frontend": "passed", "multi_file_upload": "passed", "drag_drop": "passed",
                                  "partial_errors": "passed", "classification": "passed", "details": "passed",
                                  "network_errors": "passed", "mobile": "passed", "sqlite_rows": count,
                                  "raw_files": len(raw_before), "process_restart_persistence": "passed",
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
