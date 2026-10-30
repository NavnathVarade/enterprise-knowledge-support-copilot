from __future__ import annotations

import pytest
from packages.config.settings import IngestionSettings
from packages.domain.errors import InvalidDocument, PayloadTooLarge, UnsupportedMimeType
from rag.ingestion.validation import (
    validate_filename,
    validate_mime_type,
    validate_size,
    validate_upload,
)


@pytest.fixture
def settings() -> IngestionSettings:
    return IngestionSettings(max_upload_size_bytes=1024)


@pytest.mark.unit
@pytest.mark.parametrize(
    "filename", ["report.pdf", "My Policy v2.md", "notes.txt", "file-name_2026.csv"]
)
def test_valid_filenames_are_accepted(filename: str) -> None:
    validate_filename(filename)  # must not raise


@pytest.mark.unit
@pytest.mark.parametrize(
    "filename",
    [
        "",
        ".",
        "..",
        "../../etc/passwd",
        "/etc/passwd",
        "..\\..\\windows\\system32",
        "file;rm -rf.txt",
        "file\x00name.txt",
    ],
)
def test_malicious_or_invalid_filenames_are_rejected(filename: str) -> None:
    with pytest.raises(InvalidDocument):
        validate_filename(filename)


@pytest.mark.unit
def test_allowed_mime_type_is_accepted(settings: IngestionSettings) -> None:
    validate_mime_type("application/pdf", settings=settings)  # must not raise


@pytest.mark.unit
def test_disallowed_mime_type_is_rejected(settings: IngestionSettings) -> None:
    with pytest.raises(UnsupportedMimeType):
        validate_mime_type("application/x-executable", settings=settings)


@pytest.mark.unit
def test_empty_content_is_rejected(settings: IngestionSettings) -> None:
    with pytest.raises(InvalidDocument):
        validate_size(b"", settings=settings)


@pytest.mark.unit
def test_oversized_content_is_rejected(settings: IngestionSettings) -> None:
    with pytest.raises(PayloadTooLarge):
        validate_size(b"x" * 2000, settings=settings)  # limit is 1024


@pytest.mark.unit
def test_content_within_limit_is_accepted(settings: IngestionSettings) -> None:
    validate_size(b"x" * 100, settings=settings)  # must not raise


@pytest.mark.unit
def test_validate_upload_runs_all_checks_and_stops_at_first_failure(
    settings: IngestionSettings,
) -> None:
    with pytest.raises(InvalidDocument):
        validate_upload(
            filename="../evil.exe",
            mime_type="application/x-executable",
            content=b"hello",
            settings=settings,
        )


@pytest.mark.unit
def test_validate_upload_accepts_a_clean_upload(settings: IngestionSettings) -> None:
    validate_upload(
        filename="policy.md", mime_type="text/markdown", content=b"# Policy", settings=settings
    )  # must not raise
