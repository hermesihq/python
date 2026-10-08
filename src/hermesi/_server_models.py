"""The shapes of the rest of the server API: subscribers, messages, and what became of an event."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


class _Unset:
    """The type of :data:`UNSET`."""

    _instance: Optional[_Unset] = None

    def __new__(cls) -> _Unset:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "UNSET"

    def __bool__(self) -> bool:
        return False


#: "Not given", as opposed to ``None``. ``subscribers.put("u", phone_e164=None)`` **clears** the phone number; leaving the argument
#: out leaves it alone. Python cannot tell those apart with ``None`` alone, hence this.
UNSET: Any = _Unset()


@dataclass(frozen=True)
class ChannelIdentity:
    """A destination registered for a subscriber: a device token, a chat id, a Web Push endpoint."""

    channel: str
    identifier: str
    #: ``active``, or ``invalid`` / ``unsubscribed`` once a provider or the person said so.
    state: str = "active"
    state_reason: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    verified_at: Optional[str] = None
    last_used_at: Optional[str] = None

    @classmethod
    def from_wire(cls, body: dict[str, Any]) -> ChannelIdentity:
        return cls(
            channel=str(body["channel"]),
            identifier=str(body["identifier"]),
            state=str(body.get("state", "active")),
            state_reason=body.get("state_reason"),
            metadata=dict(body.get("metadata") or {}),
            verified_at=body.get("verified_at"),
            last_used_at=body.get("last_used_at"),
        )


@dataclass(frozen=True)
class Preferences:
    """The overrides a subscriber has stored. A channel or category that is absent has none and follows the category's default."""

    #: Per channel, for every category: ``{"sms": False}``.
    global_: dict[str, bool] = field(default_factory=dict)
    #: Per category key, then per channel: ``{"marketing": {"email": False}}``.
    categories: dict[str, dict[str, bool]] = field(default_factory=dict)

    @classmethod
    def from_wire(cls, body: dict[str, Any]) -> Preferences:
        return cls(
            global_={str(k): bool(v) for k, v in (body.get("global") or {}).items()},
            categories={
                str(category): {str(channel): bool(value) for channel, value in (channels or {}).items()}
                for category, channels in (body.get("categories") or {}).items()
            },
        )


@dataclass(frozen=True)
class SubscriberProfile:
    """What Hermesi holds about a subscriber. (``Subscriber`` is the shape you *send* inline with an event.)"""

    id: str
    external_id: str
    email: Optional[str] = None
    phone_e164: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    locale: Optional[str] = None
    timezone: Optional[str] = None
    avatar_url: Optional[str] = None
    data: dict[str, Any] = field(default_factory=dict)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    channels: list[ChannelIdentity] = field(default_factory=list)
    preferences: Preferences = field(default_factory=Preferences)

    @classmethod
    def from_wire(cls, body: dict[str, Any]) -> SubscriberProfile:
        return cls(
            id=str(body["id"]),
            external_id=str(body["external_id"]),
            email=body.get("email"),
            phone_e164=body.get("phone_e164"),
            first_name=body.get("first_name"),
            last_name=body.get("last_name"),
            locale=body.get("locale"),
            timezone=body.get("timezone"),
            avatar_url=body.get("avatar_url"),
            data=dict(body.get("data") or {}),
            created_at=body.get("created_at"),
            updated_at=body.get("updated_at"),
            channels=[ChannelIdentity.from_wire(c) for c in body.get("channels") or [] if isinstance(c, dict)],
            preferences=Preferences.from_wire(body.get("preferences") or {}),
        )


@dataclass(frozen=True)
class Message:
    """A message and how far it got. It is final when ``terminal_at`` is set."""

    id: str
    channel: str
    #: ``queued``, ``routing``, ``sent``, ``delivered``, ``opened``, ``clicked``, or a refusal: ``failed``, ``bounced``,
    #: ``suppressed``, ``skipped``, ``cancelled``.
    status: str
    step_key: Optional[str] = None
    provider: Optional[str] = None
    failure_code: Optional[str] = None
    failure_message: Optional[str] = None
    created_at: Optional[str] = None
    terminal_at: Optional[str] = None

    @property
    def is_final(self) -> bool:
        """True once nothing more will happen to this message."""
        return self.terminal_at is not None

    @classmethod
    def from_wire(cls, body: dict[str, Any]) -> Message:
        return cls(
            id=str(body["id"]),
            channel=str(body["channel"]),
            status=str(body["status"]),
            step_key=body.get("step_key"),
            provider=body.get("provider"),
            failure_code=body.get("failure_code"),
            failure_message=body.get("failure_message"),
            created_at=body.get("created_at"),
            terminal_at=body.get("terminal_at"),
        )


