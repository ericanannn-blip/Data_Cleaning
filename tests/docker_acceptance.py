"""Verify the release container against a named volume and real HTTP streams."""

import argparse
import hashlib
import io
import json
import os
import secrets
import socket
import subprocess
import time
import wave
from pathlib import Path
from uuid import uuid4

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="dat-ingestion:test")
    parser.add_argument("--output", type=Path, default=Path("test-results/docker-acceptance.json"))
    parser.add_argument("--extended", action="store_true", help="Verify the science and media image profile")
    args = parser.parse_args()
    name = "dat-acceptance-" + uuid4().hex[:10]
    volume, token = name + "-data", secrets.token_hex(24)
    environment = {key: value for key, value in os.environ.items() if key not in
                   {"DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"}}
    def docker(*command):
        result = subprocess.run(["docker", "--host=unix:///var/run/docker.sock", *command], env=environment,
                                capture_output=True, text=True, check=True)
        return result.stdout.strip()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    headers = {"authorization": "Bearer " + token}
    report = {"source": "generated fixtures, not the user attachment", "image": args.image}
    def start():
        docker("run", "-d", "--name", name, "--memory=1g", "--pids-limit=64", "--cap-drop=ALL",
               "--security-opt=no-new-privileges", "-p", f"127.0.0.1:{port}:8000", "-v", volume + ":/data",
               "-e", "API_TOKEN=" + token, args.image)
        deadline = time.monotonic() + 30
        with httpx.Client(trust_env=False, timeout=1) as client:
            while time.monotonic() < deadline:
                try:
                    if client.get(url + "/health").status_code == 200:
                        return
                except httpx.TransportError:
                    pass
                time.sleep(.1)
        raise RuntimeError("Container readiness failed")
    docker("volume", "create", volume)
    try:
        start()
        with httpx.Client(trust_env=False, timeout=30, headers=headers) as client:
            assert httpx.get(url + "/files", trust_env=False).status_code == 401
            report["memory_before"] = docker("stats", "--no-stream", "--format", "{{.MemUsage}}", name)
            chunk = b"1,2\n" * 16384
            chunks, digest = 512, hashlib.sha256()
            def body():
                first = b"id,value\n"
                digest.update(first)
                yield first
                for _ in range(chunks):
                    digest.update(chunk)
                    yield chunk
            started = time.monotonic()
            response = client.post(url + "/files?filename=large.dat", content=body(),
                                   headers={"content-type": "application/octet-stream"})
            response.raise_for_status()
            record = response.json()["uploaded_file"]
            assert record["sha256"] == digest.hexdigest()
            assert record["file_size"] == 32 * 1024 * 1024 + 9
            report["streamed_bytes"] = record["file_size"]
            report["upload_seconds"] = round(time.monotonic() - started, 3)
            response = client.post(url + "/files", files=[("files", (f"{i}.dat", b"id,value\n1,2\n")) for i in range(1005)])
            response.raise_for_status()
            assert response.json()["succeeded"] == 1005
            assert len(response.json()["files"]) == 200
            report["multipart_file_count"] = 1005
            expected_count = 1006
            if args.extended:
                import tempfile

                import h5py
                import numpy as np
                import pyarrow as pa
                import pyarrow.parquet as pq
                science = []
                with tempfile.TemporaryDirectory() as temporary:
                    path = Path(temporary) / "array.npy"
                    np.save(path, np.arange(50))
                    science.append(("npy", path.read_bytes()))
                    path = Path(temporary) / "table.parquet"
                    pq.write_table(pa.table({"id": [1, 2], "value": [2.0, 3.0]}), path)
                    science.append(("parquet", path.read_bytes()))
                    path = Path(temporary) / "dataset.h5"
                    with h5py.File(path, "w") as target:
                        target["measurement"] = np.arange(50)
                    science.append(("hdf5", path.read_bytes()))
                buffer = io.BytesIO()
                with wave.open(buffer, "wb") as target:
                    target.setnchannels(1)
                    target.setsampwidth(2)
                    target.setframerate(16000)
                    target.writeframes(b"\x00\x00" * 8000)
                science.append(("wav", buffer.getvalue()))
                for fmt, data in science:
                    created = client.post(url + "/files?filename=" + fmt + ".dat", content=data,
                                          headers={"content-type": "application/octet-stream"}).json()["uploaded_file"]
                    deadline = time.monotonic() + 30
                    while time.monotonic() < deadline:
                        result = client.get(url + f'/files/{created["id"]}/analysis').json()
                        if result["job"]["state"] == "completed":
                            break
                        time.sleep(.1)
                    assert result["analysis"]["status"] == "sampled", result
                    assert result["analysis"]["format"] == fmt, result
                    expected_count += 1
                report["science_and_media"] = "passed"
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                reading = client.get(url + f'/files/{record["id"]}/analysis').json()
                if reading["job"]["state"] == "completed":
                    break
                time.sleep(.1)
            assert reading["analysis"]["status"] == "sampled", reading
            assert len(reading["analysis"]["schema"]["candidates"]) == 3
            assert client.get(url + "/files").json()["total"] == expected_count
            report["memory_after"] = docker("stats", "--no-stream", "--format", "{{.MemUsage}}", name)
            languages = docker("exec", name, "tesseract", "--list-langs")
            assert all(language in languages for language in ("eng", "chi_sim", "chi_tra"))
            docker("exec", name, "python", "-m", "backend.manage", "backup", "/data/acceptance-backup")
            integrity = json.loads(docker("exec", name, "python", "-m", "backend.manage", "verify"))
            assert integrity["ok"] and integrity["checked_files"] == expected_count
            report["backup_and_integrity"] = "passed"
            docker("stop", "-t", "10", name)
            docker("rm", name)
            start()
            assert client.get(url + "/files").json()["total"] == expected_count
            assert client.get(url + f'/files/{record["id"]}/analysis').json()["analysis"] == reading["analysis"]
            downloaded = hashlib.sha256()
            with client.stream("GET", url + f'/files/{record["id"]}/download') as response:
                response.raise_for_status()
                for data in response.iter_bytes(65536):
                    downloaded.update(data)
            assert downloaded.hexdigest() == digest.hexdigest()
            report["container_replacement_persistence"] = "passed"
            report["authentication"] = "passed"
            report["chinese_ocr_models"] = "installed"
            report["sqlite_rows"] = expected_count
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report))
    except BaseException:
        logs = subprocess.run(["docker", "--host=unix:///var/run/docker.sock", "logs", name],
                              env=environment, capture_output=True, text=True, check=False)
        print((logs.stdout + logs.stderr)[-8000:])
        raise
    finally:
        subprocess.run(["docker", "--host=unix:///var/run/docker.sock", "rm", "-f", name], env=environment, capture_output=True, check=False)
        docker("volume", "rm", volume)


if __name__ == "__main__":
    main()
