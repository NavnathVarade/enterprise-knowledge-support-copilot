"""
Upload validation (Section 88).

Deliberately pure and synchronous -- no I/O, no database. `validate_upload` either
returns cleanly or raises a specific DomainError subclass; the route layer maps that to
an HTTP response the same way every other domain error is handled.

Phase 32 added content checks: the bytes must match the claimed type (magic bytes, strict
UTF-8 for text formats), DOCX archives are inspected for zip bombs BEFORE any parser opens
them, and PDFs are capped by page count.

Still NOT attempted (said plainly): virus/malware scanning, and process-level sandboxing of
the parsers (pypdf / python-docx run in-process in the ingestion worker; the pod's memory
limit, read-only filesystem, non-root user and NetworkPolicy bound the blast radius).
"""

from __future__ import annotations

import io
import re
import zipfile

from packages.config.settings import IngestionSettings
from packages.domain.errors import InvalidDocument, PayloadTooLarge, UnsupportedMimeType

# Deliberately conservative: letters, digits, dot, dash, underscore, space. Rejects any
# path separator (/ \) or parent-directory reference outright rather than trying to
# "clean" a malicious filename -- Section 88's "do not trust user-provided filenames"
# is best honored by refusing anything suspicious, not by attempting to sanitize it.
_SAFE_FILENAME_PATTERN = re.compile(r"^[A-Za-z0-9._ \-]+$")


def validate_filename(filename: str) -> None:
    if not filename or filename in {".", ".."}:
        raise InvalidDocument("Filename is missing or invalid.")
    if "/" in filename or "\\" in filename:
        raise InvalidDocument("Filename must not contain path separators.")
    if not _SAFE_FILENAME_PATTERN.match(filename):
        raise InvalidDocument("Filename contains unsupported characters.")


def validate_mime_type(mime_type: str, *, settings: IngestionSettings) -> None:
    if mime_type not in settings.allowed_mime_types:
        raise UnsupportedMimeType(f"MIME type '{mime_type}' is not permitted for upload.")


def validate_size(content: bytes, *, settings: IngestionSettings) -> None:
    if len(content) == 0:
        raise InvalidDocument("Uploaded file is empty.")
    if len(content) > settings.max_upload_size_bytes:
        raise PayloadTooLarge(
            f"Uploaded file ({len(content)} bytes) exceeds the maximum allowed size "
            f"({settings.max_upload_size_bytes} bytes)."
        )


_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_TEXT_TYPES = frozenset({"text/plain", "text/markdown", "text/csv", "application/json"})
_ZIP_MAGIC = b"PK\x03\x04"
_BIG_ENTRY_BYTES = 1024 * 1024  # ratio checks only matter for entries that can hurt


def _validate_text(content: bytes) -> None:
    if content.startswith((b"%PDF-", _ZIP_MAGIC)):
        raise InvalidDocument("File content does not match the declared text type.")
    if b"\x00" in content:
        raise InvalidDocument("Text file contains NUL bytes.")
    try:
        content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InvalidDocument("Text file is not valid UTF-8.") from exc


def _validate_pdf(content: bytes, settings: IngestionSettings) -> None:
    if b"%PDF-" not in content[:1024]:
        raise InvalidDocument("File content does not match the declared PDF type.")
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            raise InvalidDocument("Encrypted PDFs are not supported.")
        pages = len(reader.pages)
    except InvalidDocument:
        raise
    except Exception as exc:  # noqa: BLE001 -- pypdf raises many types on malformed input
        raise InvalidDocument("PDF is malformed.") from exc
    if pages > settings.max_pdf_pages:
        raise PayloadTooLarge(f"PDF has {pages} pages; the maximum is {settings.max_pdf_pages}.")


def _validate_docx(content: bytes, settings: IngestionSettings) -> None:
    """Inspect the archive's table of contents (no decompression) before any parser opens it.
    `zipfile` never yields more bytes for an entry than its declared size, so capping the
    declared sizes bounds what python-docx can be made to read."""
    if not content.startswith(_ZIP_MAGIC):
        raise InvalidDocument("File content does not match the declared DOCX type.")
    try:
        infos = zipfile.ZipFile(io.BytesIO(content)).infolist()
    except zipfile.BadZipFile as exc:
        raise InvalidDocument("DOCX archive is malformed.") from exc
    if len(infos) > settings.max_zip_entries:
        raise PayloadTooLarge("DOCX archive has too many entries.")
    total = 0
    names = set()
    for info in infos:
        name = info.filename
        if name.startswith(("/", "\\")) or ".." in name.split("/"):
            raise InvalidDocument("DOCX archive contains an unsafe path.")
        names.add(name)
        total += info.file_size
        if total > settings.max_zip_uncompressed_bytes:
            raise PayloadTooLarge("DOCX archive expands beyond the permitted size.")
        if (
            info.file_size > _BIG_ENTRY_BYTES
            and info.file_size > settings.max_zip_compression_ratio * max(info.compress_size, 1)
        ):
            raise PayloadTooLarge("DOCX archive has an implausible compression ratio.")
    if "[Content_Types].xml" not in names or "word/document.xml" not in names:
        raise InvalidDocument("File is not a Word document.")


def validate_content(*, mime_type: str, content: bytes, settings: IngestionSettings) -> None:
    """The bytes must be what the client claims they are, and safe to hand to a parser."""
    if mime_type in _TEXT_TYPES:
        _validate_text(content)
    elif mime_type == "application/pdf":
        _validate_pdf(content, settings)
    elif mime_type == _DOCX:
        _validate_docx(content, settings)


def validate_extracted_text(text: str, *, settings: IngestionSettings) -> None:
    """Bounds parsed output so one document cannot dominate chunking and embedding cost."""
    if len(text) > settings.max_extracted_chars:
        raise PayloadTooLarge(
            f"Extracted text ({len(text)} characters) exceeds the maximum "
            f"({settings.max_extracted_chars})."
        )


def validate_upload(
    *, filename: str, mime_type: str, content: bytes, settings: IngestionSettings
) -> None:
    """Runs every upload check; raises on the first failure."""
    validate_filename(filename)
    validate_mime_type(mime_type, settings=settings)
    validate_size(content, settings=settings)
    validate_content(mime_type=mime_type, content=content, settings=settings)
