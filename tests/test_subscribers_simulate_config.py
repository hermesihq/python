from __future__ import annotations

import httpx
import pytest
import respx

from hermesi import AsyncHermesi, Hermesi, HermesiConnectionError, NotFoundError, Subscriber

from .conftest import BASE, KEY, Sleeper, error_body

# --- preference links -----------------------------------------------------------------------------


@respx.mock
def test_mints_a_preference_link(client: Hermesi) -> None:
    route = respx.post(f"{BASE}/v1/subscribers/user_1/preference-link").respond(
        200, json={"url": "https://app.example.test/preferences/abc"}
    )

    link = client.subscribers.preference_link("user_1")

    assert link.url == "https://app.example.test/preferences/abc"
    assert route.calls.last.request.headers["authorization"] == f"Bearer {KEY}"
    assert "idempotency-key" not in route.calls.last.request.headers


@respx.mock
def test_escapes_the_external_id_in_the_path(client: Hermesi) -> None:
    route = respx.post(url__regex=rf"{BASE}/v1/subscribers/.*/preference-link").respond(
        200, json={"url": "https://x/p"}
    )

    client.subscribers.preference_link("user/1 +é")

    assert route.calls.last.request.url.raw_path == b"/v1/subscribers/user%2F1%20%2B%C3%A9/preference-link"


@respx.mock
def test_an_unknown_subscriber_is_a_not_found_error(client: Hermesi) -> None:
    respx.post(f"{BASE}/v1/subscribers/nobody/preference-link").respond(
        404, json=error_body("subscriber_not_found", "not_found")
    )

    with pytest.raises(NotFoundError):
        client.subscribers.preference_link("nobody")


@respx.mock
def test_a_link_response_without_a_url_is_an_error(client: Hermesi) -> None:
    respx.post(f"{BASE}/v1/subscribers/u/preference-link").respond(200, json={"nope": 1})

    with pytest.raises(Exception, match="unexpected_response"):
        client.subscribers.preference_link("u")


def test_requires_an_external_id(client: Hermesi) -> None:
    with pytest.raises(ValueError, match="external_id"):
        client.subscribers.preference_link("")


@respx.mock
def test_retries_a_preference_link_too(client: Hermesi, sleeper: Sleeper) -> None:
    route = respx.post(f"{BASE}/v1/subscribers/u/preference-link").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json={"url": "https://x/p"})]
    )

    assert client.subscribers.preference_link("u").url == "https://x/p"
    assert route.call_count == 2


# --- simulate mode ---------------------------------------------------------------------------------


@respx.mock
def test_simulate_sends_nothing_and_records_the_event() -> None:
    with Hermesi(simulate=True) as hermesi:
        result = hermesi.events.trigger(
            "order.shipped", ["u1", Subscriber("u2", email="a@b.test")], {"id": 1}, idempotency_key="k", delay="5m"
        )

        assert result.status == "simulated"
        assert result.event_id == "evt_simulated_1"
        assert result.idempotency_key == "k"
        [event] = hermesi.simulated
        assert (event.name, event.payload, event.idempotency_key) == ("order.shipped", {"id": 1}, "k")
        assert event.recipient == ["u1", Subscriber("u2", email="a@b.test")]
        assert event.options == {"delay": "5m"}
    assert respx.calls.call_count == 0


def test_simulate_still_validates_what_a_real_call_would() -> None:
    with Hermesi(simulate=True) as hermesi:
        with pytest.raises(ValueError, match="name"):
            hermesi.events.trigger("", "u")
        with pytest.raises(TypeError):
            hermesi.events.trigger("a.sent", "u", {"x": object()})
        assert hermesi.simulated == []


def test_simulate_numbers_its_events_and_makes_a_preference_link() -> None:
    with Hermesi(simulate=True) as hermesi:
        first = hermesi.events.trigger("a.sent", "u")
        second = hermesi.events.trigger("a.sent", "u")

        assert (first.event_id, second.event_id) == ("evt_simulated_1", "evt_simulated_2")
        assert hermesi.subscribers.preference_link("user/1").url == "https://simulated.invalid/preferences/user%2F1"
        assert hermesi.tokens.mint("u", environment_id="env_1").count(".") == 1


