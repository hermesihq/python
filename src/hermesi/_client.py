"""The clients: ``Hermesi`` (synchronous) and ``AsyncHermesi``.

Thin on purpose. They build a request, send it with retries, and turn the answer into a typed result or an exception. They make
no decision about notifications: that is the platform's job, and an SDK that decides is a second implementation of it.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional, TypeVar, Union

import httpx

from . import _calls
from ._calls import Call, HermesiSimulationError
from ._errors import HermesiAPIError, HermesiConnectionError, error_from_response
from ._models import Actor, EventResult, PreferenceLink, Recipient, Subscriber
from ._retry import RetryPolicy, is_retryable_status, parse_retry_after
from ._server_models import (
    UNSET,
    ChannelIdentity,
    EventRun,
    Message,
    MessageResult,
    Preferences,
    SimulatedCall,
    SubscriberProfile,
)
from ._tokens import mint_subscriber_token
from ._wire import encode as _encode_body
from ._wire import path_segment as _path_segment

T = TypeVar("T")

VERSION = "0.1.0"
DEFAULT_TIMEOUT = 30.0
ENV_KEY = "HERMESI_SECRET_KEY"
ENV_URL = "HERMESI_BASE_URL"


def _recipient_wire(recipient: Union[Recipient, list[Recipient]]) -> Any:
    if isinstance(recipient, list):
        return [_recipient_wire(item) for item in recipient]
    return recipient.to_wire() if isinstance(recipient, Subscriber) else recipient


def _instant(value: Union[str, datetime]) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("send_at must carry a timezone: a naive datetime is ambiguous about when it means")
        return value.isoformat()
    return value


@dataclass
class SimulatedEvent:
    """What ``simulate=True`` records instead of sending, so a test can assert on it."""

    name: str
    recipient: Union[Recipient, list[Recipient]]
    payload: dict[str, Any]
    idempotency_key: str
    options: dict[str, Any] = field(default_factory=dict)


class _Base:
    def __init__(
        self,
        api_key: Optional[str],
        *,
        base_url: Optional[str],
        timeout: float,
        retry: Optional[RetryPolicy],
        simulate: bool,
    ) -> None:
        self.simulate = simulate
        self.simulated: list[SimulatedEvent] = []
        #: With ``simulate=True``, every call that is not an event (subscribers, messages), recorded instead of sent.
        self.simulated_calls: list[SimulatedCall] = []
        key = api_key if api_key is not None else os.environ.get(ENV_KEY)
        url = base_url if base_url is not None else os.environ.get(ENV_URL)
        if key is None and not simulate:
            raise ValueError(f"api_key is required (or set {ENV_KEY}). It is your secret key, hm_sk_...")
        if key is not None and not key.startswith("hm_sk_"):
            raise ValueError(
                "api_key must be a secret key (hm_sk_...). A public key (hm_pk_...) is for browsers and apps and cannot publish events."
            )
        if url is None and not simulate:
            raise ValueError(f"base_url is required (or set {ENV_URL}), for example https://your-hermesi-host")
        self._key = key or ""
        self._base_url = (url or "http://simulated.invalid").rstrip("/")
        self._timeout = timeout
        self._retry = retry or RetryPolicy()
        self._counter = 0

    def __repr__(self) -> str:
        # Never the key: a client ends up in logs and tracebacks.
        return f"{type(self).__name__}(base_url={self._base_url!r}, simulate={self.simulate})"

    # --- building requests -------------------------------------------------------------------

    def _headers(self, idempotency_key: Optional[str]) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": f"hermesi-python/{VERSION} httpx/{httpx.__version__}",
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    @staticmethod
    def _event_body(
        name: str,
        recipient: Union[Recipient, list[Recipient]],
        payload: Optional[dict[str, Any]],
        actor: Optional[Actor],
        delay: Optional[str],
        send_at: Optional[Union[str, datetime]],
        override: Optional[dict[str, Any]],
        tenant: Optional[str],
    ) -> dict[str, Any]:
        if not name:
            raise ValueError("name is required, for example order.shipped")
        body: dict[str, Any] = {"name": name, "recipient": _recipient_wire(recipient), "payload": payload or {}}
        if actor is not None:
            body["actor"] = actor.to_wire()
        if delay is not None:
            body["delay"] = delay
        if send_at is not None:
            body["send_at"] = _instant(send_at)
        if override is not None:
            body["override"] = override
        if tenant is not None:
            body["tenant"] = tenant
        return body

    @staticmethod
    def _encode(body: dict[str, Any]) -> bytes:
        return _encode_body(body)

    def _simulate_call(self, call: Call[T]) -> T:
        """What ``simulate=True`` does with a call: record it and answer plausibly. A read has no plausible answer (nothing was sent,
        so there is nothing to read back), and says so instead of inventing one."""
        if call.simulated is None:
            raise HermesiSimulationError(
                f"{call.method} {call.path} reads from Hermesi, and this client is in simulate mode: nothing was sent, so there is nothing to read."
            )
        # Encoded as a real call would be, so a body that cannot be serialised fails in a test exactly as it would in production.
        call.encoded()
        self._counter += 1
        self.simulated_calls.append(SimulatedCall(call.method, call.path, call.body, call.idempotency_key))
        return call.simulated(self._counter)

    # --- reading answers ---------------------------------------------------------------------

    @staticmethod
    def _json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            return None

    def _refusal(self, response: httpx.Response) -> HermesiAPIError:
        return error_from_response(
            response.status_code, self._json(response), parse_retry_after(response.headers.get("Retry-After"))
        )

    def _event_result(self, response: httpx.Response, idempotency_key: str) -> EventResult:
        body = self._json(response)
        if not isinstance(body, dict) or "event_id" not in body:
            raise error_from_response(response.status_code, None, None)
        replayed = response.headers.get("Idempotency-Replayed", "").lower() == "true"
        return EventResult.from_wire(body, replayed=replayed, idempotency_key=idempotency_key)

    def _link(self, response: httpx.Response) -> PreferenceLink:
        body = self._json(response)
        if not isinstance(body, dict) or not isinstance(body.get("url"), str):
            raise error_from_response(response.status_code, None, None)
        return PreferenceLink(url=body["url"])

    # --- simulation --------------------------------------------------------------------------

    def _simulate_event(
        self,
        name: str,
        recipient: Union[Recipient, list[Recipient]],
        payload: Optional[dict[str, Any]],
        key: str,
        options: dict[str, Any],
    ) -> EventResult:
        self._counter += 1
        self.simulated.append(SimulatedEvent(name, recipient, payload or {}, key, options))
        return EventResult(event_id=f"evt_simulated_{self._counter}", status="simulated", idempotency_key=key)

    @staticmethod
    def _event_options(**options: Any) -> dict[str, Any]:
        return {k: v for k, v in options.items() if v is not None}

    # --- minting -----------------------------------------------------------------------------

    def _mint(self, external_id: str, environment_id: str, ttl_seconds: int) -> str:
        return mint_subscriber_token(
            self._key or "hm_sk_simulated", external_id, environment_id, ttl_seconds=ttl_seconds
        )


class Hermesi(_Base):
    """The synchronous client.

    >>> hermesi = Hermesi(api_key=os.environ["HERMESI_SECRET_KEY"], base_url="https://your-hermesi-host")  # doctest: +SKIP
    >>> hermesi.events.trigger("order.shipped", "user_8821", {"order_id": "4821"})  # doctest: +SKIP

    ``simulate=True`` sends nothing: events are recorded in ``hermesi.simulated`` for a test to assert on.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        base_url: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        retry: Optional[RetryPolicy] = None,
        simulate: bool = False,
        http_client: Optional[httpx.Client] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(api_key, base_url=base_url, timeout=timeout, retry=retry, simulate=simulate)
        self._owns_client = http_client is None
        self._http = http_client or httpx.Client(timeout=timeout)
        self._sleep = sleep
        self.events = Events(self)
        self.subscribers = Subscribers(self)
        self.messages = Messages(self)
        self.tokens = Tokens(self)

    def _run(self, call: Call[T]) -> T:
        if self.simulate:
            return self._simulate_call(call)
        response = self._send(call.method, call.path, call.encoded(), call.idempotency_key)
        return call.parse(response, call.idempotency_key or "")

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def __enter__(self) -> Hermesi:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _send(self, method: str, path: str, body: Optional[bytes], idempotency_key: Optional[str]) -> httpx.Response:
        """One call, with retries. Returns a 2xx response; raises ``HermesiAPIError`` or ``HermesiConnectionError`` otherwise."""
        retries = 0
        while True:
            try:
                response = self._http.request(
                    method,
                    self._base_url + path,
                    content=body,
                    headers=self._headers(idempotency_key),
                    timeout=self._timeout,
                )
            except httpx.TransportError as exc:
                delay = self._retry.delay(retries, None)
                if delay is None:
                    raise HermesiConnectionError(f"Could not reach Hermesi at {self._base_url}: {exc}") from exc
            else:
                if response.is_success:
                    return response
                if not is_retryable_status(response.status_code):
                    raise self._refusal(response)
                delay = self._retry.delay(retries, parse_retry_after(response.headers.get("Retry-After")))
                if delay is None:
                    raise self._refusal(response)
            retries += 1
            self._sleep(delay)


