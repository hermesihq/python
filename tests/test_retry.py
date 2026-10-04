from __future__ import annotations

import httpx
import pytest
import respx

from hermesi import Hermesi, HermesiConnectionError, RateLimitError, RetryPolicy, ServerError, ValidationError
from hermesi._retry import parse_retry_after

from .conftest import BASE, KEY, Sleeper, accepted, error_body

EVENTS = f"{BASE}/v1/events"


@respx.mock
def test_retries_a_5xx_and_succeeds_with_the_same_idempotency_key(client: Hermesi, sleeper: Sleeper) -> None:
    route = respx.post(EVENTS).mock(
        side_effect=[
            httpx.Response(503, json=error_body("unavailable")),
            httpx.Response(502),
            httpx.Response(202, json=accepted()),
        ]
    )

    result = client.events.trigger("order.shipped", "u")

    assert result.event_id.startswith("evt_")
    assert route.call_count == 3
    keys = {call.request.headers["idempotency-key"] for call in route.calls}
    assert keys == {result.idempotency_key}, "every attempt carries the one key, so a retry cannot send twice"
    assert len(sleeper.waits) == 2


@respx.mock
def test_waits_exactly_as_long_as_the_server_asks_on_a_429(client: Hermesi, sleeper: Sleeper) -> None:
    respx.post(EVENTS).mock(
        side_effect=[
            httpx.Response(429, json=error_body("rate_limited", "rate_limit_error"), headers={"Retry-After": "2"}),
            httpx.Response(202, json=accepted()),
        ]
    )

    client.events.trigger("order.shipped", "u")

    assert sleeper.waits == [2.0]


@respx.mock
def test_does_not_sleep_through_a_retry_after_longer_than_it_is_willing_to_wait(sleeper: Sleeper) -> None:
    with Hermesi(KEY, base_url=BASE, sleep=sleeper, retry=RetryPolicy(max_retry_after=30)) as hermesi:
        route = respx.post(EVENTS).respond(
            429, json=error_body("rate_limited", "rate_limit_error"), headers={"Retry-After": "600"}
        )

        with pytest.raises(RateLimitError) as caught:
            hermesi.events.trigger("order.shipped", "u")

    assert caught.value.retry_after == 600.0
    assert route.call_count == 1
    assert sleeper.waits == [], "a request handler that sleeps for ten minutes is worse than one that fails"


@respx.mock
def test_gives_up_after_the_configured_retries_and_raises_the_last_answer(sleeper: Sleeper) -> None:
    with Hermesi(KEY, base_url=BASE, sleep=sleeper, retry=RetryPolicy(max_retries=2)) as hermesi:
        route = respx.post(EVENTS).respond(500, json=error_body("internal_error"))

        with pytest.raises(ServerError) as caught:
            hermesi.events.trigger("order.shipped", "u")

    assert route.call_count == 3, "the first attempt and two retries"
    assert caught.value.status == 500
    assert len(sleeper.waits) == 2


@respx.mock
def test_max_retries_zero_turns_retrying_off(sleeper: Sleeper) -> None:
    with Hermesi(KEY, base_url=BASE, sleep=sleeper, retry=RetryPolicy(max_retries=0)) as hermesi:
        route = respx.post(EVENTS).respond(503, json=error_body("unavailable"))

        with pytest.raises(ServerError):
            hermesi.events.trigger("order.shipped", "u")

    assert route.call_count == 1
    assert sleeper.waits == []


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
@respx.mock
def test_never_retries_a_refusal(client: Hermesi, sleeper: Sleeper, status: int) -> None:
    route = respx.post(EVENTS).respond(status, json=error_body("x", "validation_error"))

    with pytest.raises(Exception):  # noqa: B017 - the class is tested elsewhere
        client.events.trigger("order.shipped", "u")

    assert route.call_count == 1
    assert sleeper.waits == []


@respx.mock
def test_retries_a_connection_failure_and_a_timeout(client: Hermesi, sleeper: Sleeper) -> None:
    route = respx.post(EVENTS).mock(
        side_effect=[httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), httpx.Response(202, json=accepted())]
    )

    client.events.trigger("order.shipped", "u")

    assert route.call_count == 3
    assert len(sleeper.waits) == 2
    assert len({call.request.headers["idempotency-key"] for call in route.calls}) == 1


@respx.mock
def test_a_connection_that_never_comes_back_is_a_connection_error_not_an_api_error(sleeper: Sleeper) -> None:
    with Hermesi(KEY, base_url=BASE, sleep=sleeper, retry=RetryPolicy(max_retries=1)) as hermesi:
        route = respx.post(EVENTS).mock(side_effect=httpx.ConnectError("refused"))

        with pytest.raises(HermesiConnectionError) as caught:
            hermesi.events.trigger("order.shipped", "u")

    assert route.call_count == 2
    assert BASE in str(caught.value)
    assert isinstance(caught.value.__cause__, httpx.ConnectError)


@respx.mock
def test_backoff_grows_and_stays_inside_its_bounds(sleeper: Sleeper) -> None:
    with Hermesi(
        KEY, base_url=BASE, sleep=sleeper, retry=RetryPolicy(max_retries=4, backoff_base=1.0, backoff_cap=3.0)
    ) as hermesi:
        respx.post(EVENTS).respond(503)

        with pytest.raises(ServerError):
            hermesi.events.trigger("order.shipped", "u")

    # Equal jitter: each wait is between half the ceiling and the ceiling; the ceilings are 1, 2, 3 (capped), 3.
    ceilings = [1.0, 2.0, 3.0, 3.0]
    assert len(sleeper.waits) == 4
    for wait, ceiling in zip(sleeper.waits, ceilings):
        assert ceiling / 2 <= wait <= ceiling


@respx.mock
def test_a_validation_error_after_a_retry_is_still_raised_as_one(client: Hermesi) -> None:
    respx.post(EVENTS).mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(422, json=error_body("invalid_event_name", "validation_error")),
        ]
    )

    with pytest.raises(ValidationError):
        client.events.trigger("bad", "u")


# --- the policy on its own -----------------------------------------------------------------------


def test_the_policy_stops_at_max_retries() -> None:
    policy = RetryPolicy(max_retries=2)

    assert policy.delay(0, None) is not None
    assert policy.delay(1, None) is not None
    assert policy.delay(2, None) is None


def test_the_policy_honours_retry_after_exactly_and_without_jitter() -> None:
    assert RetryPolicy().delay(0, 4.0, rng=lambda: 0.99) == 4.0
    assert RetryPolicy().delay(0, 0.0) == 0.0


def test_the_policy_refuses_a_retry_after_over_its_limit() -> None:
    assert RetryPolicy(max_retry_after=10).delay(0, 11.0) is None
    assert RetryPolicy(max_retry_after=10).delay(0, 10.0) == 10.0


def test_the_policy_never_waits_less_than_half_the_ceiling() -> None:
    policy = RetryPolicy(backoff_base=2.0)

    assert policy.delay(0, None, rng=lambda: 0.0) == 1.0
    assert policy.delay(0, None, rng=lambda: 1.0) == 2.0
    assert policy.delay(1, None, rng=lambda: 0.0) == 2.0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("5", 5.0),
        (" 2.5 ", 2.5),
        ("0", 0.0),
        (None, None),
        ("", None),
        ("soon", None),
        ("-3", None),
        ("Wed, 21 Oct 2026 07:28:00 GMT", None),
    ],
)
def test_retry_after_is_read_as_seconds_and_nothing_else(raw: str | None, expected: float | None) -> None:
    assert parse_retry_after(raw) == expected
