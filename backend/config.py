"""Storage and resource limits, configurable in one place."""

from dataclasses import dataclass
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def positive_int(name: str, default: int) -> int:
    value = int(os.getenv(name, default))
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True)
class Settings:
    database_path: Path
    upload_dir: Path
    max_file_bytes: int = 10 * 1024 * 1024
    max_request_bytes: int = 50 * 1024 * 1024
    max_files: int = 20
    max_storage_bytes: int = 500 * 1024 * 1024
    inspection_bytes: int = 64 * 1024

    @classmethod
    def from_env(cls):
        data_dir = Path(os.getenv("APP_DATA_DIR", str(ROOT / "data")))
        return cls(
            database_path=Path(os.getenv("DATABASE_PATH", str(data_dir / "app.db"))),
            upload_dir=Path(os.getenv("UPLOAD_DIR", str(data_dir / "raw"))),
            max_file_bytes=positive_int("MAX_FILE_BYTES", 10 * 1024 * 1024),
            max_request_bytes=positive_int("MAX_REQUEST_BYTES", 50 * 1024 * 1024),
            max_files=positive_int("MAX_FILES", 20),
            max_storage_bytes=positive_int("MAX_STORAGE_BYTES", 500 * 1024 * 1024),
            inspection_bytes=positive_int("INSPECTION_BYTES", 64 * 1024),
        )
