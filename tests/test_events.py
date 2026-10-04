from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import httpx
import pytest
import respx

from hermesi import (
    Actor,
    AuthenticationError,
    ForbiddenError,
    Hermesi,
    HermesiAPIError,
    NotFoundError,
    RateLimitError,
    ServerError,
    Subscriber,
    ValidationError,
)

from .conftest import BASE, KEY, accepted, error_body

EVENTS = f"{BASE}/v1/events"


def body_of(route: respx.Route) -> dict[str, object]:
    return json.loads(route.calls.last.request.content)  # type: ignore[no-any-return]


@respx.mock
def test_publishes_an_event_with_the_secret_key_and_a_json_body(client: Hermesi) -> None:
    route = respx.post(EVENTS).respond(202, json=accepted())

    result = client.events.trigger("order.shipped", "user_8821", {"order_id": "4821"})

    request = route.calls.last.request
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert request.headers["content-type"] == "application/json"
    assert request.headers["user-agent"].startswith("hermesi-python/")
    assert body_of(route) == {"name": "order.shipped", "recipient": "user_8821", "payload": {"order_id": "4821"}}
    assert result.event_id == "evt_01K2QH8F3T7Y0RJ4N5V6WX8ZQD"
    assert result.status == "accepted"
    assert [(n.id, n.subscriber_id, n.workflow) for n in result.notifications] == [("not_1", "sub_1", "order-shipped")]
    assert result.warnings == []


@respx.mock
def test_generates_an_idempotency_key_when_none_is_given_and_reports_it(client: Hermesi) -> None:
    route = respx.post(EVENTS).respond(202, json=accepted())

    result = client.events.trigger("order.shipped", "user_1")

    sent = route.calls.last.request.headers["idempotency-key"]
    assert uuid.UUID(sent), "a UUID"
    assert result.idempotency_key == sent


@respx.mock
def test_uses_the_callers_idempotency_key_as_is(client: Hermesi) -> None:
    route = respx.post(EVENTS).respond(202, json=accepted())

    result = client.events.trigger("order.shipped", "user_1", idempotency_key="order-4821-shipped")

    assert route.calls.last.request.headers["idempotency-key"] == "order-4821-shipped"
    assert result.idempotency_key == "order-4821-shipped"


@respx.mock
def test_two_events_get_two_different_generated_keys(client: Hermesi) -> None:
    route = respx.post(EVENTS).respond(202, json=accepted())

    client.events.trigger("order.shipped", "user_1")
    client.events.trigger("order.shipped", "user_1")

    keys = {call.request.headers["idempotency-key"] for call in route.calls}
    assert len(keys) == 2


@respx.mock
def test_says_when_the_server_replayed_an_earlier_answer(client: Hermesi) -> None:
    respx.post(EVENTS).respond(202, json=accepted(), headers={"Idempotency-Replayed": "true"})

    assert client.events.trigger("order.shipped", "user_1", idempotency_key="k").replayed is True


@respx.mock
def test_a_fresh_event_is_not_a_replay(client: Hermesi) -> None:
    respx.post(EVENTS).respond(202, json=accepted())

    assert client.events.trigger("order.shipped", "user_1").replayed is False


@respx.mock
def test_sends_a_subscriber_described_inline_and_leaves_out_what_is_not_set(client: Hermesi) -> None:
    route = respx.post(EVENTS).respond(202, json=accepted())

    client.events.trigger(
        "order.shipped", Subscriber("cust_1", email="a@example.test", locale="fr", data={"plan": "pro"})
    )

    assert body_of(route)["recipient"] == {
        "external_id": "cust_1",
        "email": "a@example.test",
        "locale": "fr",
        "data": {"plan": "pro"},
    }


@respx.mock
def test_sends_a_list_of_recipients_of_either_kind(client: Hermesi) -> None:
    route = respx.post(EVENTS).respond(202, json=accepted())

    client.events.trigger("order.shipped", ["user_1", Subscriber("user_2", phone_e164="+237670000001")])

    assert body_of(route)["recipient"] == ["user_1", {"external_id": "user_2", "phone_e164": "+237670000001"}]


@respx.mock
def test_sends_the_optional_fields_only_when_given(client: Hermesi) -> None:
    route = respx.post(EVENTS).respond(202, json=accepted())

    client.events.trigger("order.shipped", "user_1")
    assert set(body_of(route)) == {"name", "recipient", "payload"}

    client.events.trigger(
        "order.shipped",
        "user_1",
        actor=Actor(external_id="user_9", name="Ada"),
        delay="15m",
        override={"email": {"subject": "Hi"}},
        tenant="acme",
    )
    assert body_of(route) == {
        "name": "order.shipped",
        "recipient": "user_1",
        "payload": {},
        "actor": {"external_id": "user_9", "name": "Ada"},
        "delay": "15m",
        "override": {"email": {"subject": "Hi"}},
        "tenant": "acme",
    }


@respx.mock
def test_send_at_takes_a_timezone_aware_datetime_or_a_string_and_refuses_a_naive_one(client: Hermesi) -> None:
    route = respx.post(EVENTS).respond(202, json=accepted())

    client.events.trigger("a.sent", "u", send_at=datetime(2026, 10, 5, 9, 30, tzinfo=timezone.utc))
    assert body_of(route)["send_at"] == "2026-10-05T09:30:00+00:00"
    client.events.trigger("a.sent", "u", send_at="2026-10-05T09:30:00Z")
    assert body_of(route)["send_at"] == "2026-10-05T09:30:00Z"

    with pytest.raises(ValueError, match="timezone"):
        client.events.trigger("a.sent", "u", send_at=datetime(2026, 10, 5, 9, 30))


