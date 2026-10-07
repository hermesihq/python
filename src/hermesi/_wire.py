"""How values become a request: JSON, and path segments."""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import quote


def json_default(value: Any) -> Any:
    """What a payload may hold beyond JSON's own types: dates and decimals and ids are common in an event and have one obvious form."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (Decimal, uuid.UUID)):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def encode(body: dict[str, Any]) -> bytes:
    return json.dumps(body, default=json_default, separators=(",", ":")).encode("utf-8")


def path_segment(value: str, name: str = "external_id") -> str:
    """A value as one path segment. ``.`` and ``..`` are refused: a URL parser resolves them, even percent-encoded, which would
    aim a request carrying the secret key at another endpoint."""
    if not value:
        raise ValueError(f"{name} is required")
    if value in (".", ".."):
        raise ValueError(f'{name} cannot be "{value}"')
    return quote(value, safe="")
