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

The subscriber must already exist in that environment. The tests that send a direct message or write preferences also need, in that
environment, a published `sms` template whose key is HERMESI_LIVE_SMS_TEMPLATE (its text may use `{{ payload.code }}`) and a
non-critical category whose key is HERMESI_LIVE_CATEGORY; without them those tests are skipped.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import httpx
import pytest

from hermesi import (
    AuthenticationError,
    ConflictError,
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
SMS_TEMPLATE = os.environ.get("HERMESI_LIVE_SMS_TEMPLATE", "")
CATEGORY = os.environ.get("HERMESI_LIVE_CATEGORY", "")

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


# --- the rest of the server API: events read back, subscribers, direct messages --------------------


def _fresh() -> str:
    return f"live_{uuid.uuid4().hex[:10]}"


def test_an_event_is_read_back_with_its_notification(live: Hermesi) -> None:
    sent = live.events.trigger("order.shipped", SUBSCRIBER, {"order_id": "live"})

    run = live.events.get(sent.event_id)

    assert run.event_id == sent.event_id and run.name == "order.shipped"
    assert run.payload == {"order_id": "live"}
    assert [n.external_id for n in run.notifications] == [SUBSCRIBER]


def test_an_event_that_does_not_exist_is_not_found(live: Hermesi) -> None:
    with pytest.raises(NotFoundError) as caught:
        live.events.get("evt_01DOESNOTEXIST00000000000")

    assert caught.value.code == "event_not_found"


def test_a_subscriber_is_created_read_updated_and_deleted(live: Hermesi) -> None:
    sid = _fresh()

    created = live.subscribers.put(sid, email="Live@Example.test", first_name="Live", locale="fr", data={"plan": "pro", "seats": 3})
    assert created.external_id == sid and created.id.startswith("sub_")
    assert created.email == "live@example.test", "stored lower-cased"
    assert created.data == {"plan": "pro", "seats": 3}

    unchanged = live.subscribers.put(sid, locale="en")
    assert (unchanged.locale, unchanged.first_name, unchanged.email) == ("en", "Live", "live@example.test"), "a field left out is left alone"

    cleared = live.subscribers.put(sid, first_name=None)
    assert cleared.first_name is None and cleared.email == "live@example.test", "None clears one field and only that"

    replaced = live.subscribers.put(sid, data={"plan": "free"})
    assert replaced.data == {"plan": "free"}, "data replaces, it is not merged"

    assert live.subscribers.get(sid).data == {"plan": "free"}
    assert live.subscribers.patch(sid, phone_e164="+237690000000").phone_e164 == "+237690000000"

    live.subscribers.delete(sid)
    live.subscribers.delete(sid)  # deleting again is not an error
    with pytest.raises(NotFoundError):
        live.subscribers.get(sid)


def test_a_value_of_the_wrong_shape_is_a_validation_error_naming_the_field(live: Hermesi) -> None:
    with pytest.raises(ValidationError) as caught:
        live.subscribers.put(_fresh(), phone_e164="690000000")

    assert any("phone_e164" in (d.field or "") for d in caught.value.detail)


def test_patching_a_subscriber_that_does_not_exist_is_not_found(live: Hermesi) -> None:
    with pytest.raises(NotFoundError) as caught:
        live.subscribers.patch(_fresh(), locale="en")

    assert caught.value.code == "subscriber_not_found"


def test_an_id_with_characters_a_path_treats_specially_works_for_every_subscriber_call(live: Hermesi) -> None:
    sid = f"team/{_fresh()} é?#"

    live.subscribers.put(sid, locale="fr")

    assert live.subscribers.get(sid).external_id == sid
    live.subscribers.register_channel(sid, "push", "tok-1")
    assert [c.identifier for c in live.subscribers.get(sid).channels] == ["tok-1"]
    live.subscribers.delete(sid)


def test_a_device_token_is_registered_listed_and_removed(live: Hermesi) -> None:
    sid = _fresh()
    live.subscribers.put(sid)

    first = live.subscribers.register_channel(sid, "push", "fcm-token-1", {"platform": "android"})
    again = live.subscribers.register_channel(sid, "push", "fcm-token-1", {"platform": "android"})

    assert (first.state, again.state) == ("active", "active")
    channels = live.subscribers.get(sid).channels
    assert [(c.channel, c.identifier, c.metadata) for c in channels] == [("push", "fcm-token-1", {"platform": "android"})]
    live.subscribers.remove_channel(sid, "push", "fcm-token-1")
    live.subscribers.remove_channel(sid, "push", "fcm-token-1")
    assert live.subscribers.get(sid).channels == []
    live.subscribers.delete(sid)


def test_an_identifier_that_is_a_url_survives_the_round_trip(live: Hermesi) -> None:
    """A Web Push endpoint is a URL: it has slashes, which a path segment can only carry percent-encoded."""
    sid = _fresh()
    live.subscribers.put(sid)
    identifier = "https://fcm.googleapis.com/fcm/send/abc:APA91b/def"
    try:
        live.subscribers.register_channel(sid, "push", identifier, {"transport": "fcm"})
    except ValidationError:
        pytest.skip("this deployment refuses that identifier shape for push")

    live.subscribers.remove_channel(sid, "push", identifier)

    assert live.subscribers.get(sid).channels == []
    live.subscribers.delete(sid)


@pytest.mark.skipif(not CATEGORY, reason="set HERMESI_LIVE_CATEGORY to a non-critical category key")
def test_preferences_are_written_read_and_removed(live: Hermesi) -> None:
    sid = _fresh()
    live.subscribers.put(sid)

    after = live.subscribers.update_preferences(sid, global_={"sms": False}, categories={CATEGORY: {"email": False, "push": False}})
    assert after.global_ == {"sms": False} and after.categories == {CATEGORY: {"email": False, "push": False}}

    removed = live.subscribers.update_preferences(sid, global_={"sms": None}, categories={CATEGORY: {"email": None}})
    assert removed.global_ == {} and removed.categories == {CATEGORY: {"push": False}}
    assert live.subscribers.preferences(sid) == removed
    live.subscribers.delete(sid)


def test_an_unknown_category_refuses_the_whole_preference_update(live: Hermesi) -> None:
    sid = _fresh()
    live.subscribers.put(sid)

    with pytest.raises(NotFoundError) as caught:
        live.subscribers.update_preferences(sid, global_={"sms": False}, categories={"no_such_category": {"email": False}})

    assert caught.value.code == "category_not_found"
    assert live.subscribers.preferences(sid).global_ == {}, "nothing was applied"
    live.subscribers.delete(sid)


@pytest.mark.skipif(not SMS_TEMPLATE, reason="set HERMESI_LIVE_SMS_TEMPLATE to the key of a published sms template")
def test_a_direct_message_is_sent_replayed_and_read_back(live: Hermesi) -> None:
    sid = _fresh()
    live.subscribers.put(sid, phone_e164="+237690000001")
    key = f"live-{uuid.uuid4()}"

    first = live.messages.send("sms", sid, SMS_TEMPLATE, data={"code": "480219"}, idempotency_key=key)
    second = live.messages.send("sms", sid, SMS_TEMPLATE, data={"code": "480219"}, idempotency_key=key)

    assert first.status == "queued" and first.replayed is False and first.message_id.startswith("msg_")
    assert second.replayed is True and second.message_id == first.message_id
    message = live.messages.get(first.message_id)
    assert message.id == first.message_id and message.channel == "sms"
    with pytest.raises(ConflictError):
        live.messages.send("sms", sid, SMS_TEMPLATE, data={"code": "111111"}, idempotency_key=key)
    live.subscribers.delete(sid)


@pytest.mark.skipif(not SMS_TEMPLATE, reason="set HERMESI_LIVE_SMS_TEMPLATE to the key of a published sms template")
def test_a_direct_message_to_someone_with_no_phone_is_reported_not_raised(live: Hermesi) -> None:
    sid = _fresh()
    live.subscribers.put(sid, email="nophone@example.test")

    result = live.messages.send("sms", sid, SMS_TEMPLATE, data={"code": "1"})

    assert result.status == "skipped" and result.messages[0].reason == "no_channel_identity"
    live.subscribers.delete(sid)


def test_a_direct_message_with_an_unknown_template_is_not_found(live: Hermesi) -> None:
    with pytest.raises(NotFoundError) as caught:
        live.messages.send("sms", SUBSCRIBER, "no-such-template")

    assert caught.value.code == "template_not_found"


def test_the_server_refuses_inline_content_instead_of_ignoring_it() -> None:
    """The SDK has no `content` argument at all, so this is asserted against the server directly."""
    response = httpx.post(
        f"{URL}/v1/messages",
        headers={"Authorization": f"Bearer {SECRET}"},
        json={"channel": "sms", "recipient": SUBSCRIBER, "template": "x", "content": {"body": "hi"}},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "inline_content_not_supported"


def test_a_bulk_import_creates_then_updates_and_each_row_means_what_a_put_would(live: Hermesi) -> None:
    a, b, c = _fresh(), _fresh(), _fresh()

    first = live.subscribers.bulk(
        [
            {"external_id": a, "email": "Bulk.A@Example.test", "first_name": "Aa", "data": {"plan": "pro"}},
            {"external_id": b, "phone_e164": "+237690000010", "locale": "fr"},
        ]
    )

    assert (first.created, first.updated) == (2, 0)
    assert [(r.external_id, r.status) for r in first.subscribers] == [(a, "created"), (b, "created")]
    assert live.subscribers.get(a).email == "bulk.a@example.test", "lower-cased, as a put does"

    second = live.subscribers.bulk([{"external_id": a, "first_name": None, "data": {"seats": 3}}, {"external_id": b, "locale": "en"}, {"external_id": c}])

    assert [r.status for r in second.subscribers] == ["updated", "updated", "created"]
    after_a, after_b = live.subscribers.get(a), live.subscribers.get(b)
    assert (after_a.email, after_a.first_name, after_a.data) == ("bulk.a@example.test", None, {"seats": 3}), "left out kept, None cleared, data replaced"
    assert (after_b.phone_e164, after_b.locale) == ("+237690000010", "en")
    for sid in (a, b, c):
        live.subscribers.delete(sid)


def test_one_invalid_row_refuses_the_whole_batch_and_writes_nothing(live: Hermesi) -> None:
    good, bad = _fresh(), _fresh()

    with pytest.raises(ValidationError) as caught:
        live.subscribers.bulk([{"external_id": good, "email": "good@example.test"}, {"external_id": bad, "phone_e164": "690000000"}])

    assert any("subscribers.1.phone_e164" in (d.field or "") for d in caught.value.detail)
    with pytest.raises(NotFoundError):
        live.subscribers.get(good)


def test_the_same_id_twice_in_a_batch_is_refused(live: Hermesi) -> None:
    sid = _fresh()

    with pytest.raises(ValidationError) as caught:
        live.subscribers.bulk([{"external_id": sid}, {"external_id": sid}])

    assert "more than once" in str(caught.value) or any("more than once" in (d.issue or "") for d in caught.value.detail)


def test_an_id_with_characters_a_path_treats_specially_is_ordinary_in_a_bulk_body(live: Hermesi) -> None:
    sid = f"team/{_fresh()} é?#"

    live.subscribers.bulk([{"external_id": sid, "locale": "fr"}])

    assert live.subscribers.get(sid).locale == "fr"
    live.subscribers.delete(sid)
