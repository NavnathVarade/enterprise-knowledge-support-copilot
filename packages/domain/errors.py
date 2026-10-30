"""
Domain-level error taxonomy.

Every error a domain/service layer can raise is declared here as a subclass of
DomainError, carrying a stable machine-readable `code` and an HTTP-safe `message`
that never leaks internals (stack traces, SQL, hostnames, credentials — Section 80).
The API layer maps DomainError -> {"error": {"code", "message", "request_id"}} and
nothing else escapes to the client.
"""

from __future__ import annotations


class DomainError(Exception):
    code: str = "DOMAIN_ERROR"
    http_status: int = 500
    safe_message: str = "An unexpected error occurred."

    def __init__(self, message: str | None = None, **context: object) -> None:
        self.safe_message = message or self.safe_message
        self.context = context  # for internal logging only — never serialized to the client
        super().__init__(self.safe_message)


class UnauthorizedDocumentAccess(DomainError):
    code = "UNAUTHORIZED_DOCUMENT_ACCESS"
    http_status = 403
    safe_message = "You do not have access to the requested resource."


class PermissionDenied(DomainError):
    code = "PERMISSION_DENIED"
    http_status = 403
    safe_message = "Your role does not permit this operation."


class ResourceNotFound(DomainError):
    code = "RESOURCE_NOT_FOUND"
    http_status = 404
    safe_message = "The requested resource was not found."


class DocumentNotFound(DomainError):
    code = "DOCUMENT_NOT_FOUND"
    http_status = 404
    safe_message = "The requested document could not be found."


class InvalidDocument(DomainError):
    code = "INVALID_DOCUMENT"
    http_status = 422
    safe_message = "The submitted document is invalid."


class IllegalStatusTransition(DomainError):
    code = "ILLEGAL_STATUS_TRANSITION"
    http_status = 409
    safe_message = "That status change is not allowed from the document's current state."


class TenantNotFound(DomainError):
    code = "TENANT_NOT_FOUND"
    http_status = 404
    safe_message = "The requested tenant could not be found."


class TenantMismatch(DomainError):
    code = "TENANT_MISMATCH"
    http_status = 403
    safe_message = "The requested resource does not belong to your tenant."


class InvalidQuery(DomainError):
    code = "INVALID_QUERY"
    http_status = 400
    safe_message = "The submitted query could not be processed."


class EmbeddingProviderUnavailable(DomainError):
    code = "EMBEDDING_PROVIDER_UNAVAILABLE"
    http_status = 503
    safe_message = "The embedding service is temporarily unavailable. Please retry shortly."


class RetrievalUnavailable(DomainError):
    code = "RETRIEVAL_UNAVAILABLE"
    http_status = 503
    safe_message = "Retrieval is temporarily unavailable. Please retry shortly."


class LLMUnavailable(DomainError):
    code = "LLM_UNAVAILABLE"
    http_status = 503
    safe_message = (
        "Answer generation is temporarily unavailable. Retrieved sources may still be shown."
    )


class LLMTimeout(LLMUnavailable):
    code = "LLM_TIMEOUT"
    safe_message = "Answer generation timed out. Please retry."


class LLMInvalidResponse(DomainError):
    code = "LLM_INVALID_RESPONSE"
    http_status = 502
    safe_message = "The answer generator returned a response that could not be used."


class LLMContextOverflow(DomainError):
    code = "LLM_CONTEXT_OVERFLOW"
    http_status = 413
    safe_message = "The request is too large for the answer generator."


class InvalidAnalyticsQuery(DomainError):
    code = "INVALID_ANALYTICS_QUERY"
    http_status = 400
    safe_message = "The analytics request is not supported."


class AnalyticsQueryTimeout(DomainError):
    code = "ANALYTICS_QUERY_TIMEOUT"
    http_status = 504
    safe_message = "The analytics query took too long and was cancelled."


class RateLimitExceeded(DomainError):
    code = "RATE_LIMIT_EXCEEDED"
    http_status = 429
    safe_message = "Rate limit exceeded. Please slow down and retry later."


class BudgetExceeded(DomainError):
    code = "BUDGET_EXCEEDED"
    http_status = 429
    safe_message = "This workspace has reached its daily usage budget. Try again tomorrow (UTC)."


class OutputPolicyViolation(DomainError):
    code = "OUTPUT_POLICY_VIOLATION"
    http_status = 502
    safe_message = "The generated answer was withheld because it failed a safety check."


class CitationValidationFailed(DomainError):
    code = "CITATION_VALIDATION_FAILED"
    http_status = 502
    safe_message = "The generated answer could not be verified against its sources."


class ConfigurationError(DomainError):
    code = "CONFIGURATION_ERROR"
    http_status = 500
    safe_message = "The service is misconfigured."


class UnsupportedMimeType(InvalidDocument):
    code = "UNSUPPORTED_MIME_TYPE"
    safe_message = "The uploaded file type is not supported."


class ParsingFailed(InvalidDocument):
    code = "PARSING_FAILED"
    safe_message = "The document could not be parsed; it may be corrupted or malformed."


class PayloadTooLarge(InvalidDocument):
    code = "PAYLOAD_TOO_LARGE"
    http_status = 413
    safe_message = "The uploaded file exceeds the maximum allowed size."