class AsyncHermesi(_Base):
    """The asynchronous client: the same calls, awaited."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        base_url: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        retry: Optional[RetryPolicy] = None,
        simulate: bool = False,
        http_client: Optional[httpx.AsyncClient] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        super().__init__(api_key, base_url=base_url, timeout=timeout, retry=retry, simulate=simulate)
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=timeout)
        self._sleep = sleep
        self.events = AsyncEvents(self)
        self.subscribers = AsyncSubscribers(self)
        self.messages = AsyncMessages(self)
        self.tokens = Tokens(self)

    async def _run(self, call: Call[T]) -> T:
        if self.simulate:
            return self._simulate_call(call)
        response = await self._send(call.method, call.path, call.encoded(), call.idempotency_key)
        return call.parse(response, call.idempotency_key or "")

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    async def __aenter__(self) -> AsyncHermesi:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _send(
        self, method: str, path: str, body: Optional[bytes], idempotency_key: Optional[str]
    ) -> httpx.Response:
        retries = 0
        while True:
            try:
                response = await self._http.request(
                    method,
                    self._base_url + path,
                    content=body,
                    headers=self._headers(idempotency_key),
                    timeout=self._timeout,
                )
            except httpx.TransportError as exc:
                delay = self._retry.delay(retries, None)
                if delay is None:
                    raise HermesiConnectionError(f"Could not reach Hermesi at {self._base_url}: {exc}") from exc
            else:
                if response.is_success:
                    return response
                if not is_retryable_status(response.status_code):
                    raise self._refusal(response)
                delay = self._retry.delay(retries, parse_retry_after(response.headers.get("Retry-After")))
                if delay is None:
                    raise self._refusal(response)
            retries += 1
            await self._sleep(delay)


# --- resources -----------------------------------------------------------------------------------


class Events:
    def __init__(self, client: Hermesi) -> None:
        self._client = client

    def trigger(
        self,
        name: str,
        recipient: Union[Recipient, list[Recipient]],
        payload: Optional[dict[str, Any]] = None,
        *,
        actor: Optional[Actor] = None,
        idempotency_key: Optional[str] = None,
        delay: Optional[str] = None,
        send_at: Optional[Union[str, datetime]] = None,
        override: Optional[dict[str, Any]] = None,
        tenant: Optional[str] = None,
    ) -> EventResult:
        """Tell Hermesi that something happened. ``202``: the event is recorded and queued; nothing is delivered yet.

        ``recipient`` is a subscriber's ``external_id``, a :class:`Subscriber` (created or updated on the fly), or a list of up
        to 100 of either. ``idempotency_key`` is generated if you give none, and reused across retries: **pass your own** when
        your code might run twice for the same thing (a webhook handler, a queue consumer), because only your key survives that.
        """
        client = self._client
        key = idempotency_key or str(uuid.uuid4())
        # Encoded before the simulate branch: a payload that cannot be serialised must fail in a test exactly as it would in
        # production, or simulate mode would hide the bug it exists to catch.
        encoded = client._encode(client._event_body(name, recipient, payload, actor, delay, send_at, override, tenant))
        if client.simulate:
            return client._simulate_event(
                name,
                recipient,
                payload,
                key,
                client._event_options(actor=actor, delay=delay, send_at=send_at, override=override, tenant=tenant),
            )
        response = client._send("POST", "/v1/events", encoded, key)
        return client._event_result(response, key)

    def get(self, event_id: str) -> EventRun:
        """What became of an event: the notification each recipient got and the messages each produced, with how far each got.

        A message's status moves on after the event was accepted, so poll it: a message is final when ``is_final`` is true. Never
        returns what was sent or the recipient's address. An event of another environment is a ``NotFoundError``."""
        return self._client._run(_calls.get_event(event_id))


