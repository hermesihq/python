"""What can go wrong, as exceptions a caller can branch on.

Branch on the exception class or on ``code``, never on ``message``: the message is prose that the API rewrites without notice.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class ErrorDetail:
    """One thing wrong with a request, as the API describes it."""

    field: Optional[str]
    issue: Optional[str]


class HermesiError(Exception):
    """Base class for everything this package raises on purpose."""


class HermesiConnectionError(HermesiError):
    """Hermesi could not be reached: DNS, a refused or dropped connection, or a timeout, after the retries were used up."""


class HermesiAPIError(HermesiError):
    """Hermesi answered, and the answer was a refusal.

    ``code`` is stable and meant to be branched on. ``request_id`` is what to quote to whoever runs Hermesi.
    """

    def __init__(
        self,
        *,
        status: int,
        type: str,
        code: str,
        message: str,
        request_id: str = "",
        detail: Optional[list[ErrorDetail]] = None,
        doc_url: str = "",
        retry_after: Optional[float] = None,
    ) -> None:
        super().__init__(f"{code}: {message} (HTTP {status}, request {request_id or 'unknown'})")
        self.status = status
        self.type = type
        self.code = code
        self.message = message
        self.request_id = request_id
        self.detail = detail or []
        self.doc_url = doc_url
        #: Seconds the server asked to wait, from ``Retry-After``; only on a 429.
        self.retry_after = retry_after

    @property
    def is_retryable(self) -> bool:
        """True for a failure that sending the same request again later can fix."""
        return self.status == 429 or self.status >= 500


class AuthenticationError(HermesiAPIError):
    """401: the API key is missing, malformed, invalid or revoked."""


class ForbiddenError(HermesiAPIError):
    """403: the key is valid but may not do this."""


class NotFoundError(HermesiAPIError):
    """404: for an event, a ``recipient`` that is not a subscriber in this environment."""


class ValidationError(HermesiAPIError):
    """400 or 422: the request was refused as malformed; ``detail`` names the fields."""


class RateLimitError(HermesiAPIError):
    """429: the environment is publishing faster than Hermesi accepts. Nothing was recorded."""


class ServerError(HermesiAPIError):
    """5xx: Hermesi failed. For an event, retrying with the same idempotency key is safe."""


_BY_STATUS: dict[int, type[HermesiAPIError]] = {
    401: AuthenticationError,
    403: ForbiddenError,
    404: NotFoundError,
    400: ValidationError,
    422: ValidationError,
    429: RateLimitError,
}


def error_from_response(status: int, body: Any, retry_after: Optional[float]) -> HermesiAPIError:
    """The exception for a non-2xx response. Anything that is not the API's error envelope is reported as such, with
    ``type`` ``sdk_error`` (the API never sends it), so a caller can tell a refusal from a response the SDK could not read."""
    envelope = body.get("error") if isinstance(body, dict) else None
    envelope = envelope if isinstance(envelope, dict) else {}

    def text(key: str) -> str:
        value = envelope.get(key)
        return value if isinstance(value, str) else ""

    details = [
        ErrorDetail(field=item.get("field") or None, issue=item.get("issue") or None)
        for item in (envelope.get("detail") or [])
        if isinstance(item, dict)
    ]
    cls = _BY_STATUS.get(status) or (ServerError if status >= 500 else HermesiAPIError)
    return cls(
        status=status,
        type=text("type") or "sdk_error",
        code=text("code") or "unexpected_response",
        message=text("message") or f"Hermesi answered HTTP {status}.",
        request_id=text("request_id"),
        detail=details,
        doc_url=text("doc_url"),
        retry_after=retry_after,
    )