@dataclass(frozen=True)
class RunNotification:
    """One run of one workflow for one recipient, with the messages it produced."""

    id: str
    subscriber_id: str
    external_id: str
    status: str
    workflow: Optional[str] = None
    workflow_version: Optional[int] = None
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    #: While ``waiting``: when the run goes on (a delay, a schedule, or a fallback window).
    resume_at: Optional[str] = None
    messages: list[Message] = field(default_factory=list)

    @classmethod
    def from_wire(cls, body: dict[str, Any]) -> RunNotification:
        return cls(
            id=str(body["id"]),
            subscriber_id=str(body["subscriber_id"]),
            external_id=str(body["external_id"]),
            status=str(body["status"]),
            workflow=body.get("workflow"),
            workflow_version=body.get("workflow_version"),
            created_at=body.get("created_at"),
            started_at=body.get("started_at"),
            completed_at=body.get("completed_at"),
            resume_at=body.get("resume_at"),
            messages=[Message.from_wire(m) for m in body.get("messages") or [] if isinstance(m, dict)],
        )


@dataclass(frozen=True)
class EventRun:
    """An event and everything it caused: ``events.get(event_id)``."""

    event_id: str
    name: str
    #: ``processed``, ``no_workflow`` (no active workflow matched), or ``invalid`` (a strict payload schema refused it).
    status: str
    payload: dict[str, Any] = field(default_factory=dict)
    actor: Optional[dict[str, Any]] = None
    idempotency_key: Optional[str] = None
    error: Optional[dict[str, Any]] = None
    received_at: Optional[str] = None
    processed_at: Optional[str] = None
    notifications: list[RunNotification] = field(default_factory=list)

    @property
    def messages(self) -> list[Message]:
        """Every message of every notification, flattened."""
        return [m for n in self.notifications for m in n.messages]

    @classmethod
    def from_wire(cls, body: dict[str, Any]) -> EventRun:
        return cls(
            event_id=str(body["event_id"]),
            name=str(body["name"]),
            status=str(body["status"]),
            payload=dict(body.get("payload") or {}),
            actor=body.get("actor"),
            idempotency_key=body.get("idempotency_key"),
            error=body.get("error"),
            received_at=body.get("received_at"),
            processed_at=body.get("processed_at"),
            notifications=[RunNotification.from_wire(n) for n in body.get("notifications") or [] if isinstance(n, dict)],
        )


@dataclass(frozen=True)
class MessageCreated:
    """One message a direct send created."""

    id: str
    channel: str
    #: ``queued`` when it will be sent; ``skipped`` or ``suppressed`` when preferences or the suppression list refused it.
    status: str
    reason: Optional[str] = None


@dataclass(frozen=True)
class BulkSubscriberResult:
    """One row of a bulk import, as it came out."""

    external_id: str
    id: str
    #: ``"created"``: a subscriber that did not exist (or had been deleted, which comes back empty). ``"updated"``: one that did.
    status: str


@dataclass(frozen=True)
class BulkSubscribersResult:
    """The answer to ``subscribers.bulk``: one entry per row you sent, in the same order."""

    created: int
    updated: int
    subscribers: list[BulkSubscriberResult] = field(default_factory=list)

    @classmethod
    def from_wire(cls, body: dict[str, Any]) -> BulkSubscribersResult:
        rows = [
            BulkSubscriberResult(external_id=str(r["external_id"]), id=str(r["id"]), status=str(r["status"]))
            for r in body.get("subscribers") or []
            if isinstance(r, dict)
        ]
        return cls(created=int(body.get("created", 0)), updated=int(body.get("updated", 0)), subscribers=rows)


@dataclass(frozen=True)
class MessageResult:
    """The answer to ``messages.send``. ``202``: recorded and queued, nothing is delivered yet."""

    message_id: str
    status: str
    messages: list[MessageCreated] = field(default_factory=list)
    #: True when the server recognised the idempotency key and returned the original answer instead of sending again.
    replayed: bool = False
    #: The key the request went out with, generated if the caller gave none.
    idempotency_key: str = ""

    @classmethod
    def from_wire(cls, body: dict[str, Any], *, replayed: bool, idempotency_key: str) -> MessageResult:
        return cls(
            message_id=str(body["message_id"]),
            status=str(body["status"]),
            messages=[
                MessageCreated(id=str(m["id"]), channel=str(m["channel"]), status=str(m["status"]), reason=m.get("reason"))
                for m in body.get("messages") or []
                if isinstance(m, dict)
            ],
            replayed=replayed,
            idempotency_key=idempotency_key,
        )


@dataclass(frozen=True)
class SimulatedCall:
    """What ``simulate=True`` records for every call that is not an event, so a test can assert on it."""

    method: str
    path: str
    body: Optional[dict[str, Any]] = None
    idempotency_key: Optional[str] = None