# --- configuration ---------------------------------------------------------------------------------


def test_requires_a_key_and_a_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HERMESI_SECRET_KEY", raising=False)
    monkeypatch.delenv("HERMESI_BASE_URL", raising=False)

    with pytest.raises(ValueError, match="api_key"):
        Hermesi(None, base_url=BASE)
    with pytest.raises(ValueError, match="base_url"):
        Hermesi(KEY)


def test_reads_the_key_and_the_base_url_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMESI_SECRET_KEY", KEY)
    monkeypatch.setenv("HERMESI_BASE_URL", BASE + "/")

    with respx.mock() as router:
        route = router.post(f"{BASE}/v1/events").respond(202, json={"event_id": "evt_1"})
        with Hermesi() as hermesi:
            hermesi.events.trigger("a.sent", "u")

    assert route.call_count == 1, "the trailing slash on the base URL does not double up"


def test_refuses_a_public_key_with_a_reason() -> None:
    with pytest.raises(ValueError, match="secret key"):
        Hermesi("hm_pk_prod_public", base_url=BASE)


def test_the_repr_never_shows_the_key() -> None:
    hermesi = Hermesi(KEY, base_url=BASE)

    assert KEY not in repr(hermesi)
    assert BASE in repr(hermesi)
    hermesi.close()


def test_closes_the_client_it_made_and_not_the_one_it_was_given() -> None:
    http = httpx.Client()
    owned = Hermesi(KEY, base_url=BASE)
    borrowed = Hermesi(KEY, base_url=BASE, http_client=http)

    owned.close()
    borrowed.close()

    assert owned._http.is_closed is True
    assert http.is_closed is False
    http.close()


# --- the asynchronous client ------------------------------------------------------------------------


async def no_sleep(_: float) -> None:
    return None


@respx.mock
async def test_the_async_client_publishes_retries_and_raises_like_the_sync_one() -> None:
    waits: list[float] = []

    async def record(seconds: float) -> None:
        waits.append(seconds)

    route = respx.post(f"{BASE}/v1/events").mock(
        side_effect=[
            httpx.Response(429, json=error_body("rate_limited", "rate_limit_error"), headers={"Retry-After": "3"}),
            httpx.Response(202, json={"event_id": "evt_1"}),
        ]
    )
    async with AsyncHermesi(KEY, base_url=BASE, sleep=record) as hermesi:
        result = await hermesi.events.trigger("order.shipped", "u", {"a": 1}, idempotency_key="k")

    assert result.event_id == "evt_1"
    assert waits == [3.0]
    assert [call.request.headers["idempotency-key"] for call in route.calls] == ["k", "k"]


@respx.mock
async def test_the_async_client_reports_a_lost_connection_and_a_refusal() -> None:
    respx.post(f"{BASE}/v1/events").mock(side_effect=httpx.ConnectError("refused"))
    async with AsyncHermesi(KEY, base_url=BASE, sleep=no_sleep) as hermesi:
        with pytest.raises(HermesiConnectionError):
            await hermesi.events.trigger("a.sent", "u")

    respx.post(f"{BASE}/v1/events").respond(404, json=error_body("subscriber_not_found", "not_found"))
    async with AsyncHermesi(KEY, base_url=BASE, sleep=no_sleep) as hermesi:
        with pytest.raises(NotFoundError):
            await hermesi.events.trigger("a.sent", "nobody")


@respx.mock
async def test_the_async_client_mints_links_and_simulates() -> None:
    respx.post(f"{BASE}/v1/subscribers/u/preference-link").respond(200, json={"url": "https://x/p"})
    async with AsyncHermesi(KEY, base_url=BASE) as hermesi:
        assert (await hermesi.subscribers.preference_link("u")).url == "https://x/p"
        assert hermesi.tokens.mint("u", environment_id="env_1")

    async with AsyncHermesi(simulate=True) as simulated:
        result = await simulated.events.trigger("a.sent", "u")
        assert result.status == "simulated"
        assert (await simulated.subscribers.preference_link("u")).url.startswith("https://simulated.invalid/")
