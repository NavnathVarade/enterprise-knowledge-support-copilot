from __future__ import annotations

import pytest
from packages.domain.errors import (
    DocumentNotFound,
    DomainError,
    RateLimitExceeded,
    UnauthorizedDocumentAccess,
)


@pytest.mark.unit
def test_domain_error_default_message() -> None:
    err = DomainError()
    assert err.code == "DOMAIN_ERROR"
    assert err.http_status == 500


@pytest.mark.unit
def test_unauthorized_document_access_is_403() -> None:
    err = UnauthorizedDocumentAccess()
    assert err.http_status == 403
    assert "access" in err.safe_message.lower()


@pytest.mark.unit
def test_document_not_found_is_404() -> None:
    err = DocumentNotFound()
    assert err.http_status == 404


@pytest.mark.unit
def test_rate_limit_exceeded_is_429() -> None:
    err = RateLimitExceeded()
    assert err.http_status == 429


@pytest.mark.unit
def test_error_context_not_in_message(caplog: pytest.LogCaptureFixture) -> None:
    err = DocumentNotFound(document_id="doc-123", internal_sql="SELECT * FROM documents")
    # context is for internal logging only; safe_message must never embed it
    assert "SELECT" not in err.safe_message
    assert err.context["internal_sql"] == "SELECT * FROM documents"