@respx.mock
def test_serialises_the_values_an_event_payload_commonly_holds(client: Hermesi) -> None:
    route = respx.post(EVENTS).respond(202, json=accepted())
    ident = uuid.UUID("12345678-1234-5678-1234-567812345678")

    client.events.trigger(
        "order.shipped",
        "u",
        {
            "at": datetime(2026, 10, 5, 9, 30, tzinfo=timezone.utc),
            "on": date(2026, 10, 5),
            "total": Decimal("12.50"),
            "id": ident,
        },
    )

    assert body_of(route)["payload"] == {
        "at": "2026-10-05T09:30:00+00:00",
        "on": "2026-10-05",
        "total": "12.50",
        "id": "12345678-1234-5678-1234-567812345678",
    }


def test_refuses_a_payload_it_cannot_serialise_before_sending_anything(client: Hermesi) -> None:
    with respx.mock() as router, pytest.raises(TypeError, match="not JSON serializable"):
        client.events.trigger("a.sent", "u", {"x": object()})
    assert router.calls.call_count == 0


def test_requires_a_name(client: Hermesi) -> None:
    with pytest.raises(ValueError, match="name"):
        client.events.trigger("", "user_1")


# --- what the server can answer -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "cls"),
    [
        (401, AuthenticationError),
        (403, ForbiddenError),
        (404, NotFoundError),
        (400, ValidationError),
        (422, ValidationError),
    ],
)
@respx.mock
def test_turns_a_refusal_into_an_exception_of_the_right_class(
    client: Hermesi, status: int, cls: type[HermesiAPIError]
) -> None:
    route = respx.post(EVENTS).respond(
        status,
        json=error_body(
            "subscriber_not_found", "not_found", "No subscriber.", detail=[{"field": "recipient", "issue": "not found"}]
        ),
    )

    with pytest.raises(cls) as caught:
        client.events.trigger("order.shipped", "nobody")

    error = caught.value
    assert (error.status, error.code, error.type, error.message, error.request_id) == (
        status,
        "subscriber_not_found",
        "not_found",
        "No subscriber.",
        "req_1",
    )
    assert [(d.field, d.issue) for d in error.detail] == [("recipient", "not found")]
    assert error.doc_url == "https://d/x"
    assert error.is_retryable is False
    assert route.call_count == 1, "a refusal is not retried"


@respx.mock
def test_the_exception_names_the_code_and_the_request_to_quote(client: Hermesi) -> None:
    respx.post(EVENTS).respond(404, json=error_body("subscriber_not_found", "not_found", "No subscriber."))

    with pytest.raises(NotFoundError) as caught:
        client.events.trigger("order.shipped", "nobody")

    assert "subscriber_not_found" in str(caught.value)
    assert "req_1" in str(caught.value)


@respx.mock
def test_a_5xx_that_outlasts_the_retries_is_a_server_error() -> None:
    from hermesi import RetryPolicy

    with Hermesi(KEY, base_url=BASE, retry=RetryPolicy(max_retries=0)) as hermesi:
        respx.post(EVENTS).respond(503, json=error_body("unavailable"))

        with pytest.raises(ServerError) as caught:
            hermesi.events.trigger("order.shipped", "u")

    assert caught.value.is_retryable is True


@respx.mock
def test_a_429_is_a_rate_limit_error_carrying_retry_after() -> None:
    from hermesi import RetryPolicy

    with Hermesi(KEY, base_url=BASE, retry=RetryPolicy(max_retries=0)) as hermesi:
        respx.post(EVENTS).respond(
            429, json=error_body("rate_limited", "rate_limit_error"), headers={"Retry-After": "7"}
        )

        with pytest.raises(RateLimitError) as caught:
            hermesi.events.trigger("order.shipped", "u")

    assert caught.value.retry_after == 7.0


@respx.mock
def test_an_error_that_is_not_the_apis_envelope_is_reported_as_such(client: Hermesi) -> None:
    from hermesi import RetryPolicy

    with Hermesi(KEY, base_url=BASE, retry=RetryPolicy(max_retries=0)) as hermesi:
        respx.post(EVENTS).respond(502, text="<html>Bad gateway</html>")

        with pytest.raises(HermesiAPIError) as caught:
            hermesi.events.trigger("order.shipped", "u")

    # The API never sends this type, so a caller can tell a response the SDK could not read from the server reporting a failure.
    assert (caught.value.type, caught.value.code, caught.value.status) == ("sdk_error", "unexpected_response", 502)


@respx.mock
def test_a_success_that_is_not_an_event_is_an_error_not_a_made_up_result(client: Hermesi) -> None:
    respx.post(EVENTS).respond(202, json={"something": "else"})

    with pytest.raises(HermesiAPIError) as caught:
        client.events.trigger("order.shipped", "u")

    assert caught.value.code == "unexpected_response"


@respx.mock
def test_a_success_that_is_not_json_is_an_error(client: Hermesi) -> None:
    respx.post(EVENTS).respond(202, text="accepted")

    with pytest.raises(HermesiAPIError):
        client.events.trigger("order.shipped", "u")


@respx.mock
def test_the_exceptions_share_one_base_a_caller_can_catch(client: Hermesi) -> None:
    from hermesi import HermesiError

    respx.post(EVENTS).respond(401, json=error_body("authentication_failed", "authentication_error"))

    with pytest.raises(HermesiError):
        client.events.trigger("order.shipped", "u")


def test_an_injected_http_client_is_used_and_left_open() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(202, json=accepted()))
    http = httpx.Client(transport=transport)

    with Hermesi(KEY, base_url=BASE, http_client=http) as hermesi:
        assert hermesi.events.trigger("order.shipped", "u").event_id.startswith("evt_")

    assert http.is_closed is False
    http.close()
