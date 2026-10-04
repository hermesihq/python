"""The shapes the SDK sends and receives."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Union


@dataclass(frozen=True)
class Subscriber:
    """A recipient described inline. Sent with an event, it creates or updates the subscriber on the fly."""

    external_id: str
    email: Optional[str] = None
    phone_e164: Optional[str] = None
    name: Optional[str] = None
    locale: Optional[str] = None
    data: Optional[dict[str, Any]] = None

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {"external_id": self.external_id}
        for key in ("email", "phone_e164", "name", "locale", "data"):
            value = getattr(self, key)
            if value is not None:
                wire[key] = value
        return wire


@dataclass(frozen=True)
class Actor:
    """Who did the thing the event reports, for templates that say "Ada commented"."""

    external_id: Optional[str] = None
    name: Optional[str] = None

    def to_wire(self) -> dict[str, Any]:
        return {
            key: value for key, value in (("external_id", self.external_id), ("name", self.name)) if value is not None
        }


#: A known subscriber's ``external_id``, or one described inline.
Recipient = Union[str, Subscriber]


@dataclass(frozen=True)
class NotificationSummary:
    """One notification an event produced: which subscriber, through which workflow."""

    id: str
    subscriber_id: str
    workflow: str


@dataclass(frozen=True)
class EventResult:
    """The answer to publishing an event. ``202``: the event is recorded and queued, nothing is delivered yet."""

    event_id: str
    status: str = "accepted"
    notifications: list[NotificationSummary] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: True when the server recognised the idempotency key and returned the original answer instead of creating an event.
    replayed: bool = False
    #: The key the request went out with, generated if the caller gave none. Quote it to find the event again.
    idempotency_key: str = ""

    @classmethod
    def from_wire(cls, body: dict[str, Any], *, replayed: bool, idempotency_key: str) -> EventResult:
        notifications = [
            NotificationSummary(
                id=str(item.get("id", "")),
                subscriber_id=str(item.get("subscriber_id", "")),
                workflow=str(item.get("workflow", "")),
            )
            for item in body.get("notifications") or []
            if isinstance(item, dict)
        ]
        return cls(
            event_id=str(body["event_id"]),
            status=str(body.get("status", "accepted")),
            notifications=notifications,
            warnings=[str(w) for w in body.get("warnings") or []],
            replayed=replayed,
            idempotency_key=idempotency_key,
        )


@dataclass(frozen=True)
class PreferenceLink:
    """A hosted preference page for one subscriber. It needs no login and works for about a year."""

    url: str
