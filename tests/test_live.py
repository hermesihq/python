"""The SDK against a real Hermesi, not a fake of one.

Skipped unless the four variables below are set. These exist because tests written against a fake of the server prove the
client and not the contract: a client can pass hundreds of them and still disagree with the server about a path, a header or
a format. Run them against a development instance of Hermesi (never production: they publish events):

    HERMESI_LIVE_URL=http://localhost:8010 \\
    HERMESI_LIVE_SECRET_KEY=hm_sk_... \\
    HERMESI_LIVE_PUBLIC_KEY=hm_pk_... \\
    HERMESI_LIVE_ENVIRONMENT_ID=env_... \\
    HERMESI_LIVE_SUBSCRIBER=user_1 \\
    pytest tests/test_live.py

The subscriber must already exist in that environment.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import httpx
import pytest

from hermesi import (
    AuthenticationError,
    Hermesi,
    HermesiConnectionError,
    NotFoundError,
    RetryPolicy,
    Subscriber,
    ValidationError,
)

URL = os.environ.get("HERMESI_LIVE_URL", "")
SECRET = os.environ.get("HERMESI_LIVE_SECRET_KEY", "")
PUBLIC = os.environ.get("HERMESI_LIVE_PUBLIC_KEY", "")
ENVIRONMENT = os.environ.get("HERMESI_LIVE_ENVIRONMENT_ID", "")
SUBSCRIBER = os.environ.get("HERMESI_LIVE_SUBSCRIBER", "")

pytestmark = pytest.mark.skipif(
    not (URL and SECRET and PUBLIC and ENVIRONMENT and SUBSCRIBER),
    reason="set HERMESI_LIVE_URL, _SECRET_KEY, _PUBLIC_KEY, _ENVIRONMENT_ID and _SUBSCRIBER to run against a real Hermesi",
)


@pytest.fixture
def live() -> Iterator[Hermesi]:
    with Hermesi(SECRET, base_url=URL, retry=RetryPolicy(max_retries=0)) as hermesi:
        yield hermesi


def test_publishes_an_event_and_the_server_accepts_it(live: Hermesi) -> None:
    result = live.events.trigger("order.shipped", SUBSCRIBER, {"order_id": "4821"})

    assert result.event_id.startswith("evt_")
    assert result.status == "accepted"
    assert result.replayed is False


def test_the_same_idempotency_key_is_recognised_as_a_replay(live: Hermesi) -> None:
    key = f"live-{uuid.uuid4()}"

    first = live.events.trigger("order.shipped", SUBSCRIBER, {"order_id": "1"}, idempotency_key=key)
    second = live.events.trigger("order.shipped", SUBSCRIBER, {"order_id": "1"}, idempotency_key=key)

    assert first.replayed is False
    assert second.replayed is True
    assert second.event_id == first.event_id


def test_an_inline_subscriber_is_created_on_the_fly(live: Hermesi) -> None:
    result = live.events.trigger(
        "order.shipped", Subscriber(f"live_{uuid.uuid4().hex[:8]}", email="live@example.test", locale="fr")
    )

    assert result.event_id.startswith("evt_")


def test_a_list_of_recipients_is_accepted(live: Hermesi) -> None:
    result = live.events.trigger("order.shipped", [SUBSCRIBER, Subscriber(f"live_{uuid.uuid4().hex[:8]}")])

    assert result.event_id.startswith("evt_")


def test_a_recipient_that_is_not_a_subscriber_is_a_not_found_error(live: Hermesi) -> None:
    with pytest.raises(NotFoundError) as caught:
        live.events.trigger("order.shipped", f"nobody_{uuid.uuid4().hex}")

    assert caught.value.code == "subscriber_not_found"
    assert caught.value.request_id.startswith("req_")


def test_an_event_name_the_server_does_not_accept_is_a_validation_error(live: Hermesi) -> None:
    with pytest.raises(ValidationError) as caught:
        live.events.trigger("notadottedname", SUBSCRIBER)

    assert caught.value.status in (400, 422)
    assert caught.value.detail or caught.value.code


def test_a_wrong_key_is_an_authentication_error() -> None:
    with Hermesi("hm_sk_prod_not_a_real_key", base_url=URL, retry=RetryPolicy(max_retries=0)) as hermesi:
        with pytest.raises(AuthenticationError) as caught:
            hermesi.events.trigger("order.shipped", SUBSCRIBER)

    assert caught.value.status == 401


def test_a_preference_link_is_minted(live: Hermesi) -> None:
    link = live.subscribers.preference_link(SUBSCRIBER)

    assert link.url.startswith("http")
    assert "/preferences/" in link.url


def test_a_preference_link_for_an_id_with_characters_a_path_treats_specially(live: Hermesi) -> None:
    """The same class of id once made the server answer 404 for a subscriber that exists."""
    subscriber = f"team/{uuid.uuid4().hex[:8]} é?#"
    live.events.trigger("order.shipped", Subscriber(subscriber))

    link = live.subscribers.preference_link(subscriber)

    assert "/preferences/" in link.url


def test_a_minted_token_is_accepted_by_the_client_api(live: Hermesi) -> None:
    """The token format is the one thing the server verifies cryptographically, so only the real server can say it is right."""
    token = live.tokens.mint(SUBSCRIBER, environment_id=ENVIRONMENT)

    response = httpx.get(
        f"{URL}/v1/client/inbox/counts",
        headers={"Authorization": f"Bearer {PUBLIC}", "X-Hermesi-Subscriber-Token": token},
    )

    assert response.status_code == 200, response.text
    assert set(response.json()) >= {"unread", "unseen"}


def test_a_token_for_another_environment_is_refused_by_the_client_api(live: Hermesi) -> None:
    token = live.tokens.mint(SUBSCRIBER, environment_id="env_not_this_one")

    response = httpx.get(
        f"{URL}/v1/client/inbox/counts",
        headers={"Authorization": f"Bearer {PUBLIC}", "X-Hermesi-Subscriber-Token": token},
    )

    assert response.status_code == 401


def test_an_unreachable_server_is_a_connection_error() -> None:
    with Hermesi(SECRET, base_url="http://127.0.0.1:9", retry=RetryPolicy(max_retries=0), timeout=2) as hermesi:
        with pytest.raises(HermesiConnectionError):
            hermesi.events.trigger("order.shipped", SUBSCRIBER)
