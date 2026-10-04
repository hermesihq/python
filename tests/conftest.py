from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Callable

import pytest

from hermesi import Hermesi, RetryPolicy

BASE = "https://hermesi.example.test"
KEY = "hm_sk_prod_testkey"


class Sleeper:
    """Records the waits instead of making them, so retry tests take no time and can assert on the delays."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


@pytest.fixture
def sleeper() -> Sleeper:
    return Sleeper()


@pytest.fixture
def client(sleeper: Sleeper) -> Iterator[Hermesi]:
    with Hermesi(KEY, base_url=BASE, sleep=sleeper) as hermesi:
        yield hermesi


def accepted(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "event_id": "evt_01K2QH8F3T7Y0RJ4N5V6WX8ZQD",
        "status": "accepted",
        "notifications": [{"id": "not_1", "subscriber_id": "sub_1", "workflow": "order-shipped"}],
        "warnings": [],
    }
    body.update(overrides)
    return body


def error_body(
    code: str = "internal_error", type_: str = "internal_error", message: str = "no", **extra: Any
) -> dict[str, Any]:
    error: dict[str, Any] = {
        "type": type_,
        "code": code,
        "message": message,
        "request_id": "req_1",
        "detail": [],
        "doc_url": "https://d/x",
    }
    error.update(extra)
    return {"error": error}


NO_JITTER: Callable[[], float] = lambda: 0.0  # noqa: E731

FAST = RetryPolicy(max_retries=3)