class Subscribers:
    """The people you notify. ``put`` and ``patch`` follow one rule: **an argument you give is set, ``None`` clears the field, and one
    you leave out is left alone**, so a sync job that knows half a profile does not blank the other half."""

    def __init__(self, client: Hermesi) -> None:
        self._client = client

    def put(
        self,
        external_id: str,
        *,
        email: Optional[str] = UNSET,
        phone_e164: Optional[str] = UNSET,
        first_name: Optional[str] = UNSET,
        last_name: Optional[str] = UNSET,
        locale: Optional[str] = UNSET,
        timezone: Optional[str] = UNSET,
        avatar_url: Optional[str] = UNSET,
        data: Optional[dict[str, Any]] = UNSET,
    ) -> SubscriberProfile:
        """Create the subscriber or update it, idempotently. Call it when a user signs up and when their profile changes.

        ``data`` **replaces** the stored attributes (at most 32 KB); it is not merged. Values are checked by the server: an email
        must look like one, ``phone_e164`` must be E.164, ``locale`` a language tag, ``timezone`` an IANA name."""
        fields = dict(email=email, phone_e164=phone_e164, first_name=first_name, last_name=last_name, locale=locale,
                      timezone=timezone, avatar_url=avatar_url, data=data)
        return self._client._run(_calls.put_subscriber(external_id, fields))

    def patch(
        self,
        external_id: str,
        *,
        email: Optional[str] = UNSET,
        phone_e164: Optional[str] = UNSET,
        first_name: Optional[str] = UNSET,
        last_name: Optional[str] = UNSET,
        locale: Optional[str] = UNSET,
        timezone: Optional[str] = UNSET,
        avatar_url: Optional[str] = UNSET,
        data: Optional[dict[str, Any]] = UNSET,
    ) -> SubscriberProfile:
        """Like :meth:`put`, for a subscriber that must already exist: a ``NotFoundError`` otherwise, where ``put`` would create it."""
        fields = dict(email=email, phone_e164=phone_e164, first_name=first_name, last_name=last_name, locale=locale,
                      timezone=timezone, avatar_url=avatar_url, data=data)
        return self._client._run(_calls.put_subscriber(external_id, fields, partial=True))

    def get(self, external_id: str) -> SubscriberProfile:
        """The profile, the registered channel identities and the stored preference overrides."""
        return self._client._run(_calls.get_subscriber(external_id))

    def delete(self, external_id: str) -> None:
        """Erase the personal data Hermesi holds for this subscriber and anonymise the messages they received, keeping their
        status for your statistics. Idempotent: deleting one that is already gone succeeds. Inbox items, the stored text of messages
        and event payloads are **not** erased yet."""
        self._client._run(_calls.delete_subscriber(external_id))

    def register_channel(
        self, external_id: str, channel: str, identifier: str, metadata: Optional[dict[str, Any]] = None
    ) -> ChannelIdentity:
        """Attach a device token, a chat id or a Web Push endpoint to a subscriber, who must exist. Call it on every app start: it
        refreshes the identity and makes it active again if a provider had marked it invalid, and never duplicates it."""
        return self._client._run(_calls.register_channel(external_id, channel, identifier, metadata))

    def remove_channel(self, external_id: str, channel: str, identifier: str) -> None:
        """Remove a channel identity. Idempotent."""
        self._client._run(_calls.remove_channel(external_id, channel, identifier))

    def preferences(self, external_id: str) -> Preferences:
        """The preference overrides stored for the subscriber. Absent means no override."""
        return self._client._run(_calls.get_preferences(external_id))

    def update_preferences(
        self,
        external_id: str,
        *,
        global_: Optional[dict[str, Optional[bool]]] = UNSET,
        categories: Optional[dict[str, dict[str, Optional[bool]]]] = UNSET,
    ) -> Preferences:
        """``True`` or ``False`` sets an override, ``None`` removes it (the category default applies again). All or nothing: an
        unknown category, or a critical one, refuses the whole request. Returns the preferences as they are afterwards."""
        return self._client._run(_calls.update_preferences(external_id, global_, categories))

    def preference_link(self, external_id: str) -> PreferenceLink:
        """A link to the hosted preference page for one subscriber. It needs no login and works for about a year."""
        client = self._client
        segment = _path_segment(external_id)
        if client.simulate:
            return PreferenceLink(url=f"https://simulated.invalid/preferences/{segment}")
        response = client._send("POST", f"/v1/subscribers/{segment}/preference-link", b"{}", None)
        return client._link(response)


