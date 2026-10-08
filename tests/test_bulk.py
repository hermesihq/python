"""``subscribers.bulk``: up to 1 000 upserts in one request (``POST /v1/subscribers/bulk``).

Each row has to mean what the same ``put`` would, which for a sync job comes down to one distinction: a key present with ``None``
clears the field, and a key absent leaves it alone. A mapping carries that as naturally as keyword arguments do, so these tests hold
it, and that a typo is refused naming its row instead of being dropped, which in a bulk import means a column of the file that is
silently never applied.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from hermesi import UNSET, AsyncHermesi, Hermesi, HermesiSimulationError, ValidationError

from .conftest import BASE, FAST, KEY, Sleeper, error_body

ANSWER: dict[str, Any] = {
    "created": 2,
    "updated": 1,
    "subscribers": [
        {"external_id": "user_1", "id": "sub_1", "status": "created"},
        {"external_id": "user_2", "id": "sub_2", "status": "updated"},
        {"external_id": "user_3", "id": "sub_3", "status": "created"},
    ],
}


@pytest.fixture
def client() -> Hermesi:
    return Hermesi(KEY, base_url=BASE, retry=FAST)


def sent(route: respx.Route) -> dict[str, Any]:
    return json.loads(route.calls.last.request.content)  # type: ignore[no-any-return]


@respx.mock
def test_a_batch_is_one_post_and_the_result_says_what_happened_to_each_row(client: Hermesi) -> None:
    route = respx.post(f"{BASE}/v1/subscribers/bulk").respond(200, json=ANSWER)

    result = client.subscribers.bulk([{"external_id": "user_1", "email": "a@example.cm"}, {"external_id": "user_2", "locale": "fr"}])

    assert route.call_count == 1
    request = route.calls.last.request
    assert request.headers["authorization"] == f"Bearer {KEY}" and "idempotency-key" not in request.headers
    assert sent(route) == {"subscribers": [{"external_id": "user_1", "email": "a@example.cm"}, {"external_id": "user_2", "locale": "fr"}]}
    assert (result.created, result.updated) == (2, 1), "the two counts are not interchangeable"
    assert [(r.external_id, r.id, r.status) for r in result.subscribers] == [
        ("user_1", "sub_1", "created"),
        ("user_2", "sub_2", "updated"),
        ("user_3", "sub_3", "created"),
    ]


@respx.mock
def test_a_key_with_none_clears_and_a_key_left_out_is_left_alone(client: Hermesi) -> None:
    route = respx.post(f"{BASE}/v1/subscribers/bulk").respond(200, json=ANSWER)

    client.subscribers.bulk([{"external_id": "u", "phone_e164": None, "data": None}, {"external_id": "v"}])

    assert sent(route)["subscribers"] == [{"external_id": "u", "phone_e164": None, "data": None}, {"external_id": "v"}]


@respx.mock
def test_unset_means_the_same_as_leaving_the_key_out(client: Hermesi) -> None:
    route = respx.post(f"{BASE}/v1/subscribers/bulk").respond(200, json=ANSWER)

    client.subscribers.bulk([{"external_id": "u", "email": UNSET, "locale": "fr"}])

    assert sent(route)["subscribers"] == [{"external_id": "u", "locale": "fr"}]


@respx.mock
def test_any_iterable_of_rows_works_including_a_generator(client: Hermesi) -> None:
    route = respx.post(f"{BASE}/v1/subscribers/bulk").respond(200, json=ANSWER)

    client.subscribers.bulk({"external_id": f"u{i}"} for i in range(3))

    assert [row["external_id"] for row in sent(route)["subscribers"]] == ["u0", "u1", "u2"]


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([], "at least one subscriber"),
        ([{"email": "a@example.cm"}], "row 0: external_id is required"),
        ([{"external_id": "a"}, {"external_id": ""}], "row 1: external_id is required"),
        ([{"external_id": "a"}, {"external_id": 7}], "row 1: external_id is required"),
        ([{"external_id": "a", "phone": "+237690000000"}], 'row 0: unknown subscriber field "phone"'),
        ([{"external_id": "a"}, {"external_id": "b", "phoneE164": "+1"}], 'row 1: unknown subscriber field "phoneE164"'),
    ],
)
@respx.mock
def test_a_row_that_cannot_be_right_is_refused_naming_it_before_any_request(client: Hermesi, rows: list[Any], message: str) -> None:
    route = respx.post(f"{BASE}/v1/subscribers/bulk").respond(200, json=ANSWER)

    with pytest.raises((ValueError, TypeError), match=message):
        client.subscribers.bulk(rows)

    assert route.call_count == 0


@respx.mock
def test_a_row_that_is_not_a_mapping_is_a_type_error(client: Hermesi) -> None:
    with pytest.raises(TypeError, match="row 1 must be a mapping"):
        client.subscribers.bulk([{"external_id": "a"}, "user_2"])  # type: ignore[list-item]


@respx.mock
def test_the_servers_refusal_lists_every_problem_with_its_row(client: Hermesi) -> None:
    respx.post(f"{BASE}/v1/subscribers/bulk").respond(
        422,
        json=error_body(
            "validation_error",
            "validation_error",
            detail=[
                {"field": "body.subscribers.0.phone_e164", "issue": "Value error, not an E.164 phone number"},
                {"field": "body.subscribers.2.locale", "issue": "Value error, not a language tag"},
            ],
        ),
    )

    with pytest.raises(ValidationError) as caught:
        client.subscribers.bulk([{"external_id": "a"}, {"external_id": "b"}, {"external_id": "c"}])

    assert [d.field for d in caught.value.detail] == ["body.subscribers.0.phone_e164", "body.subscribers.2.locale"]


@respx.mock
def test_a_batch_is_retried_like_any_idempotent_write(sleeper: Sleeper) -> None:
    route = respx.post(f"{BASE}/v1/subscribers/bulk").mock(
        side_effect=[httpx.Response(503, json=error_body("unavailable", "api_error")), httpx.Response(200, json=ANSWER)]
    )
    client = Hermesi(KEY, base_url=BASE, retry=FAST, sleep=sleeper)

    result = client.subscribers.bulk([{"external_id": "user_1"}])

    assert route.call_count == 2 and result.created == 2


@respx.mock
def test_an_answer_without_the_subscribers_list_is_not_believed(client: Hermesi) -> None:
    respx.post(f"{BASE}/v1/subscribers/bulk").respond(200, json={"created": 3})

    with pytest.raises(Exception, match="unexpected_response"):
        client.subscribers.bulk([{"external_id": "user_1"}])


def test_simulate_records_the_call_and_answers_plausibly_without_sending() -> None:
    with Hermesi(simulate=True) as hermesi:
        result = hermesi.subscribers.bulk([{"external_id": "a", "email": "a@example.cm"}, {"external_id": "b"}])

        assert (result.created, result.updated) == (2, 0)
        assert [(r.external_id, r.status) for r in result.subscribers] == [("a", "created"), ("b", "created")]
        call = hermesi.simulated_calls[0]
        assert (call.method, call.path) == ("POST", "/v1/subscribers/bulk")
        assert call.body == {"subscribers": [{"external_id": "a", "email": "a@example.cm"}, {"external_id": "b"}]}


def test_simulate_validates_rows_exactly_as_a_real_call_does() -> None:
    with Hermesi(simulate=True) as hermesi:
        with pytest.raises(ValueError, match='unknown subscriber field "phone"'):
            hermesi.subscribers.bulk([{"external_id": "a", "phone": "+1"}])

        assert hermesi.simulated_calls == []
        assert isinstance(HermesiSimulationError(), Exception)


@respx.mock
async def test_the_async_client_has_it_too() -> None:
    route = respx.post(f"{BASE}/v1/subscribers/bulk").respond(200, json=ANSWER)

    async with AsyncHermesi(KEY, base_url=BASE, retry=FAST) as hermesi:
        result = await hermesi.subscribers.bulk([{"external_id": "user_1", "email": None}])

    assert sent(route) == {"subscribers": [{"external_id": "user_1", "email": None}]}
    assert result.created == 2
