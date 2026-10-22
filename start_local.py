"""Create a portable local virtual environment and run the packaged application."""

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--science", action="store_true", help="Install NPY, Parquet and HDF5 readers")
    parser.add_argument("--reinstall", action="store_true", help="Check and install dependencies again")
    parser.add_argument("--setup-only", action="store_true", help="Prepare the environment without starting the server")
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 12):
        parser.error("Please install Python 3.12: https://www.python.org/downloads/")
    if not 1 <= args.port <= 65535:
        parser.error("Port must be between 1 and 65535")

    environment_dir = ROOT / ".venv"
    executable = environment_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not executable.is_file():
        print("Creating the local .venv environment...", flush=True)
        venv.EnvBuilder(with_pip=True).create(environment_dir)
    lock = ROOT / ("requirements-science-lock.txt" if args.science else "requirements-lock.txt")
    fingerprint = hashlib.sha256(lock.read_bytes()).hexdigest()
    marker = environment_dir / ".dat-cleaning-requirements.sha256"
    previous = marker.read_text(encoding="ascii").strip() if marker.is_file() else ""
    if args.reinstall or fingerprint != previous:
        print("Installing dependencies; the first start requires internet access...", flush=True)
        subprocess.run([str(executable), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(lock)],
                       cwd=ROOT, check=True)
        marker.write_text(fingerprint + "\n", encoding="ascii")
    if args.setup_only:
        print("Local environment ready.", flush=True)
        return 0

    environment = os.environ.copy()
    environment["APP_ENV"] = "development"
    environment.setdefault("APP_DATA_DIR", str(ROOT / "data"))
    if not shutil.which("tesseract"):
        print("OCR requires a separate Tesseract installation; see LOCAL_START.md.", flush=True)
    print(f"Open http://127.0.0.1:{args.port} in your browser. Press Ctrl+C to stop.", flush=True)
    return subprocess.run([str(executable), "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1",
                           "--port", str(args.port)], cwd=ROOT, env=environment).returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
    except subprocess.CalledProcessError as error:
        print("Dependency installation failed. Check the network, then run the launcher again.", file=sys.stderr)
        raise SystemExit(error.returncode)
