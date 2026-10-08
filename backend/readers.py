"""Bounded readers. Uploaded paths and archive member names are never served.

Images and PDF pages are re-encoded as PNG. ZIP members are read into a bounded
buffer, never extracted, and nested archives are not traversed automatically.
PDFium is serialized because its native API is not thread-safe.
"""

from collections import Counter
from contextlib import contextmanager
import io
from pathlib import PurePosixPath
import re
import stat
from threading import RLock
import warnings
import zipfile

from PIL import Image, UnidentifiedImageError
import pypdfium2 as pdfium


MAX_ARCHIVE_ENTRIES = 1000
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
MAX_MEMBER_BYTES = 2 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000
PDF_LOCK = RLock()


class ReaderError(ValueError):
    """A public, path-free message suitable for an API response."""


def _open_image(source):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image = Image.open(source)
            if image.width * image.height > MAX_IMAGE_PIXELS:
                image.close()
                raise ReaderError("Image exceeds the 25 megapixel reading limit.")
            return image
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as exc:
        raise ReaderError("Image content could not be read.") from exc


def image_metadata(source):
    with _open_image(source) as image:
        result = {"width": image.width, "height": image.height,
                  "mode": image.mode, "frames": getattr(image, "n_frames", 1)}
        image.verify()
        return result


def image_png(source):
    try:
        with _open_image(source) as image:
            image.thumbnail((1600, 1600))
            output = io.BytesIO()
            image.convert("RGBA").save(output, format="PNG")
            return output.getvalue()
    except (OSError, ValueError) as exc:
        if isinstance(exc, ReaderError):
            raise
        raise ReaderError("Image content could not be rendered.") from exc


@contextmanager
def pdf_document(source):
    with PDF_LOCK:
        try:
            document = pdfium.PdfDocument(str(source) if hasattr(source, "__fspath__") else source)
        except (pdfium.PdfiumError, ValueError, OSError) as exc:
            raise ReaderError("PDF is damaged, encrypted, or unavailable to the reader.") from exc
        try:
            yield document
        except pdfium.PdfiumError as exc:
            raise ReaderError("PDF content could not be read.") from exc
        finally:
            document.close()


def pdf_metadata(source):
    with pdf_document(source) as document:
        metadata = document.get_metadata_dict()
        return {"pages": len(document), "title": metadata.get("Title") or None,
                "author": metadata.get("Author") or None}


def pdf_page(source, page_number, as_image=False):
    with pdf_document(source) as document:
        if page_number < 1 or page_number > len(document):
            raise ReaderError("PDF page is outside the document.")
        page = document[page_number - 1]
        try:
            if not as_image:
                text_page = page.get_textpage()
                try:
                    count = min(text_page.count_chars(), 8000)
                    return {"page": page_number, "pages": len(document),
                            "text": text_page.get_text_range(0, count) if count else "",
                            "truncated": text_page.count_chars() > count}
                finally:
                    text_page.close()
            width, height = page.get_size()
            if not (0 < width <= 100_000 and 0 < height <= 100_000):
                raise ReaderError("PDF page dimensions exceed the reading limit.")
            bitmap = page.render(scale=min(1.5, 1400 / max(width, height)))
            try:
                output = io.BytesIO()
                bitmap.to_pil().save(output, format="PNG")
                return output.getvalue()
            finally:
                bitmap.close()
        finally:
            page.close()


def member_problem(info, duplicate=False):
    name = info.filename.replace("\\", "/")
    if (name.startswith("/") or re.match(r"^[a-zA-Z]:", name)
            or ".." in PurePosixPath(name).parts
            or any(ord(char) < 32 for char in name)
            or stat.S_ISLNK(info.external_attr >> 16)):
        return "Unsafe member path or symbolic link."
    if duplicate:
        return "Duplicate member name; select a unique archive entry."
    if info.flag_bits & 1:
        return "Encrypted archive member."
    if info.file_size > MAX_MEMBER_BYTES:
        return "Member exceeds the 2 MiB reading limit."
    if info.file_size > 1024 * 1024 and info.file_size / max(info.compress_size, 1) > 100:
        return "Member compression ratio exceeds the reading limit."
    return None


@contextmanager
def zip_document(source):
    try:
        archive = zipfile.ZipFile(source)
        infos = archive.infolist()
        if len(infos) > MAX_ARCHIVE_ENTRIES:
            archive.close()
            raise ReaderError("ZIP exceeds the 1000 entry reading limit.")
        if sum(info.file_size for info in infos) > MAX_ARCHIVE_BYTES:
            archive.close()
            raise ReaderError("ZIP exceeds the 50 MiB expanded reading limit.")
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        if isinstance(exc, ReaderError):
            raise
        raise ReaderError("ZIP directory could not be read.") from exc
    try:
        yield archive, infos
    finally:
        archive.close()


def zip_entries(source):
    with zip_document(source) as (_, infos):
        counts = Counter(info.filename for info in infos)
        return [{"index": index, "name": info.filename, "size": info.file_size,
                 "compressed_size": info.compress_size, "directory": info.is_dir(),
                 "blocked": member_problem(info, counts[info.filename] > 1)}
                for index, info in enumerate(infos)]


def zip_member(source, index):
    with zip_document(source) as (archive, infos):
        if not 0 <= index < len(infos):
            raise ReaderError("Archive entry was not found.")
        info = infos[index]
        problem = member_problem(info, sum(item.filename == info.filename for item in infos) > 1)
        if problem or info.is_dir():
            raise ReaderError(problem or "Choose a file rather than a directory.")
        try:
            with archive.open(info) as stream:
                data = stream.read(MAX_MEMBER_BYTES + 1)
            if len(data) > MAX_MEMBER_BYTES:
                raise ReaderError("Expanded archive member exceeds the reading limit.")
            return info.filename, data
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError) as exc:
            raise ReaderError("Archive member could not be read.") from exc
