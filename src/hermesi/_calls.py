"""The calls beyond publishing an event, described once and run by the synchronous and the asynchronous client alike.

A call is data: a method, a path, a body, an idempotency key, how to read the answer, and what ``simulate=True`` returns instead.
Keeping that apart from *sending* it is what lets ``Hermesi`` and ``AsyncHermesi`` share every rule below instead of each carrying
a copy that can drift.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Callable, Generic, Optional, TypeVar, Union

import httpx

from ._errors import HermesiError, error_from_response
from ._models import Subscriber
from ._server_models import (
    UNSET,
    ChannelIdentity,
    EventRun,
    Message,
    MessageResult,
    Preferences,
    SubscriberProfile,
)
from ._wire import encode, path_segment

T = TypeVar("T")

PROFILE_FIELDS = ("email", "phone_e164", "first_name", "last_name", "locale", "timezone", "avatar_url", "data")


class HermesiSimulationError(HermesiError):
    """A read was asked of a client in ``simulate=True`` mode. Nothing was sent, so there is nothing to read back."""


@dataclass
class Call(Generic[T]):
    method: str
    path: str
    parse: Callable[[httpx.Response, str], T]
    body: Optional[dict[str, Any]] = None
    #: Sent as ``Idempotency-Key`` and reused by every retry. ``None`` for calls that are idempotent by nature.
    idempotency_key: Optional[str] = None
    #: What ``simulate=True`` returns. ``None`` marks a read, which has no meaningful simulated answer.
    simulated: Optional[Callable[[int], T]] = None

    def encoded(self) -> Optional[bytes]:
        return encode(self.body) if self.body is not None else None


def _object(response: httpx.Response, required: str) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict) or required not in body:
        raise error_from_response(response.status_code, None, None)
    return body


def _replayed(response: httpx.Response) -> bool:
    return str(response.headers.get("Idempotency-Replayed", "")).lower() == "true"


# --- events ------------------------------------------------------------------------------------


def get_event(event_id: str) -> Call[EventRun]:
    segment = path_segment(event_id, "event_id")
    return Call("GET", f"/v1/events/{segment}", parse=lambda r, _k: EventRun.from_wire(_object(r, "event_id")))


# --- subscribers -------------------------------------------------------------------------------


def _profile_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """What was given: a value sets, ``None`` clears, ``UNSET`` is left out of the body so the server leaves the field alone."""
    return {name: fields[name] for name in PROFILE_FIELDS if fields.get(name, UNSET) is not UNSET}


def _simulated_profile(external_id: str, body: dict[str, Any], n: int) -> SubscriberProfile:
    return SubscriberProfile.from_wire({"id": f"sub_simulated_{n}", "external_id": external_id, **body})


def put_subscriber(external_id: str, fields: dict[str, Any], *, partial: bool = False) -> Call[SubscriberProfile]:
    segment = path_segment(external_id)
    body = _profile_fields(fields)
    return Call(
        "PATCH" if partial else "PUT",
        f"/v1/subscribers/{segment}",
        body=body,
        parse=lambda r, _k: SubscriberProfile.from_wire(_object(r, "external_id")),
        simulated=lambda n: _simulated_profile(external_id, body, n),
    )


def get_subscriber(external_id: str) -> Call[SubscriberProfile]:
    segment = path_segment(external_id)
    return Call("GET", f"/v1/subscribers/{segment}", parse=lambda r, _k: SubscriberProfile.from_wire(_object(r, "external_id")))


def delete_subscriber(external_id: str) -> Call[None]:
    segment = path_segment(external_id)
    return Call("DELETE", f"/v1/subscribers/{segment}", parse=lambda r, _k: None, simulated=lambda n: None)


def register_channel(
    external_id: str, channel: str, identifier: str, metadata: Optional[dict[str, Any]]
) -> Call[ChannelIdentity]:
    segment = path_segment(external_id)
    if not channel:
        raise ValueError("channel is required, for example push")
    if not identifier:
        raise ValueError("identifier is required: a device token, a chat id or a Web Push endpoint")
    body: dict[str, Any] = {"channel": channel, "identifier": identifier}
    if metadata is not None:
        body["metadata"] = metadata
    return Call(
        "POST",
        f"/v1/subscribers/{segment}/channels",
        body=body,
        parse=lambda r, _k: ChannelIdentity.from_wire(_object(r, "identifier")),
        simulated=lambda n: ChannelIdentity(channel=channel, identifier=identifier, metadata=dict(metadata or {})),
    )


def remove_channel(external_id: str, channel: str, identifier: str) -> Call[None]:
    segment = path_segment(external_id)
    if not channel:
        raise ValueError("channel is required, for example push")
    # `identifier` is the rest of the path on the server, so a Web Push endpoint URL or a Teams `channel:<team>/<channel>` goes in
    # percent-encoded as one segment.
    return Call(
        "DELETE",
        f"/v1/subscribers/{segment}/channels/{path_segment(channel, 'channel')}/{path_segment(identifier, 'identifier')}",
        parse=lambda r, _k: None,
        simulated=lambda n: None,
    )


def get_preferences(external_id: str) -> Call[Preferences]:
    segment = path_segment(external_id)
    return Call("GET", f"/v1/subscribers/{segment}/preferences", parse=lambda r, _k: Preferences.from_wire(_object(r, "global")))


def update_preferences(external_id: str, global_: Any, categories: Any) -> Call[Preferences]:
    segment = path_segment(external_id)
    body: dict[str, Any] = {}
    if global_ is not UNSET:
        body["global"] = global_
    if categories is not UNSET:
        body["categories"] = categories
    if not body:
        raise ValueError("give global_ or categories: there is nothing to change")
    return Call(
        "PATCH",
        f"/v1/subscribers/{segment}/preferences",
        body=body,
        parse=lambda r, _k: Preferences.from_wire(_object(r, "global")),
        simulated=lambda n: Preferences(
            global_={k: v for k, v in (body.get("global") or {}).items() if v is not None},
            categories={c: {k: v for k, v in ch.items() if v is not None} for c, ch in (body.get("categories") or {}).items()},
        ),
    )


# --- messages ----------------------------------------------------------------------------------


def send_message(
    channel: str,
    recipient: Union[str, Subscriber],
    template: str,
    *,
    category: Optional[str],
    data: Optional[dict[str, Any]],
    priority: Optional[str],
    idempotency_key: Optional[str],
) -> Call[MessageResult]:
    if not channel:
        raise ValueError("channel is required, for example sms")
    if not template:
        raise ValueError("template is required: the key of a published template")
    key = idempotency_key or str(uuid.uuid4())
    body: dict[str, Any] = {
        "channel": channel,
        "recipient": recipient.to_wire() if isinstance(recipient, Subscriber) else recipient,
        "template": template,
    }
    if category is not None:
        body["category"] = category
    if data is not None:
        body["data"] = data
    if priority is not None:
        body["priority"] = priority
    return Call(
        "POST",
        "/v1/messages",
        body=body,
        idempotency_key=key,
        parse=lambda r, k: MessageResult.from_wire(_object(r, "message_id"), replayed=_replayed(r), idempotency_key=k),
        simulated=lambda n: MessageResult(message_id=f"msg_simulated_{n}", status="simulated", idempotency_key=key),
    )


def get_message(message_id: str) -> Call[Message]:
    segment = path_segment(message_id, "message_id")
    return Call("GET", f"/v1/messages/{segment}", parse=lambda r, _k: Message.from_wire(_object(r, "id")))
