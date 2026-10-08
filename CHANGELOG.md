# Changelog

All notable changes to `hermesi`. This file describes what a consumer gets.

**`0.x` means the public API can still change.** A minor bump may contain a breaking change; a patch bump will not. Each release
lists breaking changes first.

## 0.3.0 (2026-10-08)

### Added

- **`subscribers.bulk(rows)`** (and the async one): create or update up to 1 000 subscribers in one request, for a first import or a
  nightly sync. Each row is a mapping with an `external_id` and any `put` field, with the same meaning (a key you include is set, `None`
  clears it, a key you leave out is left alone). A key that is none of those is refused naming its row. All or nothing: a server refusal
  lists every problem with its row and writes nothing. `BulkSubscribersResult` and `BulkSubscriberResult` say which rows were created
  and which updated.

## 0.2.0 (2026-10-07)

### Added

- **`events.get(event_id)`**: the notification each recipient got from an event and the messages each produced, with how far each
  got. `EventRun`, `RunNotification`, `Message` (`is_final` tells when nothing more will happen).
- **Subscribers**: `subscribers.put`, `patch`, `get`, `delete`, `register_channel`, `remove_channel`, `preferences` and
  `update_preferences`. An argument you give is set, `None` clears the field and one you leave out is left alone (`UNSET` is the
  "not given" marker); `data` replaces. `SubscriberProfile`, `ChannelIdentity`, `Preferences`.
- **`messages.send` and `messages.get`**: the direct send, for when the channel is a requirement (an OTP that must be an SMS). It
  keeps one idempotency key across its retries, generated if you give none. `MessageResult`.
- `ConflictError` for a `409`, `HermesiSimulationError`, `simulated_calls` and `SimulatedCall` for test mode. In test mode reads raise
  rather than invent an answer.

### Changed

- Internal: the request-building rules shared by the synchronous and the asynchronous client moved out of `_client.py`; no behaviour
  change for `events.trigger`, `subscribers.preference_link` or `tokens.mint`.

### Fixed

- **The documentation showed `delay="15m"`**, a format the server does not accept: `delay` is an ISO 8601 duration, `PT15M`. It
  also did not say that the API ignored `send_at` and `delay` until now; Hermesi now honours them (or refuses a request it cannot
  honour with `422 invalid_schedule`). The README has a Scheduling section.

## 0.1.0 (2026-10-04)

First release.

- `Hermesi` and `AsyncHermesi`: `events.trigger`, `subscribers.preference_link`, `tokens.mint`.
- Retries on connection failures, timeouts, `429` (honouring `Retry-After`) and `5xx`, with exponential backoff and jitter; an
  idempotency key on every event, generated if none is given and kept across the retries.
- Typed exceptions: `AuthenticationError`, `ForbiddenError`, `NotFoundError`, `ValidationError`, `RateLimitError`,
  `ServerError`, `HermesiConnectionError`.
- `mint_subscriber_token`, and `simulate=True` to test code that publishes events without a network.
- A subscriber id of `.` or `..` is refused: a URL parser resolves it even when escaped, which would aim a request carrying your
  secret key at another endpoint.