class Messages:
    """The direct send: one message, through one template, on a channel **you** choose. Use an event unless the channel is a
    requirement rather than a decision (the OTP that must be an SMS)."""

    def __init__(self, client: Hermesi) -> None:
        self._client = client

    def send(
        self,
        channel: str,
        recipient: Union[str, Subscriber],
        template: str,
        *,
        category: Optional[str] = None,
        data: Optional[dict[str, Any]] = None,
        priority: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> MessageResult:
        """Send ``template`` to ``recipient`` on ``channel``. Preferences, suppressions and the audit trail still apply: a refused
        message is reported in ``status`` and ``messages[].reason``, not raised. A mistake (unknown template, no variant for the
        channel, unknown recipient) is raised and creates nothing.

        ``data`` becomes the template's ``payload.*`` and is not kept once a provider has the message. ``idempotency_key`` is
        generated if you give none and reused across retries; **pass your own** when your code might run twice for the same thing,
        because for an OTP the difference is one SMS or two."""
        return self._client._run(
            _calls.send_message(
                channel, recipient, template, category=category, data=data, priority=priority, idempotency_key=idempotency_key
            )
        )

    def get(self, message_id: str) -> Message:
        """How far a message got. It is final when ``is_final`` is true. Never returns what was sent or to whom."""
        return self._client._run(_calls.get_message(message_id))


class Tokens:
    """Subscriber tokens, minted locally: no request is made."""

    def __init__(self, client: _Base) -> None:
        self._client = client

    def mint(self, external_id: str, *, environment_id: str, ttl_seconds: int = 3600) -> str:
        """A token that lets a browser or an app act as ``external_id``. Valid for at most an hour: mint a fresh one when asked.

        ``environment_id`` is the ``env_...`` id of the environment the secret key belongs to (shown in the dashboard).
        """
        return self._client._mint(external_id, environment_id, ttl_seconds)


class AsyncEvents:
    def __init__(self, client: AsyncHermesi) -> None:
        self._client = client

    async def trigger(
        self,
        name: str,
        recipient: Union[Recipient, list[Recipient]],
        payload: Optional[dict[str, Any]] = None,
        *,
        actor: Optional[Actor] = None,
        idempotency_key: Optional[str] = None,
        delay: Optional[str] = None,
        send_at: Optional[Union[str, datetime]] = None,
        override: Optional[dict[str, Any]] = None,
        tenant: Optional[str] = None,
    ) -> EventResult:
        """See :meth:`Events.trigger`."""
        client = self._client
        key = idempotency_key or str(uuid.uuid4())
        # Encoded before the simulate branch: a payload that cannot be serialised must fail in a test exactly as it would in
        # production, or simulate mode would hide the bug it exists to catch.
        encoded = client._encode(client._event_body(name, recipient, payload, actor, delay, send_at, override, tenant))
        if client.simulate:
            return client._simulate_event(
                name,
                recipient,
                payload,
                key,
                client._event_options(actor=actor, delay=delay, send_at=send_at, override=override, tenant=tenant),
            )
        response = await client._send("POST", "/v1/events", encoded, key)
        return client._event_result(response, key)

    async def get(self, event_id: str) -> EventRun:
        """See :meth:`Events.get`."""
        return await self._client._run(_calls.get_event(event_id))


class AsyncSubscribers:
    """See :class:`Subscribers`."""

    def __init__(self, client: AsyncHermesi) -> None:
        self._client = client

    async def put(
        self,
        external_id: str,
        *,
        email: Optional[str] = UNSET,
        phone_e164: Optional[str] = UNSET,
        first_name: Optional[str] = UNSET,
        last_name: Optional[str] = UNSET,
        locale: Optional[str] = UNSET,
        timezone: Optional[str] = UNSET,
        avatar_url: Optional[str] = UNSET,
        data: Optional[dict[str, Any]] = UNSET,
    ) -> SubscriberProfile:
        """See :meth:`Subscribers.put`."""
        fields = dict(email=email, phone_e164=phone_e164, first_name=first_name, last_name=last_name, locale=locale,
                      timezone=timezone, avatar_url=avatar_url, data=data)
        return await self._client._run(_calls.put_subscriber(external_id, fields))

    async def patch(
        self,
        external_id: str,
        *,
        email: Optional[str] = UNSET,
        phone_e164: Optional[str] = UNSET,
        first_name: Optional[str] = UNSET,
        last_name: Optional[str] = UNSET,
        locale: Optional[str] = UNSET,
        timezone: Optional[str] = UNSET,
        avatar_url: Optional[str] = UNSET,
        data: Optional[dict[str, Any]] = UNSET,
    ) -> SubscriberProfile:
        """See :meth:`Subscribers.patch`."""
        fields = dict(email=email, phone_e164=phone_e164, first_name=first_name, last_name=last_name, locale=locale,
                      timezone=timezone, avatar_url=avatar_url, data=data)
        return await self._client._run(_calls.put_subscriber(external_id, fields, partial=True))

    async def get(self, external_id: str) -> SubscriberProfile:
        """See :meth:`Subscribers.get`."""
        return await self._client._run(_calls.get_subscriber(external_id))

    async def delete(self, external_id: str) -> None:
        """See :meth:`Subscribers.delete`."""
        await self._client._run(_calls.delete_subscriber(external_id))

    async def register_channel(
        self, external_id: str, channel: str, identifier: str, metadata: Optional[dict[str, Any]] = None
    ) -> ChannelIdentity:
        """See :meth:`Subscribers.register_channel`."""
        return await self._client._run(_calls.register_channel(external_id, channel, identifier, metadata))

    async def remove_channel(self, external_id: str, channel: str, identifier: str) -> None:
        """See :meth:`Subscribers.remove_channel`."""
        await self._client._run(_calls.remove_channel(external_id, channel, identifier))

    async def preferences(self, external_id: str) -> Preferences:
        """See :meth:`Subscribers.preferences`."""
        return await self._client._run(_calls.get_preferences(external_id))

    async def update_preferences(
        self,
        external_id: str,
        *,
        global_: Optional[dict[str, Optional[bool]]] = UNSET,
        categories: Optional[dict[str, dict[str, Optional[bool]]]] = UNSET,
    ) -> Preferences:
        """See :meth:`Subscribers.update_preferences`."""
        return await self._client._run(_calls.update_preferences(external_id, global_, categories))

    async def preference_link(self, external_id: str) -> PreferenceLink:
        """See :meth:`Subscribers.preference_link`."""
        client = self._client
        segment = _path_segment(external_id)
        if client.simulate:
            return PreferenceLink(url=f"https://simulated.invalid/preferences/{segment}")
        response = await client._send("POST", f"/v1/subscribers/{segment}/preference-link", b"{}", None)
        return client._link(response)


class AsyncMessages:
    """See :class:`Messages`."""

    def __init__(self, client: AsyncHermesi) -> None:
        self._client = client

    async def send(
        self,
        channel: str,
        recipient: Union[str, Subscriber],
        template: str,
        *,
        category: Optional[str] = None,
        data: Optional[dict[str, Any]] = None,
        priority: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> MessageResult:
        """See :meth:`Messages.send`."""
        return await self._client._run(
            _calls.send_message(
                channel, recipient, template, category=category, data=data, priority=priority, idempotency_key=idempotency_key
            )
        )

    async def get(self, message_id: str) -> Message:
        """See :meth:`Messages.get`."""
        return await self._client._run(_calls.get_message(message_id))
