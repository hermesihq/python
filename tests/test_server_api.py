"""Events read-back, subscribers, and direct messages: the rest of the server API (spec 6.3 to 6.5).

Two rules matter more here than the paths. A subscriber write must tell "clear this field" (``None``) from "leave it alone" (the
argument left out), because a sync job that knows half a profile must not blank the other half. And a direct message must keep one
idempotency key across its retries, because for an OTP the difference is one SMS or two.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from hermesi import (
    UNSET,
    AsyncHermesi,
    ConflictError,
    Hermesi,
    HermesiSimulationError,
    NotFoundError,
    Subscriber,
    ValidationError,
)

from .conftest import BASE, FAST, KEY, Sleeper, error_body

PROFILE: dict[str, Any] = {
    "id": "sub_1",
    "external_id": "user_8821",
    "email": "amina@example.cm",
    "phone_e164": "+237690000000",
    "first_name": "Amina",
    "last_name": None,
    "locale": "fr",
    "timezone": "Africa/Douala",
    "avatar_url": None,
    "data": {"plan": "pro"},
    "created_at": "2026-10-06T10:00:00Z",
    "updated_at": "2026-10-06T10:00:00Z",
    "channels": [
        {"channel": "push", "identifier": "fcm_1", "state": "active", "state_reason": None, "metadata": {"platform": "android"},
         "verified_at": None, "last_used_at": "2026-10-06T10:00:00Z"}
    ],
    "preferences": {"global": {"sms": False}, "categories": {"marketing": {"email": False}}},
}


def body_of(route: respx.Route) -> dict[str, Any]:
    return json.loads(route.calls.last.request.content)  # type: ignore[no-any-return]


# --- reading back an event ---------------------------------------------------------------------


EVENT_RUN = {
    "event_id": "evt_1",
    "name": "order.shipped",
    "status": "processed",
    "received_at": "2026-10-06T10:00:00Z",
    "processed_at": None,
    "payload": {"order_id": "4821"},
    "actor": None,
    "idempotency_key": "order-4821",
    "error": None,
    "notifications": [
        {
            "id": "not_1", "subscriber_id": "sub_1", "external_id": "user_8821", "workflow": "order-shipped", "workflow_version": 3,
            "status": "completed", "created_at": "2026-10-06T10:00:00Z", "started_at": None, "completed_at": None, "resume_at": None,
            "messages": [
                {"id": "msg_1", "channel": "email", "step_key": "mail", "status": "delivered", "provider": "resend", "failure_code": None,
                 "failure_message": None, "created_at": "2026-10-06T10:00:00Z", "terminal_at": "2026-10-06T10:00:05Z"},
                {"id": "msg_2", "channel": "sms", "step_key": "sms", "status": "queued", "provider": None, "failure_code": None,
                 "failure_message": None, "created_at": "2026-10-06T10:00:00Z", "terminal_at": None},
            ],
        }
    ],
}


@respx.mock
def test_reads_back_an_event_with_its_notifications_and_messages(client: Hermesi) -> None:
    route = respx.get(f"{BASE}/v1/events/evt_1").respond(200, json=EVENT_RUN)

    run = client.events.get("evt_1")

    assert route.calls.last.request.headers["authorization"] == f"Bearer {KEY}"
    assert (run.event_id, run.name, run.status, run.payload) == ("evt_1", "order.shipped", "processed", {"order_id": "4821"})
    [notification] = run.notifications
    assert (notification.external_id, notification.workflow, notification.workflow_version) == ("user_8821", "order-shipped", 3)
    assert [(m.id, m.status, m.provider, m.is_final) for m in run.messages] == [
        ("msg_1", "delivered", "resend", True),
        ("msg_2", "queued", None, False),
    ]


@respx.mock
def test_an_unknown_event_is_a_not_found_error(client: Hermesi) -> None:
    respx.get(f"{BASE}/v1/events/evt_nope").respond(404, json=error_body("event_not_found", "not_found"))

    with pytest.raises(NotFoundError) as caught:
        client.events.get("evt_nope")

    assert caught.value.code == "event_not_found"


def test_an_id_that_a_url_would_resolve_is_refused_before_anything_is_sent(client: Hermesi) -> None:
    for bad in ("..", ".", ""):
        with pytest.raises(ValueError):
            client.events.get(bad)


@respx.mock
def test_a_read_is_retried_on_a_server_error_like_everything_else(sleeper: Sleeper) -> None:
    route = respx.get(f"{BASE}/v1/events/evt_1").mock(
        side_effect=[httpx.Response(503, json=error_body()), httpx.Response(200, json=EVENT_RUN)]
    )
    with Hermesi(KEY, base_url=BASE, retry=FAST, sleep=sleeper) as hermesi:
        run = hermesi.events.get("evt_1")

    assert run.event_id == "evt_1" and route.call_count == 2 and len(sleeper.waits) == 1


# --- subscribers: put and patch ----------------------------------------------------------------


@respx.mock
def test_put_sends_only_the_fields_it_was_given(client: Hermesi) -> None:
    route = respx.put(f"{BASE}/v1/subscribers/user_8821").respond(200, json=PROFILE)

    profile = client.subscribers.put("user_8821", email="amina@example.cm", locale="fr")

    assert body_of(route) == {"email": "amina@example.cm", "locale": "fr"}, "a field left out is not in the body, so the server leaves it"
    assert (profile.id, profile.email, profile.phone_e164, profile.first_name) == ("sub_1", "amina@example.cm", "+237690000000", "Amina")
    assert profile.last_name is None and profile.data == {"plan": "pro"}
    assert [(c.channel, c.identifier, c.state, c.metadata) for c in profile.channels] == [("push", "fcm_1", "active", {"platform": "android"})]
    assert profile.preferences.global_ == {"sms": False} and profile.preferences.categories == {"marketing": {"email": False}}


@respx.mock
def test_none_clears_a_field_and_leaving_it_out_does_not(client: Hermesi) -> None:
    route = respx.put(f"{BASE}/v1/subscribers/user_8821").respond(200, json=PROFILE)

    client.subscribers.put("user_8821", last_name=None, data=None)

    sent = body_of(route)
    assert sent == {"last_name": None, "data": None}, "null is sent as null: that is how a field is cleared"
    assert "email" not in sent


@respx.mock
def test_data_is_sent_whole_because_the_server_replaces_it(client: Hermesi) -> None:
    route = respx.put(f"{BASE}/v1/subscribers/u").respond(200, json=PROFILE)

    client.subscribers.put("u", data={"plan": "free"})

    assert body_of(route) == {"data": {"plan": "free"}}


@respx.mock
def test_patch_uses_patch(client: Hermesi) -> None:
    route = respx.patch(f"{BASE}/v1/subscribers/user_8821").respond(200, json=PROFILE)

    client.subscribers.patch("user_8821", phone_e164=None)

    assert route.called and body_of(route) == {"phone_e164": None}


@respx.mock
def test_patching_a_subscriber_that_does_not_exist_is_not_found(client: Hermesi) -> None:
    respx.patch(f"{BASE}/v1/subscribers/nobody").respond(404, json=error_body("subscriber_not_found", "not_found"))

    with pytest.raises(NotFoundError) as caught:
        client.subscribers.patch("nobody", locale="en")

    assert caught.value.code == "subscriber_not_found"


@respx.mock
def test_a_value_the_server_refuses_names_the_field(client: Hermesi) -> None:
    respx.put(f"{BASE}/v1/subscribers/u").respond(
        422, json=error_body("validation_error", "validation_error", detail=[{"field": "body.phone_e164", "issue": "not an E.164 phone number"}])
    )

    with pytest.raises(ValidationError) as caught:
        client.subscribers.put("u", phone_e164="690000000")

    assert [(d.field, d.issue) for d in caught.value.detail] == [("body.phone_e164", "not an E.164 phone number")]


@respx.mock
def test_an_id_with_characters_a_path_treats_specially_is_encoded_as_one_segment(client: Hermesi) -> None:
    route = respx.put(url__regex=rf"{BASE}/v1/subscribers/.*").respond(200, json=PROFILE)

    client.subscribers.put("team/amina é?#", locale="fr")

    assert route.calls.last.request.url.raw_path.decode() == "/v1/subscribers/team%2Famina%20%C3%A9%3F%23"


def test_put_refuses_a_missing_or_dot_id(client: Hermesi) -> None:
    for bad in ("", ".", ".."):
        with pytest.raises(ValueError):
            client.subscribers.put(bad, locale="fr")


@respx.mock
def test_a_put_is_retried_because_it_is_idempotent(sleeper: Sleeper) -> None:
    route = respx.put(f"{BASE}/v1/subscribers/u").mock(
        side_effect=[httpx.Response(502, json=error_body()), httpx.Response(200, json=PROFILE)]
    )
    with Hermesi(KEY, base_url=BASE, retry=FAST, sleep=sleeper) as hermesi:
        hermesi.subscribers.put("u", locale="fr")

    assert route.call_count == 2


# --- subscribers: get, delete ------------------------------------------------------------------


@respx.mock
def test_get_returns_the_profile(client: Hermesi) -> None:
    respx.get(f"{BASE}/v1/subscribers/user_8821").respond(200, json=PROFILE)

    assert client.subscribers.get("user_8821").external_id == "user_8821"


@respx.mock
def test_delete_returns_nothing_on_204_and_is_safe_to_repeat(client: Hermesi) -> None:
    route = respx.delete(f"{BASE}/v1/subscribers/user_8821").respond(204)

    client.subscribers.delete("user_8821")
    client.subscribers.delete("user_8821")
    assert route.call_count == 2


# --- subscribers: channel identities and preferences -------------------------------------------


@respx.mock
def test_register_channel_sends_the_identity_and_returns_it(client: Hermesi) -> None:
    route = respx.post(f"{BASE}/v1/subscribers/user_8821/channels").respond(200, json=PROFILE["channels"][0])

    identity = client.subscribers.register_channel("user_8821", "push", "fcm_1", {"platform": "android"})

    assert body_of(route) == {"channel": "push", "identifier": "fcm_1", "metadata": {"platform": "android"}}
    assert (identity.channel, identity.identifier, identity.state) == ("push", "fcm_1", "active")


@respx.mock
def test_register_channel_leaves_metadata_out_when_there_is_none(client: Hermesi) -> None:
    route = respx.post(f"{BASE}/v1/subscribers/u/channels").respond(200, json=PROFILE["channels"][0])

    client.subscribers.register_channel("u", "push", "t")

    assert body_of(route) == {"channel": "push", "identifier": "t"}


@respx.mock
def test_remove_channel_encodes_an_identifier_that_is_a_url(client: Hermesi) -> None:
    route = respx.delete(url__regex=rf"{BASE}/v1/subscribers/user_8821/channels/.*").respond(204)

    client.subscribers.remove_channel("user_8821", "push", "https://push.example.cm/send/abc")

    assert route.calls.last.request.url.raw_path.decode() == "/v1/subscribers/user_8821/channels/push/https%3A%2F%2Fpush.example.cm%2Fsend%2Fabc"


def test_register_and_remove_refuse_missing_parts(client: Hermesi) -> None:
    for call in (
        lambda: client.subscribers.register_channel("u", "", "t"),
        lambda: client.subscribers.register_channel("u", "push", ""),
        lambda: client.subscribers.remove_channel("u", "", "t"),
        lambda: client.subscribers.remove_channel("u", "push", ".."),
    ):
        with pytest.raises(ValueError):
            call()


@respx.mock
def test_preferences_are_read(client: Hermesi) -> None:
    respx.get(f"{BASE}/v1/subscribers/u/preferences").respond(200, json={"global": {"sms": False}, "categories": {"marketing": {"email": False}}})

    prefs = client.subscribers.preferences("u")

    assert prefs.global_ == {"sms": False} and prefs.categories == {"marketing": {"email": False}}


@respx.mock
def test_updating_preferences_sends_true_false_and_null(client: Hermesi) -> None:
    route = respx.patch(f"{BASE}/v1/subscribers/u/preferences").respond(200, json={"global": {}, "categories": {"marketing": {"push": False}}})

    prefs = client.subscribers.update_preferences("u", global_={"sms": None}, categories={"marketing": {"email": None, "push": False}})

    assert body_of(route) == {"global": {"sms": None}, "categories": {"marketing": {"email": None, "push": False}}}
    assert prefs.categories == {"marketing": {"push": False}}


@respx.mock
def test_updating_preferences_sends_only_the_part_given(client: Hermesi) -> None:
    route = respx.patch(f"{BASE}/v1/subscribers/u/preferences").respond(200, json={"global": {"sms": False}, "categories": {}})

    client.subscribers.update_preferences("u", global_={"sms": False})

    assert body_of(route) == {"global": {"sms": False}}


def test_updating_nothing_is_an_error_not_an_empty_request(client: Hermesi) -> None:
    with pytest.raises(ValueError):
        client.subscribers.update_preferences("u")


@respx.mock
def test_a_critical_or_unknown_category_refuses_the_whole_update(client: Hermesi) -> None:
    respx.patch(f"{BASE}/v1/subscribers/u/preferences").respond(
        422, json=error_body("critical_category_preference_not_allowed", "validation_error", detail=[{"field": "categories.security", "issue": "category is critical"}])
    )

    with pytest.raises(ValidationError) as caught:
        client.subscribers.update_preferences("u", categories={"security": {"email": False}})

    assert caught.value.code == "critical_category_preference_not_allowed"


# --- direct messages ---------------------------------------------------------------------------

SENT = {"message_id": "msg_1", "status": "queued", "messages": [{"id": "msg_1", "channel": "sms", "status": "queued", "reason": None}]}


@respx.mock
def test_sends_a_message_with_only_the_fields_given(client: Hermesi) -> None:
    route = respx.post(f"{BASE}/v1/messages").respond(202, json=SENT)

    result = client.messages.send("sms", "user_8821", "otp-code", data={"code": "480219"}, category="security", priority="critical")

    assert body_of(route) == {
        "channel": "sms", "recipient": "user_8821", "template": "otp-code", "category": "security", "data": {"code": "480219"}, "priority": "critical",
    }
    assert (result.message_id, result.status, result.replayed) == ("msg_1", "queued", False)
    assert [(m.id, m.status, m.reason) for m in result.messages] == [("msg_1", "queued", None)]


@respx.mock
def test_optional_fields_are_left_out_when_not_given(client: Hermesi) -> None:
    route = respx.post(f"{BASE}/v1/messages").respond(202, json=SENT)

    client.messages.send("sms", "u", "otp-code")

    assert body_of(route) == {"channel": "sms", "recipient": "u", "template": "otp-code"}


@respx.mock
def test_an_inline_recipient_is_sent_in_its_wire_form(client: Hermesi) -> None:
    route = respx.post(f"{BASE}/v1/messages").respond(202, json=SENT)

    client.messages.send("sms", Subscriber("new_user", phone_e164="+237690000000"), "otp-code")

    assert body_of(route)["recipient"] == {"external_id": "new_user", "phone_e164": "+237690000000"}


@respx.mock
def test_a_key_is_generated_reported_and_kept_across_retries(sleeper: Sleeper) -> None:
    """The reason this endpoint exists in an SDK: a retry after a timeout must not send a second SMS."""
    route = respx.post(f"{BASE}/v1/messages").mock(side_effect=[httpx.Response(503, json=error_body()), httpx.Response(202, json=SENT)])
    with Hermesi(KEY, base_url=BASE, retry=FAST, sleep=sleeper) as hermesi:
        result = hermesi.messages.send("sms", "u", "otp-code")

    keys = [call.request.headers["idempotency-key"] for call in route.calls]
    assert len(keys) == 2 and keys[0] == keys[1], "the same key on every attempt"
    assert result.idempotency_key == keys[0]


@respx.mock
def test_the_callers_own_key_is_used_as_is_and_a_replay_is_reported(client: Hermesi) -> None:
    route = respx.post(f"{BASE}/v1/messages").respond(202, json=SENT, headers={"Idempotency-Replayed": "true"})

    result = client.messages.send("sms", "u", "otp-code", idempotency_key="otp-user_8821-482")

    assert route.calls.last.request.headers["idempotency-key"] == "otp-user_8821-482"
    assert result.replayed is True and result.idempotency_key == "otp-user_8821-482"


@respx.mock
def test_a_refused_message_is_a_result_not_an_exception(client: Hermesi) -> None:
    respx.post(f"{BASE}/v1/messages").respond(
        202, json={"message_id": "msg_2", "status": "suppressed", "messages": [{"id": "msg_2", "channel": "sms", "status": "suppressed", "reason": "sms address suppressed"}]}
    )

    result = client.messages.send("sms", "u", "otp-code")

    assert result.status == "suppressed" and result.messages[0].reason == "sms address suppressed"


@respx.mock
def test_a_key_reused_with_another_body_is_a_conflict_error(client: Hermesi) -> None:
    respx.post(f"{BASE}/v1/messages").respond(409, json=error_body("idempotency_key_reused", "conflict"))

    with pytest.raises(ConflictError) as caught:
        client.messages.send("sms", "u", "otp-code", idempotency_key="k")

    assert caught.value.status == 409 and caught.value.code == "idempotency_key_reused"


@respx.mock
def test_a_mistake_the_server_refuses_is_raised(client: Hermesi) -> None:
    respx.post(f"{BASE}/v1/messages").respond(422, json=error_body("template_variant_not_found", "validation_error"))

    with pytest.raises(ValidationError) as caught:
        client.messages.send("sms", "u", "email-only-template")

    assert caught.value.code == "template_variant_not_found"


def test_send_refuses_a_missing_channel_or_template(client: Hermesi) -> None:
    with pytest.raises(ValueError):
        client.messages.send("", "u", "t")
    with pytest.raises(ValueError):
        client.messages.send("sms", "u", "")


@respx.mock
def test_get_message_returns_how_far_it_got(client: Hermesi) -> None:
    respx.get(f"{BASE}/v1/messages/msg_1").respond(
        200, json={"id": "msg_1", "channel": "sms", "step_key": None, "status": "sent", "provider": "twilio", "failure_code": None,
                   "failure_message": None, "created_at": "2026-10-06T10:00:00Z", "terminal_at": None}
    )

    message = client.messages.get("msg_1")

    assert (message.id, message.status, message.provider, message.is_final) == ("msg_1", "sent", "twilio", False)


# --- simulate mode -----------------------------------------------------------------------------


def test_simulate_records_writes_and_sends_nothing() -> None:
    with Hermesi(simulate=True) as hermesi:
        profile = hermesi.subscribers.put("user_8821", email="a@b.cm", last_name=None)
        sent = hermesi.messages.send("sms", "user_8821", "otp-code", data={"code": "1"}, idempotency_key="k")
        hermesi.subscribers.register_channel("user_8821", "push", "tok")
        hermesi.subscribers.delete("user_8821")

    assert (profile.external_id, profile.email, profile.last_name) == ("user_8821", "a@b.cm", None)
    assert sent.status == "simulated" and sent.message_id.startswith("msg_simulated_")
    assert [(c.method, c.path) for c in hermesi.simulated_calls] == [
        ("PUT", "/v1/subscribers/user_8821"),
        ("POST", "/v1/messages"),
        ("POST", "/v1/subscribers/user_8821/channels"),
        ("DELETE", "/v1/subscribers/user_8821"),
    ]
    body = hermesi.simulated_calls[1].body
    assert hermesi.simulated_calls[1].idempotency_key == "k" and body is not None and body["data"] == {"code": "1"}


def test_simulate_cannot_read_and_says_so_instead_of_inventing_an_answer() -> None:
    with Hermesi(simulate=True) as hermesi:
        for read in (
            lambda: hermesi.events.get("evt_1"),
            lambda: hermesi.subscribers.get("u"),
            lambda: hermesi.subscribers.preferences("u"),
            lambda: hermesi.messages.get("msg_1"),
        ):
            with pytest.raises(HermesiSimulationError):
                read()
        assert hermesi.simulated_calls == []


def test_simulate_still_refuses_a_body_it_cannot_serialise() -> None:
    with Hermesi(simulate=True) as hermesi:
        with pytest.raises(TypeError):
            hermesi.subscribers.put("u", data={"x": object()})


def test_unset_is_falsy_distinct_from_none_and_prints_as_itself() -> None:
    assert UNSET is not None and not UNSET and repr(UNSET) == "UNSET"


# --- the asynchronous client does the same -----------------------------------------------------


@respx.mock
async def test_the_async_client_has_the_same_calls() -> None:
    put = respx.put(f"{BASE}/v1/subscribers/u").respond(200, json=PROFILE)
    send = respx.post(f"{BASE}/v1/messages").respond(202, json=SENT)
    get = respx.get(f"{BASE}/v1/events/evt_1").respond(200, json=EVENT_RUN)
    async with AsyncHermesi(KEY, base_url=BASE) as hermesi:
        profile = await hermesi.subscribers.put("u", locale=None)
        sent = await hermesi.messages.send("sms", "u", "otp-code")
        run = await hermesi.events.get("evt_1")

    assert json.loads(put.calls.last.request.content) == {"locale": None}
    assert profile.external_id == "user_8821" and sent.message_id == "msg_1" and run.event_id == "evt_1"
    assert send.calls.last.request.headers["idempotency-key"]
    assert get.called


async def test_the_async_client_simulates_and_refuses_reads_too() -> None:
    async with AsyncHermesi(simulate=True) as hermesi:
        sent = await hermesi.messages.send("sms", "u", "otp-code")
        with pytest.raises(HermesiSimulationError):
            await hermesi.events.get("evt_1")

    assert sent.status == "simulated" and len(hermesi.simulated_calls) == 1
