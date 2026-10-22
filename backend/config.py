"""Storage and resource limits, configurable in one place."""

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def positive_int(name: str, default: int) -> int:
    value = int(os.getenv(name, default))
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def optional_limit(name: str, default: int = 0) -> int:
    value = int(os.getenv(name, default))
    if value < 0:
        raise ValueError(f"{name} must be non-negative (0 means unlimited)")
    return value


@dataclass(frozen=True)
class Settings:
    database_path: Path
    upload_dir: Path
    max_file_bytes: int = 0
    max_request_bytes: int = 0
    max_storage_bytes: int = 0
    inspection_bytes: int = 64 * 1024
    sample_rows: int = 200
    sample_pages: int = 3
    parser_timeout: int = 60
    parser_memory_mb: int = 512
    worker_enabled: bool = True
    api_token: str = ""
    ocr_languages: str = "eng"
    archive_bytes: int = 1024 * 1024 * 1024

    @classmethod
    def from_env(cls):
        data_dir = Path(os.getenv("APP_DATA_DIR", str(ROOT / "data")))
        token = os.getenv("API_TOKEN", "")
        if os.getenv("APP_ENV", "development") == "production" and len(token) < 32:
            raise ValueError("Production requires an API_TOKEN of at least 32 characters")
        return cls(
            database_path=Path(os.getenv("DATABASE_PATH", str(data_dir / "app.db"))),
            upload_dir=Path(os.getenv("UPLOAD_DIR", str(data_dir / "raw"))),
            max_file_bytes=optional_limit("MAX_FILE_BYTES"),
            max_request_bytes=optional_limit("MAX_REQUEST_BYTES"),
            max_storage_bytes=optional_limit("MAX_STORAGE_BYTES"),
            inspection_bytes=positive_int("INSPECTION_BYTES", 64 * 1024),
            sample_rows=positive_int("SAMPLE_ROWS", 200),
            sample_pages=positive_int("SAMPLE_PAGES", 3),
            parser_timeout=positive_int("PARSER_TIMEOUT", 60),
            parser_memory_mb=positive_int("PARSER_MEMORY_MB", 512),
            worker_enabled=os.getenv("WORKER_ENABLED", "1") == "1",
            api_token=token,
            ocr_languages=os.getenv("OCR_LANGUAGES", "eng"),
            archive_bytes=positive_int("ARCHIVE_BYTES", 1024 * 1024 * 1024),
        )
