"""Phase 32: uploads must be what they claim to be and safe to hand to a parser."""

from __future__ import annotations

import io
import zipfile

import pytest
from packages.config.settings import IngestionSettings
from packages.domain.errors import InvalidDocument, PayloadTooLarge
from pypdf import PdfWriter
from rag.ingestion.validation import validate_extracted_text, validate_upload

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
SETTINGS = IngestionSettings()


def _check(mime: str, content: bytes, settings: IngestionSettings = SETTINGS) -> None:
    validate_upload(filename="f.bin", mime_type=mime, content=content, settings=settings)


def _pdf(pages: int) -> bytes:
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=72, height=72)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _zip(entries: dict[str, bytes], *, deflate: bool = True) -> bytes:
    buf = io.BytesIO()
    method = zipfile.ZIP_DEFLATED if deflate else zipfile.ZIP_STORED
    with zipfile.ZipFile(buf, "w", method) as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


def _docx(**extra: bytes) -> bytes:
    return _zip({"[Content_Types].xml": b"<Types/>", "word/document.xml": b"<w/>", **extra})


# ---------------------------------------------------------------- text formats


@pytest.mark.parametrize("mime", ["text/plain", "text/markdown", "text/csv", "application/json"])
def test_clean_utf8_text_is_accepted(mime: str) -> None:
    _check(mime, "héllo — wörld\n".encode())
    _check(mime, b"\xef\xbb\xbfwith bom")


@pytest.mark.parametrize(
    "content",
    [b"\xff\xfe\x00bad", b"abc\x00def", b"caf\xe9 latin-1", _pdf(1), _docx()],
    ids=["invalid-utf8", "nul-byte", "latin1", "pdf-as-text", "zip-as-text"],
)
def test_text_types_reject_binary_and_disguised_content(content: bytes) -> None:
    with pytest.raises(InvalidDocument):
        _check("text/plain", content)


# ---------------------------------------------------------------- pdf


def test_a_real_pdf_is_accepted() -> None:
    _check("application/pdf", _pdf(3))


@pytest.mark.parametrize(
    "content",
    [b"just text", b"%PDF-1.7\ngarbage garbage", b"PK\x03\x04zip"],
    ids=["text", "bad", "zip"],
)
def test_pdf_type_requires_a_real_pdf(content: bytes) -> None:
    with pytest.raises(InvalidDocument):
        _check("application/pdf", content)


def test_pdf_page_limit() -> None:
    limited = IngestionSettings(max_pdf_pages=3)
    _check("application/pdf", _pdf(3), limited)
    with pytest.raises(PayloadTooLarge, match="pages"):
        _check("application/pdf", _pdf(4), limited)


def test_encrypted_pdf_is_refused_with_an_accurate_message() -> None:
    w = PdfWriter()
    w.add_blank_page(width=72, height=72)
    w.encrypt("secret")
    buf = io.BytesIO()
    w.write(buf)
    with pytest.raises(InvalidDocument, match="Encrypted"):
        _check("application/pdf", buf.getvalue())


# ---------------------------------------------------------------- docx / zip bombs


def test_a_structurally_valid_docx_is_accepted() -> None:
    _check(DOCX, _docx())


def test_docx_must_be_a_zip_with_word_parts() -> None:
    for bad in (b"plain text", b"PK\x03\x04truncated", _zip({"hello.txt": b"x"})):
        with pytest.raises(InvalidDocument):
            _check(DOCX, bad)


def test_real_zip_bomb_is_rejected_by_ratio_without_decompressing() -> None:
    bomb = _docx(**{"word/bloat.bin": b"\x00" * (8 * 1024 * 1024)})  # 8 MiB of zeros ~ 8 KiB
    assert len(bomb) < 100_000  # it genuinely is a bomb: tiny on the wire
    with pytest.raises(PayloadTooLarge, match="compression ratio"):
        _check(DOCX, bomb)


def test_total_expansion_cap() -> None:
    limited = IngestionSettings(
        max_zip_uncompressed_bytes=1024 * 1024, max_zip_compression_ratio=100000
    )
    with pytest.raises(PayloadTooLarge, match="expands"):
        _check(DOCX, _docx(**{"word/a.bin": b"\x00" * (2 * 1024 * 1024)}), limited)


def test_entry_count_cap() -> None:
    limited = IngestionSettings(max_zip_entries=5)
    many = _docx(**{f"word/part{i}.xml": b"<x/>" for i in range(10)})
    with pytest.raises(PayloadTooLarge, match="entries"):
        _check(DOCX, many, limited)


@pytest.mark.parametrize("name", ["../evil.xml", "/abs/evil.xml", "word/../../evil.xml"])
def test_unsafe_archive_paths_are_rejected(name: str) -> None:
    with pytest.raises(InvalidDocument, match="unsafe path"):
        _check(DOCX, _docx(**{name: b"x"}))


# ---------------------------------------------------------------- extracted text cap


def test_extracted_text_cap() -> None:
    limited = IngestionSettings(max_extracted_chars=100)
    validate_extracted_text("x" * 100, settings=limited)
    with pytest.raises(PayloadTooLarge):
        validate_extracted_text("x" * 101, settings=limited)
